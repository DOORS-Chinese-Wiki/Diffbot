"""Discord 转载：过滤器、格式化、客户端解析、存储与引擎行为。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from diffbot_core.commands import CommandError
from diffbot_core.config import DiscordConfig
from diffbot_core.discord_api import DiscordClient, DiscordError, parse_message
from diffbot_core.manage import BridgeService
from diffbot_core.models import (
    DiscordAttachment,
    DiscordMessage,
    RepostFilter,
    RepostSource,
)
from diffbot_core.repost import RepostEngine, snowflake_now
from diffbot_core.repost_format import format_discord_message
from diffbot_core.store import Store


# ---------------------------------------------------------------------------
# models / filter
# ---------------------------------------------------------------------------


def _msg(**kw) -> DiscordMessage:
    base = dict(
        id="100",
        channel_id="200",
        guild_id="300",
        author_id="u1",
        author_name="Alice",
        content="hello patch notes",
        timestamp="2026-02-14T12:00:00+00:00",
    )
    base.update(kw)
    return DiscordMessage(**base)


def test_filter_keywords():
    flt = RepostFilter(keywords=["patch"])
    assert flt.passes(_msg())
    assert not flt.passes(_msg(content="unrelated"))


def test_filter_ignore_author_and_bot():
    assert not RepostFilter(ignore_bots=True).passes(_msg(is_bot=True))
    assert RepostFilter(ignore_bots=False).passes(_msg(is_bot=True))
    assert not RepostFilter(ignore_authors=["Alice"]).passes(_msg())
    assert not RepostFilter(ignore_authors=["u1"]).passes(_msg())


def test_filter_empty_content_without_image_dropped():
    assert not RepostFilter().passes(_msg(content="   "))
    ok = _msg(
        content="",
        attachments=[
            DiscordAttachment(id="a", filename="x.png", url="https://x", content_type="image/png")
        ],
    )
    assert RepostFilter().passes(ok)


def test_parse_message():
    raw = {
        "id": "42",
        "channel_id": "1",
        "guild_id": "2",
        "content": "hi",
        "timestamp": "2026-02-14T00:00:00+00:00",
        "author": {"id": "9", "username": "bob", "bot": True},
        "attachments": [
            {"id": "a", "filename": "f.png", "url": "https://cdn/f.png", "content_type": "image/png", "size": 10}
        ],
        "embeds": [{}],
    }
    m = parse_message(raw)
    assert m.id == "42" and m.is_bot and m.embeds_count == 1
    assert m.attachments[0].is_image


def test_format_discord_message_zh():
    src = RepostSource(
        id="discord_1", platform="discord", channel_id="1", guild_id="2", name="general"
    )
    text = format_discord_message(src, _msg(), lang="zh")
    assert "Discord" in text
    assert "Alice" in text
    assert "hello patch notes" in text
    assert "discord.com/channels/300/200/100" in text


def test_parse_channel_ref():
    assert BridgeService.parse_channel_ref("12345") == "12345"
    assert (
        BridgeService.parse_channel_ref("https://discord.com/channels/1/999")
        == "999"
    )
    with pytest.raises(CommandError):
        BridgeService.parse_channel_ref("not-a-channel")


def test_parse_source_ref_twitter():
    assert BridgeService.parse_source_ref("@SomeUser") == ("twitter", "SomeUser")
    assert BridgeService.parse_source_ref("https://x.com/SomeUser") == (
        "twitter",
        "SomeUser",
    )
    assert BridgeService.parse_source_ref("https://twitter.com/SomeUser/status/1") == (
        "twitter",
        "SomeUser",
    )
    assert BridgeService.parse_source_ref("999") == ("discord", "999")
    # 显示名带空格不是合法 handle
    with pytest.raises(CommandError):
        BridgeService.parse_source_ref("@Mayer Goring")
    # 任意 RSS URL
    platform, url = BridgeService.parse_source_ref("https://example.com/feed.xml")
    assert platform == "rss"
    assert url == "https://example.com/feed.xml"


def test_parse_rss_feed_rss2():
    from diffbot_core.rss_api import parse_feed

    body = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item>
        <title>hello</title>
        <description>&lt;p&gt;tweet body&lt;/p&gt;&lt;img src="https://pbs.example/img.jpg"/&gt;</description>
        <link>https://x.com/foo/status/12345</link>
        <pubDate>Mon, 14 Feb 2026 12:00:00 GMT</pubDate>
      </item>
    </channel></rss>
    """
    msgs = parse_feed(body, channel_id="foo", author_name="@foo")
    assert len(msgs) == 1
    assert msgs[0].id == "12345"
    assert "tweet body" in msgs[0].content
    assert msgs[0].attachments and msgs[0].attachments[0].is_image


