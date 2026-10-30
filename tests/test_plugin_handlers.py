"""插件指令层测试：用假 ncatbot + 假 event 直接驱动 _dispatch。

覆盖回归点：
- 缺陷 1：YAML 种子订阅可被 /wiki remove 真正退订
- 缺陷 4：/bot visual 只影响本群
- 缺陷 5：handler 异常必须有回执（不再静默）
- 权限：非管理员被拒绝，群管理员放行
"""

import asyncio
import importlib.util
import logging
import sys
import time
import types
from pathlib import Path

import pytest

from diffbot_core.config import BotConfig, GroupSub, WikiSite
from diffbot_core.manage import ManagementService
from diffbot_core.models import DiffInfo, DiffLine
from diffbot_core.store import Store

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- 假 ncatbot
def _install_fake_ncatbot():
    if "ncatbot.plugin" in sys.modules:
        return
    pkg = types.ModuleType("ncatbot")
    core = types.ModuleType("ncatbot.core")

    class _QQ:
        def on_group_command(self, *names):
            def deco(fn):
                return fn
            return deco

        def on_private_command(self, *names):
            def deco(fn):
                return fn
            return deco

    class _Registrar:
        qq = _QQ()

    core.registrar = _Registrar()
    plugin_mod = types.ModuleType("ncatbot.plugin")

    class NcatBotPlugin:
        pass

    plugin_mod.NcatBotPlugin = NcatBotPlugin
    utils = types.ModuleType("ncatbot.utils")
    utils.get_log = lambda name: logging.getLogger(name)
    pkg.core, pkg.plugin, pkg.utils = core, plugin_mod, utils
    sys.modules["ncatbot"] = pkg
    sys.modules["ncatbot.core"] = core
    sys.modules["ncatbot.plugin"] = plugin_mod
    sys.modules["ncatbot.utils"] = utils


