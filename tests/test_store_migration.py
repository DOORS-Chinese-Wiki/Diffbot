"""schema v1 -> v2 迁移测试。"""

import json
import sqlite3

from diffbot_core.store import Store

V1_SCHEMA = """
CREATE TABLE cursor (site TEXT PRIMARY KEY, last_ts TEXT NOT NULL DEFAULT '', last_rc INTEGER NOT NULL DEFAULT 0);
CREATE TABLE seen (site TEXT NOT NULL, rc INTEGER NOT NULL, PRIMARY KEY (site, rc));
CREATE TABLE subs (
    group_id TEXT NOT NULL, site_id TEXT NOT NULL, namespaces TEXT,
    ignore_users TEXT, min_sizediff INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, site_id)
);
CREATE TABLE watch (group_id TEXT NOT NULL, site_id TEXT NOT NULL, title TEXT NOT NULL, PRIMARY KEY (group_id, site_id, title));
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def make_v1(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(V1_SCHEMA)
    conn.execute(
        "INSERT INTO subs(group_id,site_id,namespaces,ignore_users,min_sizediff) VALUES(?,?,?,?,?)",
        ("g1", "s1", json.dumps([0]), json.dumps(["Bot"]), 5),
    )
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('dyn_sites',?)",
        (
            json.dumps(
                {
                    "my_wiki": {
                        "id": "my_wiki", "name": "My", "api_url": "https://x/w/api.php",
                        "base_url": "https://x", "language": "zh", "timezone": "Asia/Shanghai",
                        "icon": "🌐", "enabled": True, "auth_username": "",
                        "auth_password": "", "poll_interval_override": 60,
                        "digest_threshold_override": 3,
                    }
                }
            ),
        ),
    )
    conn.execute("INSERT INTO cursor(site,last_ts,last_rc) VALUES('s1','2026-01-01T00:00:00Z',9)")
    conn.commit()
    conn.close()


def test_migrate_v1_to_v2(tmp_path):
    path = tmp_path / "old.db"
    make_v1(path)
    store = Store(path)
    store.init()

    assert store.schema_version() == "3"
    subs = store.list_subs()
    assert subs[("g1", "s1")].namespaces == [0]
    assert subs[("g1", "s1")].ignore_users == ["Bot"]
    assert subs[("g1", "s1")].min_sizediff == 5

    site = store.site_object("my_wiki")
    assert site is not None
    assert site.language == "zh"
    assert site.poll_interval_override == 60
    assert site.digest_threshold_override == 3
    assert store.get_site("my_wiki")["source"] == "dynamic"

    # 游标与监视数据不丢
    assert store.get_cursor("s1") == ("2026-01-01T00:00:00Z", 9)
    store.close()


def test_migration_idempotent(tmp_path):
    path = tmp_path / "old.db"
    make_v1(path)
    store = Store(path)
    store.init()
    store.init()  # 二次初始化不应重复导入或报错
    assert len(store.list_subs()) == 1
    assert store.site_object("my_wiki") is not None
    store.close()


def test_fresh_db_has_v2_schema(tmp_path):
    store = Store(tmp_path / "new.db")
    store.init()
    assert store.schema_version() == "3"
    assert store.list_sites() == []
    assert store.list_subs() == {}
    store.close()
