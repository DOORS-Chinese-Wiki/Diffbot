import asyncio

import pytest

from diffbot_core.config import BotConfig, GroupSub, PollConfig, PushConfig, SiteFilter, WikiSite
from diffbot_core.models import DiffInfo, DiffLine
from diffbot_core.monitor import MonitorEngine, merge_subs
from diffbot_core.store import Store


class FakeClient:
    def __init__(self, batches, diffs=None, server_time="2026-01-01T00:00:00Z"):
        # batches: list of change-dict lists returned per recentchanges call
        self.batches = list(batches)
        self.diffs = diffs or {}
        self._server_time = server_time
        self.calls = 0

    async def recentchanges(self, *, newer_than=None, limit=50, namespaces=None):
        self.calls += 1
        if self.batches:
            return self.batches.pop(0)
        return []

    async def compare(self, fromrev, torev, *, uselang=None):
        return self.diffs.get((fromrev, torev), DiffInfo(lines=[DiffLine("add", "内容")], torevid=torev))

    async def page_content(self, revid):
        return "新页面内容第一行\n第二行"

    async def server_time(self):
        return self._server_time

    async def close(self):
        pass


def make_engine(tmp_path, site=None, subs=None):
    cfg = BotConfig()
    site = site or WikiSite(
        id="zh_test", name="测试", api_url="https://x/w/api.php", base_url="https://x",
        language="zh", timezone="UTC",
    )
    cfg.sites = {"zh_test": site}
    cfg.subscriptions = subs or [GroupSub(group_id="g1", sites=["zh_test"], filters={})]
    cfg.data_dir = tmp_path / "data"
    store = Store(cfg.data_dir / "test.db")
    store.init()
    sent = []
    engine = MonitorEngine(cfg, store, lambda gid, text, image_path=None, visual=None, throttle_per_minute=None: _record(sent, gid, text, image_path))
    return engine, store, sent


async def _record(sent, gid, text, image_path=None):
    sent.append((gid, text))


def rc(n=1, ts="2026-01-01T00:01:00Z", rcid=None, title="页面A", ns=0, user="张三",
       revid=100, old_revid=99, newlen=200, oldlen=100):
    base_rcid = rcid if rcid is not None else n
    return {"rcid": base_rcid, "pageid": 7, "title": title, "ns": ns, "revid": revid,
            "old_revid": old_revid, "user": user, "timestamp": ts, "comment": "编辑",
            "newlen": newlen, "oldlen": oldlen, "type": "edit" if old_revid else "new"}


async def test_first_run_no_push(tmp_path):
    engine, store, sent = make_engine(tmp_path)
    engine.clients["zh_test"] = FakeClient([[rc()]])
    await engine._poll_once(engine.cfg.sites["zh_test"], first=True)
    assert sent == []
    ts, last = store.get_cursor("zh_test")
    assert ts == "2026-01-01T00:01:00Z" and last == 1
    await engine.stop()


async def test_poll_pushes_to_subscribed_group(tmp_path):
    engine, store, sent = make_engine(tmp_path)
    store.set_cursor("zh_test", "2026-01-01T00:00:00Z", 0)
    engine.clients["zh_test"] = FakeClient([[rc(rcid=5, ts="2026-01-01T00:02:00Z")]])
    await engine._poll_once(engine.cfg.sites["zh_test"])
    assert len(sent) == 1
    gid, text = sent[0]
    assert gid == "g1"
    assert "页面A" in text and "张三" in text
    await engine.stop()


async def test_namespace_filter(tmp_path):
    sub = GroupSub(group_id="g1", sites=["zh_test"],
                   filters={"zh_test": SiteFilter(namespaces=[0])})
    engine, store, sent = make_engine(tmp_path, subs=[sub])
    store.set_cursor("zh_test", "2026-01-01T00:00:00Z", 0)
    engine.clients["zh_test"] = FakeClient(
        [[rc(rcid=6, ns=1, title="Talk:页面A"), rc(rcid=7, ns=0)]]
    )
    await engine._poll_once(engine.cfg.sites["zh_test"])
    assert len(sent) == 1
    assert "Talk:页面A" not in sent[0][1]
    await engine.stop()


async def test_ignore_user_filter(tmp_path):
    sub = GroupSub(group_id="g1", sites=["zh_test"],
                   filters={"zh_test": SiteFilter(ignore_users=["ExampleBot"])})
    engine, store, sent = make_engine(tmp_path, subs=[sub])
    store.set_cursor("zh_test", "2026-01-01T00:00:00Z", 0)
    engine.clients["zh_test"] = FakeClient(
        [[rc(rcid=8, user="ExampleBot"), rc(rcid=9, user="普通人")]]
    )
    await engine._poll_once(engine.cfg.sites["zh_test"])
    assert len(sent) == 1
    await engine.stop()


async def test_watchlist_filter(tmp_path):
    engine, store, sent = make_engine(tmp_path)
    store.set_cursor("zh_test", "2026-01-01T00:00:00Z", 0)
    store.add_watch("g1", "zh_test", "指定页")
    engine.clients["zh_test"] = FakeClient(
        [[rc(rcid=10, title="其他页"), rc(rcid=11, title="指定页")]]
    )
    await engine._poll_once(engine.cfg.sites["zh_test"])
    assert len(sent) == 1
    assert "指定页" in sent[0][1]
    await engine.stop()


async def test_dedup_rcid(tmp_path):
    engine, store, sent = make_engine(tmp_path)
    store.set_cursor("zh_test", "2026-01-01T00:00:00Z", 0)
    client = FakeClient([[rc(rcid=20)], [rc(rcid=20)]])
    engine.clients["zh_test"] = client
    await engine._poll_once(engine.cfg.sites["zh_test"])
    assert len(sent) == 1
    # 游标已推进到 rcid=20，同批内容重复返回也应被 seen 表拦截
    store.set_cursor("zh_test", "2026-01-01T00:00:00Z", 0)  # 重置游标模拟漏推
    await engine._poll_once(engine.cfg.sites["zh_test"])
    assert len(sent) == 1
    await engine.stop()


