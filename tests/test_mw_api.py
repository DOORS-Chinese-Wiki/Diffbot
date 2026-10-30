import pytest

from diffbot_core.mw_api import (
    MediaWikiClient,
    parse_inline_diff,
    parse_table_diff,
    parse_unified_diff,
    change_from_rc,
)
from diffbot_core.models import DiffInfo


INLINE_HTML = (
    '<div class="mw-diff-inline-header">←旧版本</div>'
    '<div class="mw-diff-inline-context">上下文行</div>'
    '<div class="mw-diff-inline-deleted">删除的 &amp; 行</div>'
    '<div class="mw-diff-inline-added">新增的行<br>第二行</div>'
)


def test_parse_inline_diff():
    lines = parse_inline_diff(INLINE_HTML)
    kinds = [(l.kind, l.text) for l in lines]
    assert ("ctx", "上下文行") in kinds
    assert ("del", "删除的 & 行") in kinds  # 实体已解码
    assert ("add", "新增的行\n第二行") in kinds
    assert all(k != "header" for k, _ in kinds)


def test_parse_inline_changed():
    html = (
        '<div class="mw-diff-inline-changed">'
        "<del>旧文本</del><ins>新文本</ins></div>"
    )
    lines = parse_inline_diff(html)
    kinds = {(l.kind, l.text) for l in lines}
    assert ("del", "旧文本") in kinds
    assert ("add", "新文本") in kinds


def test_parse_unified_diff():
    body = "@@ -1,2 +1,2 @@\n context\n-old\n+new\n"
    lines = parse_unified_diff(body)
    assert [(l.kind, l.text) for l in lines] == [("ctx", "context"), ("del", "old"), ("add", "new")]


# MediaWiki 默认表格格式（THBWiki/维基媒体等实际返回的格式）
TABLE_HTML = (
    '<tr><td colspan="2" class="diff-lineno">第5行：</td>'
    '<td colspan="2" class="diff-lineno">第5行：</td></tr>'
    '<tr><td class="diff-marker"></td><td class="diff-context diff-side-deleted"><div>| 上下文</div></td>'
    '<td class="diff-marker"></td><td class="diff-context diff-side-added"><div>| 上下文</div></td></tr>'
    '<tr><td class="diff-marker" data-marker="−"></td><td class="diff-deletedline"><div>旧的 &amp; 行</div></td>'
    '<td class="diff-marker" data-marker="+"></td><td class="diff-addedline"><div>新的行</div></td></tr>'
)


def test_parse_table_diff():
    lines = parse_table_diff(TABLE_HTML)
    kinds = [(l.kind, l.text) for l in lines]
    assert ("del", "旧的 & 行") in kinds
    assert ("add", "新的行") in kinds
    # 上下文行与行号行不产生 add/del
    assert all("上下文" not in t for _, t in kinds)


class FakeClient(MediaWikiClient):
    """脚本化响应的测试客户端。"""

    def __init__(self, responses):
        super().__init__("https://test.example/w/api.php")
        self._responses = list(responses)
        self.calls = []

    async def _get(self, params):
        self.calls.append(params)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


async def test_compare_inline_then_unified_fallback():
    client = FakeClient([
        {"compare": {"body": "", "fromrevid": 1, "torevid": 2}},   # inline 为空
        {"compare": {"body": "-old\n+new\n"}},                    # unified 兜底
    ])
    info = await client.compare(1, 2)
    assert isinstance(info, DiffInfo)
    assert [(l.kind, l.text) for l in info.lines] == [("del", "old"), ("add", "new")]
    assert info.torevid == 2
    assert client.calls[0]["difftype"] == "inline"
    assert client.calls[1]["difftype"] == "unified"


async def test_compare_table_fallback_same_body():
    """站点忽略 difftype 返回默认表格格式时，同一 body 内解析成功，不发第二次请求。"""
    client = FakeClient([
        {"compare": {"body": TABLE_HTML, "fromrevid": 1, "torevid": 2}},
    ])
    info = await client.compare(1, 2)
    assert [(l.kind, l.text) for l in info.lines] == [("del", "旧的 & 行"), ("add", "新的行")]
    assert len(client.calls) == 1  # 无需 unified 兜底请求


async def test_recentchanges_params():
    client = FakeClient([{"query": {"recentchanges": [{"title": "A", "rcid": 5}]}}])
    raw = await client.recentchanges(newer_than="2026-01-01T00:00:00Z", limit=10)
    assert raw[0]["title"] == "A"
    p = client.calls[0]
    assert p["rcdir"] == "newer" and p["rcstart"] == "2026-01-01T00:00:00Z"
    assert p["rctype"] == "edit|new"


async def test_recentchanges_older_default():
    client = FakeClient([{"query": {"recentchanges": []}}])
    await client.recentchanges(limit=5)
    assert client.calls[0]["rcdir"] == "older"


def test_change_from_rc_new_page():
    c = change_from_rc("s", {"rcid": 1, "title": "T", "ns": 0, "revid": 9,
                             "old_revid": 0, "user": "u", "timestamp": "2026-01-01T00:00:00Z",
                             "newlen": 100, "oldlen": 0})
    assert c.type == "new" and c.sizediff == 100


def test_change_from_rc_edit():
    c = change_from_rc("s", {"rcid": 2, "title": "T", "ns": 1, "revid": 10,
                             "old_revid": 9, "user": "u", "timestamp": "2026-01-01T00:00:00Z",
                             "newlen": 90, "oldlen": 100, "flags": "minor"})
    assert c.type == "edit" and c.sizediff == -10 and c.minor