def test_format_twitter_message():
    src = RepostSource(
        id="twitter_foo",
        platform="twitter",
        channel_id="foo",
        guild_id="",
        name="@foo",
    )
    text = format_discord_message(src, _msg(id="99", content="hi"), lang="zh")
    assert "Twitter" in text
    assert "x.com/foo/status/99" in text



# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "t.db")
    s.init()
    yield s
    s.close()


def test_schema_version_3(store: Store):
    assert store.schema_version() == "3"


def test_repost_source_crud(store: Store):
    src = RepostSource(
        id="discord_1", platform="discord", channel_id="1", guild_id="g", name="gen"
    )
    store.upsert_repost_source(src, added_by="u")
    assert store.get_repost_source("discord_1") is not None
    assert store.get_repost_source_by_channel("discord", "1") is not None
    store.set_repost_source_flags("discord_1", available=False)
    assert store.repost_source_object("discord_1").available is False
    store.delete_repost_source("discord_1")
    assert store.get_repost_source("discord_1") is None


def test_repost_binding_and_routing(store: Store):
    src = RepostSource(
        id="discord_1", platform="discord", channel_id="1", guild_id="g", name="gen"
    )
    store.upsert_repost_source(src)
    flt = RepostFilter(keywords=["x"], ignore_authors=["bob"], ignore_bots=False)
    store.upsert_repost_binding("123", "discord_1", flt)
    got = store.list_repost_bindings("123")[("123", "discord_1")]
    assert got.keywords == ["x"]
    assert got.ignore_authors == ["bob"]
    assert got.ignore_bots is False
    routing = store.repost_routing()
    assert routing["discord_1"][0][0] == "123"

    store.remove_repost_binding("123", "discord_1")
    assert store.has_repost_tombstone("123", "discord_1")
    assert ("123", "discord_1") not in store.list_repost_bindings("123")


def test_repost_cursor_and_seen(store: Store):
    assert store.get_repost_cursor("s") == ""
    store.set_repost_cursor("s", "999")
    assert store.get_repost_cursor("s") == "999"
    assert store.filter_repost_seen("s", ["1", "2"]) == ["1", "2"]
    store.mark_repost_seen("s", ["1", "2"])
    assert store.filter_repost_seen("s", ["1", "2", "3"]) == ["3"]


# ---------------------------------------------------------------------------
# DiscordClient
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status=200, data=None, text=""):
        self.status = status
        self._data = data
        self._text = text

    async def json(self, content_type=None):
        return self._data

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.closed = False

    def request(self, method, url, params=None, headers=None):
        self.calls.append((method, url, params, headers))
        item = self._responses.pop(0)
        return item

    async def close(self):
        self.closed = True


def test_discord_get_channel_rejects_voice():
    sess = FakeSession([_Resp(200, {"id": "1", "type": 2})])
    client = DiscordClient("tok", session=sess)

    async def run():
        with pytest.raises(DiscordError):
            await client.get_channel("1")

    asyncio.run(run())


def test_discord_list_messages_sorted():
    raw = [
        {"id": "20", "channel_id": "1", "content": "b", "timestamp": "2026-02-14T00:00:02+00:00", "author": {"id": "u"}},
        {"id": "10", "channel_id": "1", "content": "a", "timestamp": "2026-02-14T00:00:01+00:00", "author": {"id": "u"}},
    ]
    sess = FakeSession([_Resp(200, raw)])
    client = DiscordClient("tok", session=sess)

    async def run():
        msgs = await client.list_messages("1", after="5")
        return msgs

    msgs = asyncio.run(run())
    assert [m.id for m in msgs] == ["10", "20"]
    assert sess.calls[0][2]["after"] == "5"


