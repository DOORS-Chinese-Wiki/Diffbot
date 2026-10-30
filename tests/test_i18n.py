from diffbot_core.i18n import MESSAGES, get_message


def test_zh_template():
    assert get_message("zh", "page_updated", title="首页") == "📝 页面「首页」已更新"


def test_en_template():
    msg = get_message("en", "page_updated", title="Main_Page")
    assert msg == '📝 Page "Main_Page" has been updated'


def test_fallback_to_en():
    # 未知语言回退 en
    assert "updated" in get_message("fr", "page_updated", title="X")


def test_missing_key_returns_key():
    assert get_message("zh", "nonexistent_key") == "nonexistent_key"


def test_digest_format():
    msg = get_message("zh", "digest_header", n=3)
    assert "3" in msg
