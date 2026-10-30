"""管理服务测试：种子导入、退订 tombstone、站点 CRUD、群设置、审计。"""

import pytest

from diffbot_core.commands import CommandError
from diffbot_core.config import BotConfig, GroupSub, SiteFilter, WikiSite
from diffbot_core.manage import ManagementService
from diffbot_core.store import Store


def make(tmp_path, *, sites=None, subs=None):
    cfg = BotConfig()
    cfg.data_dir = tmp_path / "data"
    cfg.sites = sites or {
        "doors_en": WikiSite(
            id="doors_en", name="DOORS EN", api_url="https://en.example/w/api.php",
            base_url="https://en.example", language="en",
        ),
        "doors_zh": WikiSite(
            id="doors_zh", name="DOORS ZH", api_url="https://zh.example/w/api.php",
            base_url="https://zh.example", language="zh",
        ),
    }
    cfg.subscriptions = subs or [
        GroupSub(group_id="g1", sites=["doors_en", "doors_zh"], filters={})
    ]
    store = Store(cfg.data_dir / "test.db")
    store.init()
    manage = ManagementService(store, cfg)
    manage.seed_from_config()
    return manage, store, cfg


def test_seed_imports_sites_and_subs(tmp_path):
    manage, store, _ = make(tmp_path)
    assert store.get_site("doors_en") is not None
    assert ("g1", "doors_en") in store.list_subs()
    # 幂等：重复导入不产生重复行
    manage.seed_from_config()
    assert len(store.list_subs("g1")) == 2
    store.close()


def test_unsubscribe_writes_tombstone_and_blocks_reseed(tmp_path):
    """回归缺陷 1：YAML 种子订阅必须能退订且不复活。"""
    manage, store, _ = make(tmp_path)
    assert manage.unsubscribe("g1", "doors_en") is True
    assert ("g1", "doors_en") not in store.list_subs()
    assert store.has_tombstone("g1", "doors_en")
    manage.seed_from_config()  # 模拟重启
    assert ("g1", "doors_en") not in store.list_subs()
    # 重新订阅应清除 tombstone
    manage.subscribe("g1", "doors_en")
    assert ("g1", "doors_en") in store.list_subs()
    assert not store.has_tombstone("g1", "doors_en")
    store.close()


def test_routing_reflects_db_only(tmp_path):
    """回归缺陷 2：路由以 DB 为准，退订后不再路由。"""
    manage, store, _ = make(tmp_path)
    routing = manage.routing()
    assert set(routing) == {"doors_en", "doors_zh"}
    manage.unsubscribe("g1", "doors_zh")
    assert set(manage.routing()) == {"doors_en"}
    store.close()


def test_resolve_site_ambiguity_and_missing(tmp_path):
    manage, store, _ = make(tmp_path)
    assert manage.resolve_site("g1", "doors_en").id == "doors_en"
    with pytest.raises(CommandError) as ei:
        manage.resolve_site("g1", "doors")
    assert "多个站点" in ei.value.user_message
    with pytest.raises(CommandError):
        manage.resolve_site("g1", "nope")
    # 只有一个订阅时可省略站点
    manage.unsubscribe("g1", "doors_zh")
    assert manage.resolve_site("g1", None).id == "doors_en"
    store.close()


def test_resolve_site_requires_explicit_when_multiple(tmp_path):
    manage, store, _ = make(tmp_path)
    with pytest.raises(CommandError) as ei:
        manage.resolve_site("g1", None)
    assert "@站点ID" in ei.value.user_message
    store.close()


def test_group_settings_defaults_and_override(tmp_path):
    manage, store, cfg = make(tmp_path)
    cfg.push.visual = True
    cfg.push.throttle_per_minute = 5
    assert manage.get_group_visual("g1") is True          # 缺省继承全局
    manage.set_group_visual("g1", "10001", False)
    assert manage.get_group_visual("g1") is False
    assert manage.get_group_visual("g2") is True          # 其它群不受影响
    assert manage.get_group_throttle("g1") == 5
    assert manage.get_group_digest("g1") is None
    store.close()


def test_group_lang_and_admins(tmp_path):
    manage, store, _ = make(tmp_path)
    assert manage.get_group_lang("g1") == "zh"
    assert manage.set_group_lang("g1", "10001", "en") == "en"
    with pytest.raises(CommandError):
        manage.set_group_lang("g1", "10001", "fr")
    manage.add_group_admin("g1", "10001", "20002")
    assert "20002" in manage.get_group_admins("g1")
    manage.remove_group_admin("g1", "10001", "20002")
    assert manage.get_group_admins("g1") == []
    with pytest.raises(CommandError):
        manage.add_group_admin("g1", "10001", "abc")
    store.close()


def test_filter_and_watch(tmp_path):
    manage, store, _ = make(tmp_path)
    flt = manage.set_filter("g1", "doors_en", namespaces=[0, 1], min_sizediff=10)
    assert flt.namespaces == [0, 1]
    assert manage.get_filter("g1", "doors_en").min_sizediff == 10
    manage.watch_add("g1", "doors_en", "首页")
    assert manage.watch_list("g1", "doors_en") == ["首页"]
    assert manage.watch_clear("g1", "doors_en") == 1
    assert manage.watch_list("g1", "doors_en") == []
    store.close()


def test_audit_written(tmp_path):
    manage, store, _ = make(tmp_path)
    manage.set_group_visual("g1", "10001", True)
    rows = store.list_audit()
    assert rows and rows[0]["action"] == "visual_on"
    assert rows[0]["user_id"] == "10001"
    store.close()


def test_update_and_delete_site(tmp_path):
    import asyncio

    manage, store, _ = make(tmp_path)

    async def run():
        site = await manage.update_site("g1", "10001", "doors_en", name="新名字", lang="en")
        assert site.name == "新名字"
        with pytest.raises(CommandError):
            await manage.update_site("g1", "10001", "doors_en")
        deleted = await manage.delete_site("g1", "10001", "doors_zh")
        assert deleted == "doors_zh"

    asyncio.run(run())
    assert store.get_site("doors_zh") is None
    assert ("g1", "doors_zh") not in store.list_subs()
    store.close()


def test_site_api_conflict_detected(tmp_path):
    manage, store, _ = make(tmp_path)
    with pytest.raises(ValueError):
        store.upsert_site(
            WikiSite(id="other", name="x", api_url="https://en.example/w/api.php",
                     base_url="https://en.example"),
            source="dynamic",
        )
    store.close()