def test_discord_403_unavailable():
    sess = FakeSession([_Resp(403, text="forbidden")])
    client = DiscordClient("tok", session=sess, retries=1)

    async def run():
        with pytest.raises(DiscordError) as ei:
            await client.get_channel("1")
        return ei.value

    err = asyncio.run(run())
    assert err.unavailable


# ---------------------------------------------------------------------------
# RepostEngine
# ---------------------------------------------------------------------------


class FakeDiscordClient:
    def __init__(self, batches):
        self._batches = list(batches)
        self.download_calls = []

    async def list_messages(self, channel_id, after=None, limit=50):
        if not self._batches:
            return []
        return self._batches.pop(0)

    async def download_image(self, url, dest, max_bytes=0):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"img")
        self.download_calls.append(url)
        return dest

    async def close(self):
        pass


def test_engine_first_poll_no_replay(tmp_path: Path):
    store = Store(tmp_path / "t.db")
    store.init()
    src = RepostSource(
        id="discord_1", platform="discord", channel_id="1", guild_id="g", name="gen"
    )
    store.upsert_repost_source(src)
    store.upsert_repost_binding("123", "discord_1", RepostFilter(ignore_bots=False))

    sent = []

    async def sender(gid, text, image_path=None, visual=None):
        sent.append((gid, text, image_path, visual))

    engine = RepostEngine(
        store, sender, DiscordConfig(token="t", poll_interval=0.01), tmp_path
    )
    fake = FakeDiscordClient(
        [
            [_msg(id="10"), _msg(id="11")],  # first: init only
            [_msg(id="12", content="new post")],  # second: deliver
        ]
    )
    engine._clients["discord_1"] = fake

    async def run():
        await engine._poll_once(src, first=True)
        assert sent == []
        assert store.get_repost_cursor("discord_1") == "11"
        await engine._poll_once(src, first=False)
        assert len(sent) == 1
        assert "new post" in sent[0][1]
        # 转载强制 visual=False，避免群图文模式吞掉正文
        assert sent[0][3] is False
        assert store.get_repost_cursor("discord_1") == "12"

    asyncio.run(run())
    store.close()


def test_engine_empty_channel_uses_now_cursor(tmp_path: Path):
    store = Store(tmp_path / "t.db")
    store.init()
    src = RepostSource(
        id="discord_1", platform="discord", channel_id="1", guild_id="g", name="gen"
    )
    store.upsert_repost_source(src)
    engine = RepostEngine(
        store,
        None,  # type: ignore[arg-type]
        DiscordConfig(token="t"),
        tmp_path,
    )
    engine._clients["discord_1"] = FakeDiscordClient([[]])

    async def run():
        await engine._poll_once(src, first=True)

    asyncio.run(run())
    cursor = store.get_repost_cursor("discord_1")
    assert cursor.isdigit() and int(cursor) > 0
    # snowflake 精度为 1ms，允许跨毫秒边界
    assert abs(int(cursor) - int(snowflake_now())) <= (1 << 22) * 2
    store.close()


def test_bridge_service_add_requires_token(tmp_path: Path):
    store = Store(tmp_path / "t.db")
    store.init()
    cfg = DiscordConfig(token="")
    svc = BridgeService(store, cfg, discord=cfg)

    async def run():
        with pytest.raises(CommandError):
            await svc.add_binding("1", "u", "12345")

    asyncio.run(run())
    store.close()


def test_discord_429_retries(tmp_path: Path):
    sess = FakeSession(
        [
            _Resp(429, {"retry_after": 0.01}),
            _Resp(200, {"id": "1", "type": 0, "name": "general", "guild_id": "g"}),
        ]
    )
    client = DiscordClient("tok", session=sess)

    async def run():
        meta = await client.get_channel("1")
        return meta

    meta = asyncio.run(run())
    assert meta["id"] == "1"
    assert len(sess.calls) == 2