async def test_digest_on_burst(tmp_path):
    engine, store, sent = make_engine(tmp_path)
    engine.cfg.poll.digest_threshold = 3
    store.set_cursor("zh_test", "2026-01-01T00:00:00Z", 0)
    batch = [rc(rcid=30 + i, title=f"页面{i}") for i in range(5)]
    engine.clients["zh_test"] = FakeClient([batch])
    await engine._poll_once(engine.cfg.sites["zh_test"])
    assert len(sent) == 1
    assert "聚合推送：5 条变更" in sent[0][1]
    await engine.stop()


async def test_route_to_multiple_groups(tmp_path):
    subs = [
        GroupSub(group_id="g1", sites=["zh_test"], filters={}),
        GroupSub(group_id="g2", sites=["zh_test"], filters={}),
    ]
    engine, store, sent = make_engine(tmp_path, subs=subs)
    store.set_cursor("zh_test", "2026-01-01T00:00:00Z", 0)
    engine.clients["zh_test"] = FakeClient([[rc(rcid=40)]])
    await engine._poll_once(engine.cfg.sites["zh_test"])
    assert {g for g, _ in sent} == {"g1", "g2"}
    await engine.stop()


# ---------------------------------------------------------------- v2 行为

async def test_sync_adds_and_removes_runtime(tmp_path):
    """回归缺陷 3：/wiki add 后立即有轮询任务；禁用后任务被移除。"""
    cfg = BotConfig()
    cfg.data_dir = tmp_path / "data"
    store = Store(cfg.data_dir / "t.db")
    store.init()
    site = WikiSite(id="s1", name="S1", api_url="https://x/w/api.php",
                    base_url="https://x", language="zh")
    store.upsert_site(site, source="dynamic")
    sent = []
    engine = MonitorEngine(cfg, store, lambda gid, text, **kw: _record(sent, gid, text))
    engine._make_client = lambda s: FakeClient([])  # type: ignore[assignment]
    await engine.sync()
    assert "s1" in engine._site_tasks
    store.set_site_enabled("s1", False)
    await engine.sync()
    assert "s1" not in engine._site_tasks
    await engine.stop()
    store.close()


async def test_per_group_visual_flag_passed_to_sender(tmp_path):
    """回归缺陷 4：图文开关按群生效，随推送项传递。"""
    from diffbot_core.manage import ManagementService

    cfg = BotConfig()
    cfg.data_dir = tmp_path / "data"
    cfg.push.visual = False
    site = WikiSite(id="s1", name="S1", api_url="https://x/w/api.php",
                    base_url="https://x", language="zh")
    cfg.sites = {"s1": site}
    store = Store(cfg.data_dir / "t2.db")
    store.init()
    store.upsert_site(site, source="yaml")
    store.upsert_sub("g_visual", "s1")
    store.upsert_sub("g_plain", "s1")
    manage = ManagementService(store, cfg)
    manage.set_group_visual("g_visual", "10001", True)

    calls = []

    async def sender(gid, text, image_path=None, visual=None, throttle_per_minute=None):
        calls.append((gid, bool(image_path), visual))

    engine = MonitorEngine(cfg, store, sender, manage=manage)
    client = FakeClient([[
        {"rcid": 1, "pageid": 1, "title": "页面A", "ns": 0, "revid": 100,
         "old_revid": 99, "user": "A", "timestamp": "2026-01-01T00:00:01Z",
         "comment": "", "newlen": 20, "oldlen": 10, "type": "edit"},
    ]])
    engine.clients["s1"] = client
    await engine._poll_once(site, first=False)

    by_group = {gid: visual for gid, _img, visual in calls}
    assert by_group.get("g_visual") is True
    assert by_group.get("g_plain") is False
    await engine.stop()
    store.close()


async def test_yaml_filter_not_overriding_db(tmp_path):
    """回归缺陷 2：同群同站存在 DB 订阅时，以 DB 过滤为准。"""
    cfg = BotConfig()
    cfg.data_dir = tmp_path / "data"
    site = WikiSite(id="s1", name="S1", api_url="https://x/w/api.php",
                    base_url="https://x", language="zh")
    cfg.sites = {"s1": site}
    # YAML 只订阅命名空间 0
    cfg.subscriptions = [GroupSub(group_id="g1", sites=["s1"],
                                  filters={"s1": SiteFilter(namespaces=[0])})]
    store = Store(cfg.data_dir / "t3.db")
    store.init()
    store.upsert_site(site, source="yaml")
    # DB 覆盖为命名空间 1
    store.upsert_sub("g1", "s1", SiteFilter(namespaces=[1]))
    routing = merge_subs(cfg, store)
    assert routing["s1"][0][1].namespaces == [1]
    store.close()


async def test_tombstone_blocks_yaml_seed_in_routing(tmp_path):
    """退订后 YAML 种子不再参与路由。"""
    cfg = BotConfig()
    cfg.data_dir = tmp_path / "data"
    site = WikiSite(id="s1", name="S1", api_url="https://x/w/api.php",
                    base_url="https://x", language="zh")
    cfg.sites = {"s1": site}
    cfg.subscriptions = [GroupSub(group_id="g1", sites=["s1"], filters={})]
    store = Store(cfg.data_dir / "t4.db")
    store.init()
    store.upsert_site(site, source="yaml")
    store.remove_sub("g1", "s1")   # 写 tombstone
    assert merge_subs(cfg, store) == {}
    store.close()
