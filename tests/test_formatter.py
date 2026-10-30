from diffbot_core.config import PushConfig, WikiSite
from diffbot_core.formatter import format_change, format_digest, local_time, diff_url
from diffbot_core.models import Change, DiffInfo, DiffLine


def make_site(**kw):
    base = dict(
        id="zh_test", name="测试维基", api_url="https://test.example/w/api.php",
        base_url="https://test.example", language="zh", timezone="UTC",
    )
    base.update(kw)
    return WikiSite(**base)


def make_change(**kw):
    base = dict(
        site_id="zh_test", rc_id=1, page_id=10, title="项目首页", ns=0,
        revid=123, old_revid=122, user="张三", timestamp="2026-09-09T14:30:25Z",
        comment="更新成员", new_len=500, old_len=344, sizediff=156, type="edit",
    )
    base.update(kw)
    return Change(**base)


def test_format_change_structure():
    site = make_site()
    change = make_change()
    info = DiffInfo(lines=[DiffLine("add", "新增成员: 李四"), DiffLine("del", "移除成员: 赵六")])
    text = format_change(site, change, info, None, PushConfig())
    assert "[ZH] 📝 页面「项目首页」已更新" in text
    assert "张三" in text
    assert "+156" in text
    assert "更新成员" in text
    assert "🟩➕ 新增成员: 李四" in text
    assert "🟥➖ 移除成员: 赵六" in text
    assert "diff=123" in text


def test_format_change_url_quoted():
    site = make_site()
    change = make_change(title="项目 首页")
    url = diff_url(site, change)
    assert "%E9%A1%B9" in url  # 中文 UTF-8 编码
    assert " " not in url


def test_new_page_format():
    site = make_site()
    change = make_change(type="new", old_revid=0, sizediff=100)
    text = format_change(site, change, None, ["第一行", "第二行"], PushConfig())
    assert "[ZH] 🆕 页面「项目首页」已创建" in text
    assert "🟩➕ 第一行" in text
    assert "（新页面）" in text


def test_truncation():
    site = make_site()
    change = make_change()
    info = DiffInfo(lines=[DiffLine("add", f"line{i}") for i in range(20)])
    cfg = PushConfig(max_diff_lines=5)
    text = format_change(site, change, info, None, cfg)
    assert text.count("🟩➕ line") == 5
    assert "其余 15 行" in text


def test_local_time_utc():
    out = local_time("2026-09-09T14:30:25Z", "UTC")
    assert out.startswith("2026-09-09 14:30:25")


def test_local_time_invalid_tz_fallback():
    out = local_time("2026-09-09T14:30:25Z", "Not/AZone")
    assert "2026-09-09" in out


def test_local_time_invalid_ts_passthrough():
    assert local_time("bad-ts", "UTC") == "bad-ts"


def test_format_digest():
    site = make_site()
    changes = [make_change(rc_id=i, title=f"页面{i}") for i in range(3)]
    text = format_digest(site, changes, PushConfig())
    assert "聚合推送：3 条变更" in text
    assert "页面0" in text