def load_plugin_module():
    _install_fake_ncatbot()
    path = ROOT / "plugins" / "diffbot" / "main.py"
    spec = importlib.util.spec_from_file_location("diffbot_plugin_main", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclass + from __future__ import annotations 需要模块已注册才能解析注解
    sys.modules["diffbot_plugin_main"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- 假基础设施
class FakeSender:
    role = "member"


class FakeData:
    def __init__(self, raw, gid="g1", uid="10001", role="member"):
        self.raw_message = raw
        self.group_id = gid
        self.user_id = uid
        self.sender = FakeSender()
        self.sender.role = role


class FakeEvent:
    def __init__(self, raw, gid="g1", uid="10001", role="member"):
        self.data = FakeData(raw, gid, uid, role)
        self.replies = []

    async def reply(self, text=None, image=None, **kw):
        self.replies.append((text, image))

    @property
    def last_text(self):
        return self.replies[-1][0] if self.replies else ""


class FakeClient:
    async def recentchanges(self, *, limit=5, **kw):
        return []

    async def latest_revisions(self, title, limit=2):
        return [
            {"revid": 1, "user": "A", "timestamp": "2026-01-01T00:00:00Z", "comment": "", "size": 10},
            {"revid": 2, "user": "B", "timestamp": "2026-01-02T00:00:00Z", "comment": "改", "size": 20},
        ]

    async def revisions_around(self, title, revid):
        return await self.latest_revisions(title)

    async def compare(self, a, b, uselang=None):
        return DiffInfo(lines=[DiffLine("add", "新内容")])

    async def page_content(self, revid):
        return "新页面"

    async def close(self):
        pass


class FakeEngine:
    def __init__(self):
        self.stats = {"polls": 0, "pushed": 0, "errors": 0}
        self.clients = {}
        self.sync_calls = 0

    async def sync(self):
        self.sync_calls += 1

    def client_for(self, site):
        return FakeClient()

    async def remove_site(self, site_id):
        self.clients.pop(site_id, None)


# ---------------------------------------------------------------- 夹具
def make_plugin(tmp_path, *, admins=("999999",)):
    main = load_plugin_module()
    cfg = BotConfig()
    cfg.data_dir = tmp_path / "data"
    cfg.admins = list(admins)
    cfg.sites = {
        "doors_en": WikiSite(
            id="doors_en", name="DOORS EN", api_url="https://en.example/w/api.php",
            base_url="https://en.example", language="en",
        ),
        "doors_zh": WikiSite(
            id="doors_zh", name="DOORS ZH", api_url="https://zh.example/w/api.php",
            base_url="https://zh.example", language="zh",
        ),
    }
    cfg.subscriptions = [GroupSub(group_id="g1", sites=["doors_en", "doors_zh"], filters={})]
    store = Store(cfg.data_dir / "test.db")
    store.init()
    manage = ManagementService(store, cfg)
    manage.seed_from_config()

    plugin = main.DiffBotPlugin.__new__(main.DiffBotPlugin)
    plugin.cfg = cfg
    plugin.store = store
    plugin.manage = manage
    plugin.engine = FakeEngine()
    manage.engine = plugin.engine
    plugin.bridge = main.BridgeService(store, cfg, discord=cfg.discord)
    plugin.repost = None
    plugin.started_at = time.time()
    return main, plugin, store, manage


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 测试
def test_wiki_list_shows_ids(tmp_path):
    main, plugin, store, _ = make_plugin(tmp_path)
    event = FakeEvent("/wiki list")
    run(plugin._dispatch("wiki", event))
    assert "doors_en" in event.last_text
    assert "doors_zh" in event.last_text
    store.close()


def test_wiki_remove_really_unsubscribes(tmp_path):
    """缺陷 1 回归：YAML 种子订阅退订后不再路由。"""
    main, plugin, store, manage = make_plugin(tmp_path)
    event = FakeEvent("/wiki remove doors_en", role="admin")
    run(plugin._dispatch("wiki", event))
    assert "已退订" in event.last_text
    assert ("g1", "doors_en") not in store.list_subs()
    assert "doors_en" not in manage.routing()
    # 重启（重新种子）也不复活
    manage.seed_from_config()
    assert "doors_en" not in manage.routing()
    store.close()


def test_wiki_remove_by_name_with_spaces(tmp_path):
    main, plugin, store, _ = make_plugin(tmp_path)
    event = FakeEvent("/wiki remove DOORS ZH", role="owner")
    run(plugin._dispatch("wiki", event))
    assert "已退订 doors_zh" in event.last_text
    store.close()


def test_visual_is_per_group(tmp_path):
    """缺陷 4 回归：/bot visual 只影响本群。"""
    main, plugin, store, manage = make_plugin(tmp_path)
    event = FakeEvent("/bot visual on", gid="g1", role="admin")
    run(plugin._dispatch("bot", event))
    assert manage.get_group_visual("g1") is True
    assert manage.get_group_visual("g2") is False
    # 关闭也只影响本群
    off = FakeEvent("/bot visual off", gid="g1", role="admin")
    run(plugin._dispatch("bot", off))
    assert manage.get_group_visual("g1") is False
    store.close()


def test_permission_denied_for_member(tmp_path):
    main, plugin, store, _ = make_plugin(tmp_path)
    event = FakeEvent("/wiki remove doors_en", role="member")
    run(plugin._dispatch("wiki", event))
    assert "权限" in event.last_text


def test_bridge_list_empty(tmp_path):
    main, plugin, store, _ = make_plugin(tmp_path)
    event = FakeEvent("/bridge list")
    run(plugin._dispatch("bridge", event))
    assert "尚未绑定" in event.last_text or "No Discord" in event.last_text
    store.close()


def test_bridge_add_requires_token(tmp_path):
    main, plugin, store, _ = make_plugin(tmp_path)
    event = FakeEvent("/bridge add 123456789", role="admin")
    run(plugin._dispatch("bridge", event))
    assert "Token" in event.last_text or "token" in event.last_text
    store.close()


def test_bridge_filter_write_requires_admin(tmp_path):
    main, plugin, store, _ = make_plugin(tmp_path)
    from diffbot_core.models import RepostSource

    store.upsert_repost_source(
        RepostSource(
            id="discord_1", platform="discord", channel_id="1", guild_id="g", name="gen"
        )
    )
    store.upsert_repost_binding("g1", "discord_1", None)
    member = FakeEvent("/bridge filter keyword x", role="member")
    run(plugin._dispatch("bridge", member))
    assert "权限" in member.last_text
    admin = FakeEvent("/bridge filter keyword x", role="admin")
    run(plugin._dispatch("bridge", admin))
    assert "过滤" in admin.last_text or "updated" in admin.last_text.lower()
    store.close()


def test_bot_status_includes_bridge(tmp_path):
    main, plugin, store, _ = make_plugin(tmp_path)
    event = FakeEvent("/bot status")
    run(plugin._dispatch("bot", event))
    assert "转载源" in event.last_text or "Repost" in event.last_text
    store.close()


def test_super_admin_from_config(tmp_path):
    main, plugin, store, _ = make_plugin(tmp_path, admins=("10001",))
    event = FakeEvent("/wiki delete doors_zh", uid="10001", role="member")
    run(plugin._dispatch("wiki", event))
    assert "已删除" in event.last_text
    assert store.get_site("doors_zh") is None
    store.close()


def test_unexpected_exception_replies_error(tmp_path):
    """缺陷 5 回归：handler 抛异常必须有回执。"""
    main, plugin, store, manage = make_plugin(tmp_path)

    def boom(*a, **kw):
        raise RuntimeError("boom")

    manage.list_subs = boom  # type: ignore[assignment]
    event = FakeEvent("/wiki list")
    run(plugin._dispatch("wiki", event))
    assert "指令执行失败" in event.last_text
    store.close()


def test_unknown_subcommand_replies_usage(tmp_path):
    main, plugin, store, _ = make_plugin(tmp_path)
    event = FakeEvent("/wiki 乱写")
    run(plugin._dispatch("wiki", event))
    assert "用法" in event.last_text
    store.close()


def test_help_filters_by_permission(tmp_path):
    main, plugin, store, _ = make_plugin(tmp_path)
    member = FakeEvent("/help", role="member")
    run(plugin._dispatch("help", member))
    assert "/wiki" in member.last_text
    assert "/bot" in member.last_text
    # 普通成员看不到仅超管的 /wiki delete 详情
    detail = FakeEvent("/help wiki delete", role="member")
    run(plugin._dispatch("help", detail))
    assert "未找到指令" in detail.last_text
    store.close()


def test_filter_and_watch_commands(tmp_path):
    main, plugin, store, manage = make_plugin(tmp_path)
    run(plugin._dispatch("filter", FakeEvent("/filter ns 0,1 @doors_en", role="admin")))
    assert manage.get_filter("g1", "doors_en").namespaces == [0, 1]
    run(plugin._dispatch("page", FakeEvent("/page watch 主要 页面 @doors_en", role="admin")))
    assert manage.watch_list("g1", "doors_en") == ["主要 页面"]
    listing = FakeEvent("/page list @doors_en")
    run(plugin._dispatch("page", listing))
    assert "主要 页面" in listing.last_text
    store.close()


def test_diff_command_uses_text_when_visual_off(tmp_path):
    main, plugin, store, manage = make_plugin(tmp_path)
    event = FakeEvent("/diff 页面 @doors_en")
    run(plugin._dispatch("diff", event))
    texts = [t for t, _ in event.replies if t]
    assert any("页面" in t for t in texts)
    store.close()


def test_status_command(tmp_path):
    main, plugin, store, _ = make_plugin(tmp_path)
    event = FakeEvent("/bot status")
    run(plugin._dispatch("bot", event))
    assert "运行状态" in event.last_text
    store.close()
