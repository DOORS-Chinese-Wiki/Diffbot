"""指令解析器与帮助生成测试。"""

import pytest

from diffbot_core.commands import (
    ArgSpec,
    CommandError,
    CommandSpec,
    FlagSpec,
    Level,
    find_spec,
    parse_args,
    render_help,
    tokenize,
    usage_line,
)


def spec():
    return CommandSpec(
        name="diff",
        summary="查询差异",
        args=(ArgSpec("title", greedy=True),),
        flags=(FlagSpec("site"), FlagSpec("interval", type=float)),
    )


def test_tokenize_quotes_and_spaces():
    assert tokenize('/wiki add "My Site" --lang zh') == [
        "/wiki", "add", "My Site", "--lang", "zh",
    ]
    assert tokenize("  /diff   页面   名称 ") == ["/diff", "页面", "名称"]


def test_tokenize_fullwidth_quotes():
    assert tokenize("/wiki add “My Site”") == ["/wiki", "add", "My Site"]


def test_tokenize_unbalanced_quote_raises():
    with pytest.raises(CommandError):
        tokenize('/wiki add "unterminated')


def test_parse_greedy_title_with_spaces():
    parsed = parse_args("/diff 主要 页面 名称", spec())
    assert parsed.get("title") == "主要 页面 名称"


def test_parse_flags_and_site_disambiguation():
    lookup = lambda token: token if token.startswith("doors") else None
    parsed = parse_args(
        "/diff 标题 @doors_en --site doors_zh --interval 45", spec(), site_lookup=lookup
    )
    assert parsed.site_query == "doors_en"
    assert parsed.flag("site") == "doors_zh"
    assert parsed.flag("interval") == 45.0


def test_unknown_site_at_token_is_text():
    parsed = parse_args("/diff @未知站点 页面", spec(), site_lookup=lambda t: None)
    assert parsed.site_query is None
    assert parsed.get("title") == "@未知站点 页面"


def test_unknown_flag_raises_with_usage():
    with pytest.raises(CommandError) as ei:
        parse_args("/diff 标题 --nope 1", spec())
    assert "--nope" in ei.value.user_message


def test_missing_required_arg():
    s = CommandSpec(name="page", summary="x", args=(ArgSpec("title"),))
    with pytest.raises(CommandError) as ei:
        parse_args("/page", s)
    assert "title" in ei.value.user_message


def test_extra_tokens_rejected_unless_allowed():
    s = CommandSpec(name="page", summary="x", args=(ArgSpec("title"),))
    with pytest.raises(CommandError):
        parse_args("/page a b", s)
    ok = CommandSpec(name="page", summary="x", args=(ArgSpec("title"),), allow_extra=True)
    assert parse_args("/page a b", ok).get("title") == "a"


def test_flag_type_error():
    with pytest.raises(CommandError) as ei:
        parse_args("/diff 标题 --interval abc", spec())
    assert "interval" in ei.value.user_message


def test_usage_line_renders_optional_and_flags():
    s = CommandSpec(
        name="recent",
        summary="x",
        args=(ArgSpec("limit", required=False, type=int),),
        flags=(FlagSpec("site"), FlagSpec("verbose", type=bool)),
    )
    line = usage_line(s)
    assert "/recent [<limit>]" in line
    assert "[--site <值>]" in line
    assert "[--verbose]" in line


def test_render_help_filters_by_level():
    specs = (
        CommandSpec(name="diff", summary="查询", level=Level.USER),
        CommandSpec(name="secret", summary="隐藏", level=Level.SUPER_ADMIN),
    )
    user_help = render_help(specs, Level.USER)
    assert "/diff" in user_help
    assert "/secret" not in user_help
    admin_help = render_help(specs, Level.SUPER_ADMIN)
    assert "/secret" in admin_help


def test_render_help_detail():
    s = CommandSpec(
        name="wiki", summary="站点管理", args=(ArgSpec("sub", required=False),),
        examples=("list",),
    )
    text = render_help((s,), Level.USER, name="wiki")
    assert "站点管理" in text
    assert "示例" in text
    assert "用法" in text


def test_find_spec_by_alias():
    s = CommandSpec(name="bot", summary="x", aliases=("status",))
    assert find_spec((s,), "status") is s
    assert find_spec((s,), "nope") is None
