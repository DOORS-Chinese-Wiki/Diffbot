from diffbot_core.config import SiteFilter, WikiSite
from diffbot_core.store import Store


def make_store(tmp_path):
    s = Store(tmp_path / "test.db")
    s.init()
    return s


def test_cursor_roundtrip(tmp_path):
    store = make_store(tmp_path)
    assert store.get_cursor("s1") == ("", 0)
    store.set_cursor("s1", "2026-01-01T00:00:00Z", 42)
    assert store.get_cursor("s1") == ("2026-01-01T00:00:00Z", 42)
    store.close()


def test_seen_dedup_and_prune(tmp_path):
    store = make_store(tmp_path)
    assert store.filter_seen("s", [1, 2, 3]) == [1, 2, 3]
    store.mark_seen("s", [1, 2], keep=5)
    assert store.filter_seen("s", [1, 2, 3]) == [3]
    store.mark_seen("s", list(range(10, 20)), keep=5)
    # 只保留最新 5 条：filter_seen 返回"未见过"的 id
    fresh = store.filter_seen("s", list(range(0, 30)))
    assert len(fresh) == 25
    kept = [x for x in range(0, 30) if x not in set(fresh)]
    assert kept == [15, 16, 17, 18, 19]
    store.close()


def test_subs_crud(tmp_path):
    store = make_store(tmp_path)
    store.upsert_sub("g1", "s1", SiteFilter(namespaces=[0], ignore_users=["Bot"]))
    subs = store.list_subs()
    assert subs[("g1", "s1")].namespaces == [0]
    store.upsert_sub("g1", "s1", SiteFilter(namespaces=[1]))
    assert store.list_subs()[("g1", "s1")].namespaces == [1]
    assert store.remove_sub("g1", "s1")
    assert not store.remove_sub("g1", "s1")
    store.close()


def test_watch_crud(tmp_path):
    store = make_store(tmp_path)
    store.add_watch("g1", "s1", "首页")
    store.add_watch("g1", "s1", "首页")  # 幂等
    assert store.list_watch("g1", "s1") == {"首页"}
    all_w = store.list_watch_all()
    assert all_w[("g1", "s1")] == {"首页"}
    assert store.remove_watch("g1", "s1", "首页")
    assert store.list_watch("g1", "s1") == set()
    store.close()


def test_dynamic_sites(tmp_path):
    store = make_store(tmp_path)
    site = WikiSite(id="my_wiki", name="My", api_url="https://x/w/api.php",
                    base_url="https://x", language="zh")
    store.save_dynamic_site(site)
    loaded = store.get_dynamic_sites()
    assert loaded["my_wiki"].language == "zh"
    cfg_sites = {}
    store.merge_dynamic_sites(cfg_sites)
    assert "my_wiki" in cfg_sites
    store.close()


def test_wal_mode(tmp_path):
    store = make_store(tmp_path)
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    store.close()
