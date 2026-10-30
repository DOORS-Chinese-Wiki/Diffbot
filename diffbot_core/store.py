"""SQLite 持久层：游标、已见变更、动态订阅、页面监视。

- WAL 模式（容器/并发友好）
- 操作均为亚毫秒级小事务，直接同步执行即可
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

from .config import SiteFilter, WikiSite
from .models import RepostFilter, RepostSource

LOG = logging.getLogger("diffbot.store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cursor (
    site TEXT PRIMARY KEY,
    last_ts TEXT NOT NULL DEFAULT '',
    last_rc INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS seen (
    site TEXT NOT NULL,
    rc INTEGER NOT NULL,
    PRIMARY KEY (site, rc)
);
CREATE TABLE IF NOT EXISTS subs (
    group_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    namespaces TEXT,
    ignore_users TEXT,
    min_sizediff INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, site_id)
);
CREATE TABLE IF NOT EXISTS watch (
    group_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    title TEXT NOT NULL,
    PRIMARY KEY (group_id, site_id, title)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- ===== schema v2（唯一数据源）=====
CREATE TABLE IF NOT EXISTS sites (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    api_url TEXT NOT NULL UNIQUE,
    base_url TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'en',
    timezone TEXT NOT NULL DEFAULT 'UTC',
    icon TEXT NOT NULL DEFAULT '🌐',
    enabled INTEGER NOT NULL DEFAULT 1,
    auth_username TEXT NOT NULL DEFAULT '',
    auth_password TEXT NOT NULL DEFAULT '',
    poll_interval REAL,
    digest_threshold INTEGER,
    source TEXT NOT NULL DEFAULT 'dynamic',
    added_by TEXT NOT NULL DEFAULT '',
    added_at INTEGER
);
CREATE TABLE IF NOT EXISTS subscriptions (
    group_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    namespaces TEXT,
    ignore_users TEXT,
    min_sizediff INTEGER NOT NULL DEFAULT 0,
    origin TEXT NOT NULL DEFAULT 'command',
    PRIMARY KEY (group_id, site_id)
);
CREATE TABLE IF NOT EXISTS subs_tombstone (
    group_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    PRIMARY KEY (group_id, site_id)
);
CREATE TABLE IF NOT EXISTS group_settings (
    group_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (group_id, key)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    group_id TEXT,
    user_id TEXT,
    action TEXT NOT NULL,
    detail TEXT
);
-- ===== schema v3（海外平台转载）=====
CREATE TABLE IF NOT EXISTS repost_sources (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL DEFAULT 'discord',
    channel_id TEXT NOT NULL,
    guild_id TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    available INTEGER NOT NULL DEFAULT 1,
    added_by TEXT NOT NULL DEFAULT '',
    added_at INTEGER,
    UNIQUE(platform, channel_id)
);
CREATE TABLE IF NOT EXISTS repost_bindings (
    group_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    keywords TEXT,
    ignore_authors TEXT,
    ignore_bots INTEGER NOT NULL DEFAULT 1,
    origin TEXT NOT NULL DEFAULT 'command',
    PRIMARY KEY (group_id, source_id)
);
CREATE TABLE IF NOT EXISTS repost_tombstone (
    group_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    PRIMARY KEY (group_id, source_id)
);
CREATE TABLE IF NOT EXISTS repost_cursor (
    source_id TEXT PRIMARY KEY,
    last_msg_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS repost_seen (
    source_id TEXT NOT NULL,
    msg_id TEXT NOT NULL,
    PRIMARY KEY (source_id, msg_id)
);
"""

SEEN_KEEP = 8000  # 每站点保留的已见 rc_id 上限