def test_download_image_too_large(tmp_path: Path):
    class BigResp:
        status = 200

        class content:
            @staticmethod
            async def iter_chunked(n):
                yield b"x" * 100

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class BigSession:
        def get(self, url, headers=None):
            return BigResp()

        async def close(self):
            pass

    client = DiscordClient("tok", session=BigSession())  # type: ignore[arg-type]

    async def run():
        dest = tmp_path / "big.png"
        path = await client.download_image("https://x/big.png", dest, max_bytes=10)
        return path

    assert asyncio.run(run()) is None


def test_format_discord_message_en():
    src = RepostSource(
        id="discord_1", platform="discord", channel_id="1", guild_id="2", name="general"
    )
    text = format_discord_message(src, _msg(), lang="en")
    assert "Author" in text
    assert "Content" in text


def test_bridge_service_enable_restores_available(tmp_path: Path):
    store = Store(tmp_path / "t.db")
    store.init()
    src = RepostSource(
        id="discord_1", platform="discord", channel_id="1", guild_id="g", name="gen",
        enabled=True, available=False,
    )
    store.upsert_repost_source(src)
    store.upsert_repost_binding("g1", "discord_1", None)
    store.set_repost_source_flags("discord_1", available=False)

    class FakeEngine:
        async def sync(self):
            pass

    cfg = DiscordConfig(token="t")
    svc = BridgeService(store, cfg, discord=cfg, engine=FakeEngine())

    async def run():
        await svc.set_binding_active("g1", "u", "discord_1", True)

    asyncio.run(run())
    assert store.repost_source_object("discord_1").available is True
    audit = store.list_audit(5)
    assert any(r["action"] == "bridge_enable" for r in audit)
    store.close()


def test_bridge_filter_keyword_writes_audit(tmp_path: Path):
    store = Store(tmp_path / "t.db")
    store.init()
    src = RepostSource(
        id="discord_1", platform="discord", channel_id="1", guild_id="g", name="gen"
    )
    store.upsert_repost_source(src)
    store.upsert_repost_binding("g1", "discord_1", None)
    cfg = DiscordConfig(token="t")
    svc = BridgeService(store, cfg, discord=cfg)
    svc.keyword_add("g1", "admin", "discord_1", "patch")
    flt = store.list_repost_bindings("g1")[("g1", "discord_1")]
    assert flt.keywords == ["patch"]
    assert any(r["action"] == "bridge_filter" for r in store.list_audit(5))
    store.close()


class FakeRssClient:
    def __init__(self, batches):
        self._batches = list(batches)

    async def fetch_feed(self, url, *, channel_id, author_name):
        if not self._batches:
            return []
        return self._batches.pop(0)

    async def download_image(self, url, dest, max_bytes=0):
        return None

    async def close(self):
        pass


class FakeTwitterClient:
    def __init__(self, batches):
        self._batches = list(batches)

    async def list_tweets(self, screen_name, after=None, limit=20):
        if not self._batches:
            return []
        return self._batches.pop(0)

    async def download_image(self, url, dest, max_bytes=0):
        return None

    async def close(self):
        pass


def test_engine_twitter_first_poll_no_replay(tmp_path: Path):
    from diffbot_core.config import RsshubConfig

    store = Store(tmp_path / "t.db")
    store.init()
    src = RepostSource(
        id="twitter_foo",
        platform="twitter",
        channel_id="foo",
        guild_id="",
        name="@foo",
    )
    store.upsert_repost_source(src)
    store.upsert_repost_binding("g1", "twitter_foo", RepostFilter(ignore_bots=False))

    sent = []

    async def sender(gid, text, image_path=None, visual=None):
        sent.append(text)

    engine = RepostEngine(
        store,
        sender,
        DiscordConfig(token=""),
        tmp_path,
        rsshub_cfg=RsshubConfig(poll_interval=0.01),
    )
    engine._clients["twitter_foo"] = FakeTwitterClient(
        [
            [_msg(id="10", channel_id="foo"), _msg(id="11", channel_id="foo")],
            [_msg(id="12", channel_id="foo", content="new tweet")],
        ]
    )

    async def run():
        await engine._poll_once(src, first=True)
        assert sent == []
        await engine._poll_once(src, first=False)
        assert len(sent) == 1
        assert "new tweet" in sent[0]

    asyncio.run(run())
    store.close()