class Store:
    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")

    def init(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        self.migrate_legacy()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------- 游标 ----------------

    def get_cursor(self, site_id: str) -> tuple[str, int]:
        row = self._conn.execute(
            "SELECT last_ts, last_rc FROM cursor WHERE site=?", (site_id,)
        ).fetchone()
        return (row["last_ts"], row["last_rc"]) if row else ("", 0)

    def set_cursor(self, site_id: str, ts: str, rc_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO cursor(site,last_ts,last_rc) VALUES(?,?,?) "
                "ON CONFLICT(site) DO UPDATE SET last_ts=excluded.last_ts, last_rc=excluded.last_rc",
                (site_id, ts, rc_id),
            )
            self._conn.commit()

    # ---------------- 已见变更去重 ----------------

    def filter_seen(self, site_id: str, rc_ids: list[int]) -> list[int]:
        if not rc_ids:
            return []
        q = ",".join("?" * len(rc_ids))
        rows = self._conn.execute(
            f"SELECT rc FROM seen WHERE site=? AND rc IN ({q})",
            [site_id, *rc_ids],
        ).fetchall()
        seen = {r["rc"] for r in rows}
        return [x for x in rc_ids if x not in seen]

    def mark_seen(self, site_id: str, rc_ids: list[int], keep: int = SEEN_KEEP) -> None:
        if not rc_ids:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO seen(site,rc) VALUES(?,?)",
                [(site_id, x) for x in rc_ids],
            )
            # 注意：不能用 DELETE ... NOT IN (SELECT ... LIMIT)——
            # SQLite 删除过程中子查询会随表变化重新求值，导致全表误删。
            # 先取第 keep 大的 rc 作为阈值，再按阈值删除。
            row = self._conn.execute(
                "SELECT rc FROM seen WHERE site=? ORDER BY rc DESC LIMIT 1 OFFSET ?",
                (site_id, keep - 1),
            ).fetchone()
            if row:
                self._conn.execute(
                    "DELETE FROM seen WHERE site=? AND rc < ?",
                    (site_id, row["rc"]),
                )
            self._conn.commit()

    # ---------------- 动态订阅 ----------------

    def upsert_sub(
        self,
        group_id: str,
        site_id: str,
        flt: SiteFilter | None = None,
        *,
        origin: str = "command",
    ) -> None:
        """新增/更新订阅；同时清除 tombstone（重新订阅视为有意为之）。"""
        flt = flt or SiteFilter()
        with self._lock:
            self._conn.execute(
                "INSERT INTO subscriptions"
                "(group_id,site_id,active,namespaces,ignore_users,min_sizediff,origin) "
                "VALUES(?,?,1,?,?,?,?) ON CONFLICT(group_id,site_id) DO UPDATE SET "
                "active=1, namespaces=excluded.namespaces, ignore_users=excluded.ignore_users, "
                "min_sizediff=excluded.min_sizediff, origin=excluded.origin",
                (
                    group_id,
                    site_id,
                    json.dumps(flt.namespaces),
                    json.dumps(flt.ignore_users),
                    flt.min_sizediff,
                    origin,
                ),
            )
            self._conn.execute(
                "DELETE FROM subs_tombstone WHERE group_id=? AND site_id=?",
                (group_id, site_id),
            )
            self._conn.commit()

    def remove_sub(self, group_id: str, site_id: str) -> bool:
        """退订：删除订阅行并写 tombstone，防止 YAML 种子把它复活。"""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM subscriptions WHERE group_id=? AND site_id=?",
                (group_id, site_id),
            )
            existed = cur.rowcount > 0
            self._conn.execute(
                "INSERT INTO subs_tombstone(group_id,site_id,ts) VALUES(?,?,?) "
                "ON CONFLICT(group_id,site_id) DO UPDATE SET ts=excluded.ts",
                (group_id, site_id, int(time.time())),
            )
            self._conn.commit()
        return existed

    def has_tombstone(self, group_id: str, site_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM subs_tombstone WHERE group_id=? AND site_id=?",
            (group_id, site_id),
        ).fetchone()
        return row is not None

    def list_subs(
        self, group_id: str | None = None, *, active_only: bool = True
    ) -> dict[tuple[str, str], SiteFilter]:
        sql = "SELECT * FROM subscriptions"
        args: list[object] = []
        clauses = []
        if group_id is not None:
            clauses.append("group_id=?")
            args.append(group_id)
        if active_only:
            clauses.append("active=1")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        rows = self._conn.execute(sql, args).fetchall()
        out: dict[tuple[str, str], SiteFilter] = {}
        for r in rows:
            ns = json.loads(r["namespaces"]) if r["namespaces"] else None
            iu = json.loads(r["ignore_users"]) if r["ignore_users"] else []
            out[(r["group_id"], r["site_id"])] = SiteFilter(
                namespaces=ns, ignore_users=iu, min_sizediff=r["min_sizediff"]
            )
        return out

    # ---------------- 页面监视（watchlist）----------------

    def add_watch(self, group_id: str, site_id: str, title: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO watch(group_id,site_id,title) VALUES(?,?,?)",
                (group_id, site_id, title),
            )
            self._conn.commit()

    def remove_watch(self, group_id: str, site_id: str, title: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM watch WHERE group_id=? AND site_id=? AND title=?",
                (group_id, site_id, title),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def list_watch(self, group_id: str, site_id: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT title FROM watch WHERE group_id=? AND site_id=?",
            (group_id, site_id),
        ).fetchall()
        return {r["title"] for r in rows}

    def list_watch_all(self) -> dict[tuple[str, str], set[str]]:
        rows = self._conn.execute("SELECT group_id, site_id, title FROM watch").fetchall()
        out: dict[tuple[str, str], set[str]] = {}
        for r in rows:
            out.setdefault((r["group_id"], r["site_id"]), set()).add(r["title"])
        return out

    # ---------------- 动态站点（/wiki add 添加）----------------

    def get_meta(self, key: str) -> str | None:
        """读取 meta 表的通用键值。"""
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        """写入/更新 meta 表的通用键值。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()

    def get_dynamic_sites(self) -> dict[str, WikiSite]:
        return {
            row["id"]: self._site_from_row(row)
            for row in self.list_sites()
            if row["source"] == "dynamic"
        }

    def save_dynamic_site(self, site: WikiSite) -> None:
        self.upsert_site(site, source="dynamic")

    def merge_dynamic_sites(self, cfg_sites: dict[str, WikiSite]) -> None:
        for sid, site in self.get_dynamic_sites().items():
            cfg_sites.setdefault(sid, site)


    # ---------------- v2：站点表 ----------------

    @staticmethod
    def _site_from_row(row: sqlite3.Row) -> WikiSite:
        return WikiSite(
            id=row["id"],
            name=row["name"],
            api_url=row["api_url"],
            base_url=row["base_url"],
            language=row["language"],
            timezone=row["timezone"],
            icon=row["icon"],
            enabled=bool(row["enabled"]),
            auth_username=row["auth_username"],
            auth_password=row["auth_password"],
            poll_interval_override=row["poll_interval"],
            digest_threshold_override=row["digest_threshold"],
        )

    def list_sites(self, *, enabled_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM sites"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY id"
        return self._conn.execute(sql).fetchall()

    def get_site(self, site_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM sites WHERE id=?", (site_id,)
        ).fetchone()

    def get_site_by_api(self, api_url: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM sites WHERE api_url=?", (api_url,)
        ).fetchone()

    def site_object(self, site_id: str) -> WikiSite | None:
        row = self.get_site(site_id)
        return self._site_from_row(row) if row else None

    def upsert_site(
        self, site: WikiSite, *, source: str = "dynamic", added_by: str = ""
    ) -> None:
        """写入/更新站点。api_url 冲突（属于其它 id）时抛 ValueError。"""
        existing = self.get_site_by_api(site.api_url)
        if existing is not None and existing["id"] != site.id:
            raise ValueError(
                f"API 地址已被站点 {existing['id']} 使用：{site.api_url}"
            )
        with self._lock:
            self._conn.execute(
                "INSERT INTO sites"
                "(id,name,api_url,base_url,language,timezone,icon,enabled,auth_username,"
                "auth_password,poll_interval,digest_threshold,source,added_by,added_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, api_url=excluded.api_url, "
                "base_url=excluded.base_url, language=excluded.language, timezone=excluded.timezone, "
                "icon=excluded.icon, enabled=excluded.enabled, "
                "auth_username=excluded.auth_username, auth_password=excluded.auth_password, "
                "poll_interval=excluded.poll_interval, digest_threshold=excluded.digest_threshold",
                (
                    site.id,
                    site.name,
                    site.api_url,
                    site.base_url,
                    site.language,
                    site.timezone,
                    site.icon,
                    1 if site.enabled else 0,
                    site.auth_username,
                    site.auth_password,
                    site.poll_interval_override,
                    site.digest_threshold_override,
                    source,
                    added_by,
                    int(time.time()),
                ),
            )
            self._conn.commit()

    def update_site_fields(self, site_id: str, **fields: object) -> bool:
        """局部更新站点字段（白名单由调用方保证）。"""
        allowed = {
            "name", "api_url", "base_url", "language", "timezone", "icon",
            "enabled", "auth_username", "auth_password",
            "poll_interval", "digest_threshold",
        }
        cols = {k: v for k, v in fields.items() if k in allowed}
        if not cols:
            return False
        assignments = ", ".join(f"{k}=?" for k in cols)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE sites SET {assignments} WHERE id=?",
                [*cols.values(), site_id],
            )
            self._conn.commit()
        return cur.rowcount > 0

    def set_site_enabled(self, site_id: str, enabled: bool) -> bool:
        return self.update_site_fields(site_id, enabled=1 if enabled else 0)

    def delete_site(self, site_id: str) -> bool:
        """删除站点并清理其订阅/监视/游标/去重记录。"""
        with self._lock:
            cur = self._conn.execute("DELETE FROM sites WHERE id=?", (site_id,))
            existed = cur.rowcount > 0
            for table in ("subscriptions", "subs_tombstone", "watch", "cursor", "seen"):
                col = "site" if table in ("cursor", "seen") else "site_id"
                self._conn.execute(f"DELETE FROM {table} WHERE {col}=?", (site_id,))
            self._conn.commit()
        return existed

    # ---------------- v2：群设置 ----------------

    def get_group_setting(self, group_id: str, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM group_settings WHERE group_id=? AND key=?",
            (group_id, key),
        ).fetchone()
        return row["value"] if row else None

    def set_group_setting(self, group_id: str, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO group_settings(group_id,key,value) VALUES(?,?,?) "
                "ON CONFLICT(group_id,key) DO UPDATE SET value=excluded.value",
                (group_id, key, value),
            )
            self._conn.commit()

    def all_group_settings(self, group_id: str | None = None) -> dict[str, dict[str, str]]:
        if group_id is None:
            rows = self._conn.execute(
                "SELECT group_id,key,value FROM group_settings"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT group_id,key,value FROM group_settings WHERE group_id=?",
                (group_id,),
            ).fetchall()
        out: dict[str, dict[str, str]] = {}
        for r in rows:
            out.setdefault(r["group_id"], {})[r["key"]] = r["value"]
        return out

    # ---------------- v2：审计 ----------------

    def add_audit(
        self, action: str, *, group_id: str | None = None, user_id: str | None = None,
        detail: str = "",
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit(ts,group_id,user_id,action,detail) VALUES(?,?,?,?,?)",
                (int(time.time()), group_id, user_id, action, detail),
            )
            self._conn.commit()

    def list_audit(self, limit: int = 20) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    # ---------------- v1 -> v2 迁移 ----------------

    def schema_version(self) -> str:
        return self.get_meta("schema_version") or "1"

    def migrate_legacy(self) -> None:
        """v1→v2 数据搬迁，以及 schema_version 推进到 3（幂等）。"""
        version = self.schema_version()
        if version == "3":
            return
        if version == "1":
            self._migrate_v1_to_v2()
            version = "2"
        if version == "2":
            # v2→v3：repost_* 表已由 _SCHEMA 建好，只需记录版本
            with self._lock:
                self._conn.execute(
                    "INSERT INTO meta(key,value) VALUES('schema_version','3') "
                    "ON CONFLICT(key) DO UPDATE SET value='3'"
                )
                self._conn.commit()

    def _migrate_v1_to_v2(self) -> None:
        try:
            with self._lock:
                for r in self._conn.execute("SELECT * FROM subs").fetchall():
                    self._conn.execute(
                        "INSERT OR IGNORE INTO subscriptions"
                        "(group_id,site_id,active,namespaces,ignore_users,min_sizediff,origin) "
                        "VALUES(?,?,1,?,?,?,'legacy')",
                        (
                            r["group_id"], r["site_id"], r["namespaces"],
                            r["ignore_users"], r["min_sizediff"],
                        ),
                    )
                row = self._conn.execute(
                    "SELECT value FROM meta WHERE key='dyn_sites'"
                ).fetchone()
                if row and row["value"]:
                    for sid, s in (json.loads(row["value"]) or {}).items():
                        site = WikiSite(**s)
                        self._conn.execute(
                            "INSERT OR IGNORE INTO sites"
                            "(id,name,api_url,base_url,language,timezone,icon,enabled,"
                            "auth_username,auth_password,poll_interval,digest_threshold,source) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'dynamic')",
                            (
                                site.id, site.name, site.api_url, site.base_url,
                                site.language, site.timezone, site.icon,
                                1 if site.enabled else 0, site.auth_username,
                                site.auth_password, site.poll_interval_override,
                                site.digest_threshold_override,
                            ),
                        )
                self._conn.execute(
                    "INSERT INTO meta(key,value) VALUES('schema_version','2') "
                    "ON CONFLICT(key) DO UPDATE SET value='2'"
                )
                self._conn.commit()
        except Exception as exc:  # noqa: BLE001 — 迁移失败必须显式暴露
            LOG.error("schema v1->v2 迁移失败：%s", exc)
            raise

    # ---------------- v3：海外平台转载 ----------------

    @staticmethod
    def _source_from_row(row: sqlite3.Row) -> RepostSource:
        return RepostSource(
            id=row["id"],
            platform=row["platform"],
            channel_id=row["channel_id"],
            guild_id=row["guild_id"] or "",
            name=row["name"],
            enabled=bool(row["enabled"]),
            available=bool(row["available"]),
        )

    @staticmethod
    def _filter_from_binding(row: sqlite3.Row) -> RepostFilter:
        keywords = json.loads(row["keywords"]) if row["keywords"] else None
        ignore = json.loads(row["ignore_authors"]) if row["ignore_authors"] else []
        return RepostFilter(
            keywords=keywords,
            ignore_authors=ignore,
            ignore_bots=bool(row["ignore_bots"]),
        )

    def upsert_repost_source(
        self,
        source: RepostSource,
        *,
        added_by: str = "",
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO repost_sources"
                "(id,platform,channel_id,guild_id,name,enabled,available,added_by,added_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "name=excluded.name, guild_id=excluded.guild_id, "
                "enabled=excluded.enabled, available=excluded.available",
                (
                    source.id,
                    source.platform,
                    source.channel_id,
                    source.guild_id,
                    source.name,
                    1 if source.enabled else 0,
                    1 if source.available else 0,
                    added_by,
                    int(time.time()),
                ),
            )
            self._conn.commit()

    def get_repost_source(self, source_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM repost_sources WHERE id=?", (source_id,)
        ).fetchone()

    def get_repost_source_by_channel(
        self, platform: str, channel_id: str
    ) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM repost_sources WHERE platform=? AND channel_id=?",
            (platform, channel_id),
        ).fetchone()

    def list_repost_sources(self, *, enabled_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM repost_sources"
        if enabled_only:
            sql += " WHERE enabled=1 AND available=1"
        sql += " ORDER BY id"
        return self._conn.execute(sql).fetchall()

    def repost_source_object(self, source_id: str) -> RepostSource | None:
        row = self.get_repost_source(source_id)
        return self._source_from_row(row) if row else None

    def set_repost_source_flags(
        self, source_id: str, *, enabled: bool | None = None, available: bool | None = None
    ) -> bool:
        fields: list[str] = []
        values: list[object] = []
        if enabled is not None:
            fields.append("enabled=?")
            values.append(1 if enabled else 0)
        if available is not None:
            fields.append("available=?")
            values.append(1 if available else 0)
        if not fields:
            return False
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE repost_sources SET {', '.join(fields)} WHERE id=?",
                [*values, source_id],
            )
            self._conn.commit()
        return cur.rowcount > 0

    def delete_repost_source(self, source_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM repost_sources WHERE id=?", (source_id,)
            )
            existed = cur.rowcount > 0
            for table, col in (
                ("repost_bindings", "source_id"),
                ("repost_tombstone", "source_id"),
                ("repost_cursor", "source_id"),
                ("repost_seen", "source_id"),
            ):
                self._conn.execute(f"DELETE FROM {table} WHERE {col}=?", (source_id,))
            self._conn.commit()
        return existed

    def upsert_repost_binding(
        self,
        group_id: str,
        source_id: str,
        flt: RepostFilter | None = None,
        *,
        origin: str = "command",
    ) -> None:
        flt = flt or RepostFilter()
        with self._lock:
            self._conn.execute(
                "INSERT INTO repost_bindings"
                "(group_id,source_id,active,keywords,ignore_authors,ignore_bots,origin) "
                "VALUES(?,?,1,?,?,?,?) ON CONFLICT(group_id,source_id) DO UPDATE SET "
                "active=1, keywords=excluded.keywords, ignore_authors=excluded.ignore_authors, "
                "ignore_bots=excluded.ignore_bots, origin=excluded.origin",
                (
                    group_id,
                    source_id,
                    json.dumps(flt.keywords) if flt.keywords is not None else None,
                    json.dumps(list(flt.ignore_authors)),
                    1 if flt.ignore_bots else 0,
                    origin,
                ),
            )
            self._conn.execute(
                "DELETE FROM repost_tombstone WHERE group_id=? AND source_id=?",
                (group_id, source_id),
            )
            self._conn.commit()

    def remove_repost_binding(self, group_id: str, source_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM repost_bindings WHERE group_id=? AND source_id=?",
                (group_id, source_id),
            )
            existed = cur.rowcount > 0
            self._conn.execute(
                "INSERT INTO repost_tombstone(group_id,source_id,ts) VALUES(?,?,?) "
                "ON CONFLICT(group_id,source_id) DO UPDATE SET ts=excluded.ts",
                (group_id, source_id, int(time.time())),
            )
            self._conn.commit()
        return existed

    def has_repost_tombstone(self, group_id: str, source_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM repost_tombstone WHERE group_id=? AND source_id=?",
            (group_id, source_id),
        ).fetchone()
        return row is not None

    def set_repost_binding_active(
        self, group_id: str, source_id: str, active: bool
    ) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE repost_bindings SET active=? WHERE group_id=? AND source_id=?",
                (1 if active else 0, group_id, source_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def get_repost_binding(self, group_id: str, source_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM repost_bindings WHERE group_id=? AND source_id=?",
            (group_id, source_id),
        ).fetchone()

    def list_repost_bindings(
        self, group_id: str | None = None, *, active_only: bool = True
    ) -> dict[tuple[str, str], RepostFilter]:
        sql = "SELECT * FROM repost_bindings"
        args: list[object] = []
        clauses: list[str] = []
        if group_id is not None:
            clauses.append("group_id=?")
            args.append(group_id)
        if active_only:
            clauses.append("active=1")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        rows = self._conn.execute(sql, args).fetchall()
        return {(r["group_id"], r["source_id"]): self._filter_from_binding(r) for r in rows}

    def repost_routing(self) -> dict[str, list[tuple[str, RepostFilter]]]:
        """source_id -> [(group_id, filter)]，只含启用且可用的源与有效绑定。"""
        enabled = {
            r["id"]
            for r in self.list_repost_sources(enabled_only=True)
        }
        result: dict[str, list[tuple[str, RepostFilter]]] = {}
        for (gid, sid), flt in self.list_repost_bindings().items():
            if sid not in enabled:
                continue
            result.setdefault(sid, []).append((gid, flt))
        return result

    def get_repost_cursor(self, source_id: str) -> str:
        row = self._conn.execute(
            "SELECT last_msg_id FROM repost_cursor WHERE source_id=?", (source_id,)
        ).fetchone()
        return (row["last_msg_id"] if row else "") or ""

    def set_repost_cursor(self, source_id: str, last_msg_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO repost_cursor(source_id,last_msg_id) VALUES(?,?) "
                "ON CONFLICT(source_id) DO UPDATE SET last_msg_id=excluded.last_msg_id",
                (source_id, last_msg_id or ""),
            )
            self._conn.commit()

    def filter_repost_seen(self, source_id: str, msg_ids: list[str]) -> list[str]:
        if not msg_ids:
            return []
        q = ",".join("?" * len(msg_ids))
        rows = self._conn.execute(
            f"SELECT msg_id FROM repost_seen WHERE source_id=? AND msg_id IN ({q})",
            [source_id, *msg_ids],
        ).fetchall()
        seen = {r["msg_id"] for r in rows}
        return [x for x in msg_ids if x not in seen]

    def mark_repost_seen(
        self, source_id: str, msg_ids: list[str], keep: int = SEEN_KEEP
    ) -> None:
        if not msg_ids:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO repost_seen(source_id,msg_id) VALUES(?,?)",
                [(source_id, x) for x in msg_ids],
            )
            # snowflake 数值越大越新；按数值排序裁剪
            row = self._conn.execute(
                "SELECT msg_id FROM repost_seen WHERE source_id=? "
                "ORDER BY CAST(msg_id AS INTEGER) DESC LIMIT 1 OFFSET ?",
                (source_id, keep - 1),
            ).fetchone()
            if row:
                self._conn.execute(
                    "DELETE FROM repost_seen WHERE source_id=? "
                    "AND CAST(msg_id AS INTEGER) < CAST(? AS INTEGER)",
                    (source_id, row["msg_id"]),
                )
            self._conn.commit()

