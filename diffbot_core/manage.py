"""管理服务：站点、订阅、过滤器、群设置、监视与审计的唯一写入口。

设计原则：
- YAML 只作为首次种子（seed_from_config）；运行期一切以 SQLite 为准
- 所有修改都经此服务，统一写审计（谁/哪个群/什么动作）
- 权限与文案由调用方（插件层）负责，本模块只做业务校验并抛 CommandError
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .commands import CommandError
from .config import DiscordConfig, RsshubConfig, SiteFilter, WikiSite, site_id_from_url
from .discord_api import DiscordClient, DiscordError
from .models import RepostFilter, RepostSource
from .mw_api import MediaWikiClient, MediaWikiError
from .rss_api import RssClient, RssError, rsshub_user_feed
from .store import Store
from .twitter_api import TwitterClient, TwitterError

LOG = logging.getLogger("diffbot.manage")

VISUAL_KEY = "visual"
THROTTLE_KEY = "throttle_per_minute"
DIGEST_KEY = "digest_threshold"
LANG_KEY = "lang"
ADMINS_KEY = "admins"

_TRUTHY = {"1", "true", "on", "yes", "开"}


@dataclass
class SubView:
    """某群的一条订阅视图。"""

    site: WikiSite
    filter: SiteFilter
    origin: str


class ManagementService:
    """站点/订阅/设置的管理门面。"""

    def __init__(self, store: Store, cfg: Any, engine: Any = None) -> None:
        self.store = store
        self.cfg = cfg
        self.engine = engine

    # ------------------------------------------------------------------
    # 种子导入
    # ------------------------------------------------------------------

    def seed_from_config(self) -> int:
        """把 YAML 站点与订阅导入 DB（幂等）。

        - 站点：不存在则插入；source='yaml' 的站点每次启动按 YAML 更新配置，
          但保留运行期 enable/disable 状态
        - 订阅：仅插入缺失项，且尊重 tombstone（退订过的不复活）
        """
        imported = 0
        for sid, site in self.cfg.sites.items():
            row = self.store.get_site(sid)
            if row is None:
                self.store.upsert_site(site, source="yaml")
                imported += 1
            elif row["source"] == "yaml":
                merged = WikiSite(**{**vars(site), "enabled": bool(row["enabled"])})
                self.store.upsert_site(merged, source="yaml")

        existing = self.store.list_subs()
        for sub in self.cfg.subscriptions:
            for sid in sub.sites:
                if sid not in self.cfg.sites and self.store.get_site(sid) is None:
                    LOG.warning("YAML 订阅引用了不存在的站点：%s（已跳过）", sid)
                    continue
                key = (sub.group_id, sid)
                if key in existing:
                    continue
                if self.store.has_tombstone(sub.group_id, sid):
                    LOG.info("跳过已退订的 YAML 种子：群 %s 站点 %s", sub.group_id, sid)
                    continue
                flt = sub.filters.get(sid, SiteFilter())
                self.store.upsert_sub(sub.group_id, sid, flt, origin="yaml")
                imported += 1
        if imported:
            LOG.info("配置种子导入：%d 项", imported)
        return imported

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list_sites(self) -> list[WikiSite]:
        return [self.store._site_from_row(r) for r in self.store.list_sites()]

    def get_site(self, site_id: str) -> WikiSite | None:
        return self.store.site_object(site_id)

    def lookup_site(self, token: str, group_id: str | None = None) -> str | None:
        """宽松查找（给参数解析器消歧用）：命中返回 site_id，否则 None。"""
        if not token:
            return None
        key = token.strip().lower()
        rows = self.store.list_sites()
        ids = {r["id"]: r for r in rows}
        if key in {k.lower() for k in ids}:
            for sid in ids:
                if sid.lower() == key:
                    return sid
        matches = [
            r["id"]
            for r in rows
            if r["id"].lower().startswith(key) or key in (r["name"] or "").lower()
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def resolve_site(
        self, group_id: str, query: str | None, *, subscribed_only: bool = True
    ) -> WikiSite:
        """把用户输入解析成站点；歧义/缺失时抛 CommandError（不再静默兜底）。"""
        subs = self.list_subs(group_id)
        if query:
            key = query.strip().lower()
            for view in subs:
                if view.site.id.lower() == key:
                    return view.site
            hits = [
                v for v in subs
                if v.site.id.lower().startswith(key) or key in v.site.name.lower()
            ]
            if len(hits) == 1:
                return hits[0].site
            if len(hits) > 1:
                names = "、".join(v.site.id for v in hits)
                raise CommandError(f"「{query}」匹配到多个站点：{names}，请用精确 ID。")
            if not subscribed_only:
                sid = self.lookup_site(query)
                if sid:
                    site = self.get_site(sid)
                    if site:
                        return site
            raise CommandError(f"本群未订阅「{query}」。用 /wiki list 查看可用站点。")

        if not subs:
            raise CommandError("本群尚未订阅任何站点。用 /wiki add <URL> 添加。")
        if len(subs) == 1:
            return subs[0].site
        names = "、".join(v.site.id for v in subs)
        raise CommandError(f"本群订阅了多个站点，请用 @站点ID 指定：{names}")

    def list_subs(self, group_id: str) -> list[SubView]:
        rows = self.store.list_subs(group_id)
        views: list[SubView] = []
        for (gid, sid), flt in rows.items():
            site = self.get_site(sid)
            if site is None:
                continue
            origin = "yaml"
            raw = self.store._conn.execute(
                "SELECT origin FROM subscriptions WHERE group_id=? AND site_id=?",
                (gid, sid),
            ).fetchone()
            if raw:
                origin = raw["origin"]
            views.append(SubView(site=site, filter=flt, origin=origin))
        return sorted(views, key=lambda v: v.site.id)

    def get_filter(self, group_id: str, site_id: str) -> SiteFilter:
        return self.store.list_subs(group_id).get((group_id, site_id), SiteFilter())

    # ------------------------------------------------------------------
    # 订阅写操作
    # ------------------------------------------------------------------

    def subscribe(self, group_id: str, site_id: str, *, origin: str = "command") -> None:
        if self.get_site(site_id) is None:
            raise CommandError(f"站点 {site_id} 不存在。")
        self.store.upsert_sub(group_id, site_id, None, origin=origin)

    def unsubscribe(self, group_id: str, site_id: str) -> bool:
        return self.store.remove_sub(group_id, site_id)

    def set_filter(self, group_id: str, site_id: str, **fields: Any) -> SiteFilter:
        flt = self.get_filter(group_id, site_id)
        ns = fields.get("namespaces", flt.namespaces)
        ignore = fields.get("ignore_users", flt.ignore_users)
        minbytes = fields.get("min_sizediff", flt.min_sizediff)
        new = SiteFilter(namespaces=ns, ignore_users=list(ignore or []), min_sizediff=int(minbytes or 0))
        self.store.upsert_sub(group_id, site_id, new, origin="command")
        return new

    def watch_add(self, group_id: str, site_id: str, title: str) -> None:
        self.store.add_watch(group_id, site_id, title)

    def watch_remove(self, group_id: str, site_id: str, title: str) -> bool:
        return self.store.remove_watch(group_id, site_id, title)

    def watch_list(self, group_id: str, site_id: str) -> list[str]:
        return sorted(self.store.list_watch(group_id, site_id))

    def watch_clear(self, group_id: str, site_id: str) -> int:
        titles = self.watch_list(group_id, site_id)
        for t in titles:
            self.store.remove_watch(group_id, site_id, t)
        return len(titles)

    # ------------------------------------------------------------------
    # 站点写操作
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_url(raw: str) -> str:
        url = (raw or "").strip()
        if not url:
            raise CommandError("站点 URL 不能为空。")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        return url

    @staticmethod
    def _derive_api(url: str) -> tuple[str, str]:
        """从用户给的 URL 推导 (api_url, base_url)。"""
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path or "/"
        if path.endswith("api.php"):
            api = f"{origin}{path}"
            base = api[: -len("api.php")].rstrip("/")
            return api, base or origin
        return f"{origin}/w/api.php", origin

    async def _probe(self, api_url: str, *, auth: tuple[str, str] | None = None) -> str:
        """校验 API 可达/可读；返回站点时间戳。凭证错误会抛 CommandError。"""
        client = MediaWikiClient(api_url, timeout=self.cfg.poll.timeout)
        try:
            if auth:
                client._auth_username, client._auth_password = auth
                try:
                    await client.login()
                except MediaWikiError as exc:
                    raise CommandError(f"登录失败：{exc}") from exc
            try:
                ts = await client.server_time()
                await client.recentchanges(limit=1)
            except MediaWikiError as exc:
                raise CommandError(f"API 不可用：{exc}") from exc
            return ts or ""
        finally:
            await client.close()

    async def add_site(
        self,
        group_id: str,
        user: str,
        url: str,
        *,
        site_id: str | None = None,
        lang: str | None = None,
        name: str | None = None,
        api_url: str | None = None,
        auth: tuple[str, str] | None = None,
        interval: float | None = None,
        digest: int | None = None,
    ) -> WikiSite:
        """校验并新增站点，然后立即开始监控。"""
        normalized = self._normalize_url(url)
        origin = f"{urlparse(normalized).scheme}://{urlparse(normalized).netloc}"
        if api_url:
            api = self._normalize_url(api_url)
            base = api[: -len("api.php")].rstrip("/") if api.endswith("api.php") else origin
            await self._probe(api, auth=auth)
        else:
            api, base = self._derive_api(normalized)
            try:
                await self._probe(api, auth=auth)
            except CommandError:
                alt = f"{origin}/api.php"
                if alt == api:
                    raise
                await self._probe(alt, auth=auth)
                api, base = alt, origin

        sid = (site_id or site_id_from_url(normalized)).strip()
        if self.get_site(sid) is not None:
            raise CommandError(f"站点 ID {sid} 已存在。用 /wiki edit {sid} 修改，或加 --id 指定其它 ID。")
        existing = self.store.get_site_by_api(api)
        if existing is not None:
            raise CommandError(
                f"该 API 已被站点 {existing['id']} 使用；用 /wiki add 前请先 /wiki list 查看。"
            )

        host = urlparse(normalized).netloc or normalized
        site = WikiSite(
            id=sid,
            name=name or host,
            api_url=api,
            base_url=base,
            language=(lang or (host.split(".")[0] if host.count(".") >= 1 else "en")),
            timezone="Asia/Shanghai" if (lang or "").startswith("zh") else "UTC",
            auth_username=auth[0] if auth else "",
            auth_password=auth[1] if auth else "",
            poll_interval_override=interval,
            digest_threshold_override=digest,
        )
        self.store.upsert_site(site, source="dynamic", added_by=user)
        self.store.upsert_sub(group_id, sid, None, origin="command")
        self.audit(group_id, user, "site_add", f"{sid} {api}")
        await self.sync_engine()
        return site

    async def update_site(
        self, group_id: str, user: str, query: str, **fields: Any
    ) -> WikiSite:
        site = self.resolve_site(group_id, query, subscribed_only=False)
        updates: dict[str, Any] = {}
        if fields.get("name"):
            updates["name"] = str(fields["name"])
        if fields.get("lang"):
            updates["language"] = str(fields["lang"])
        if fields.get("tz"):
            updates["timezone"] = str(fields["tz"])
        if fields.get("api"):
            updates["api_url"] = self._normalize_url(str(fields["api"]))
        if fields.get("icon"):
            updates["icon"] = str(fields["icon"])
        if fields.get("interval") is not None:
            updates["poll_interval"] = float(fields["interval"])
        if fields.get("digest") is not None:
            updates["digest_threshold"] = int(fields["digest"])
        if fields.get("auth"):
            raw = str(fields["auth"])
            if "@" not in raw:
                raise CommandError("凭证格式应为 用户名@机器人名:密码，例如 Yatmt@diffbot:xxxx")
            user_part, pwd = raw.rsplit(":", 1)
            updates["auth_username"] = user_part
            updates["auth_password"] = pwd
        if not updates:
            raise CommandError("没有需要修改的字段。可用：--name/--lang/--tz/--api/--icon/--auth/--interval/--digest")

        if "api_url" in updates:
            existing = self.store.get_site_by_api(updates["api_url"])
            if existing is not None and existing["id"] != site.id:
                raise CommandError(f"API 已被站点 {existing['id']} 使用。")
        self.store.update_site_fields(site.id, **updates)
        self.audit(group_id, user, "site_edit", f"{site.id} {updates}")
        await self.sync_engine()
        return self.get_site(site.id) or site

    async def set_enabled(
        self, group_id: str, user: str, query: str, enabled: bool
    ) -> WikiSite:
        site = self.resolve_site(group_id, query, subscribed_only=False)
        self.store.set_site_enabled(site.id, enabled)
        self.audit(group_id, user, "site_enable" if enabled else "site_disable", site.id)
        await self.sync_engine()
        return self.get_site(site.id) or site

    async def delete_site(self, group_id: str, user: str, query: str) -> str:
        site = self.resolve_site(group_id, query, subscribed_only=False)
        if self.engine is not None:
            await self.engine.remove_site(site.id)
        self.store.delete_site(site.id)
        self.audit(group_id, user, "site_delete", site.id)
        return site.id

    async def sync_engine(self) -> None:
        if self.engine is not None:
            await self.engine.sync()

    # ------------------------------------------------------------------
    # 群设置
    # ------------------------------------------------------------------

    def get_group_visual(self, group_id: str) -> bool:
        raw = self.store.get_group_setting(group_id, VISUAL_KEY)
        if raw is None:
            return bool(self.cfg.push.visual)
        return raw.lower() in _TRUTHY

    def set_group_visual(self, group_id: str, user: str, on: bool) -> bool:
        self.store.set_group_setting(group_id, VISUAL_KEY, "1" if on else "0")
        self.audit(group_id, user, "visual_on" if on else "visual_off")
        return on

    def get_group_throttle(self, group_id: str) -> int:
        raw = self.store.get_group_setting(group_id, THROTTLE_KEY)
        if raw is None:
            return int(self.cfg.push.throttle_per_minute)
        try:
            return max(1, int(raw))
        except ValueError:
            return int(self.cfg.push.throttle_per_minute)

    def get_group_digest(self, group_id: str) -> int | None:
        raw = self.store.get_group_setting(group_id, DIGEST_KEY)
        if raw is None:
            return None
        try:
            return max(1, int(raw))
        except ValueError:
            return None

    def get_group_lang(self, group_id: str) -> str:
        return self.store.get_group_setting(group_id, LANG_KEY) or "zh"

    def set_group_lang(self, group_id: str, user: str, lang: str) -> str:
        lang = lang.lower()
        if lang not in ("zh", "en"):
            raise CommandError("语言只支持 zh 或 en。")
        self.store.set_group_setting(group_id, LANG_KEY, lang)
        self.audit(group_id, user, "lang_set", lang)
        return lang

    def get_group_admins(self, group_id: str) -> list[str]:
        raw = self.store.get_group_setting(group_id, ADMINS_KEY) or ""
        return [x.strip() for x in raw.split(",") if x.strip()]

    def add_group_admin(self, group_id: str, user: str, qq: str) -> list[str]:
        qq = qq.strip()
        if not qq.isdigit():
            raise CommandError("QQ 号必须是数字。")
        admins = self.get_group_admins(group_id)
        if qq not in admins:
            admins.append(qq)
        self.store.set_group_setting(group_id, ADMINS_KEY, ",".join(admins))
        self.audit(group_id, user, "admin_add", qq)
        return admins

    def remove_group_admin(self, group_id: str, user: str, qq: str) -> list[str]:
        qq = qq.strip()
        admins = [x for x in self.get_group_admins(group_id) if x != qq]
        self.store.set_group_setting(group_id, ADMINS_KEY, ",".join(admins))
        self.audit(group_id, user, "admin_remove", qq)
        return admins

    # ------------------------------------------------------------------
    # 路由与审计
    # ------------------------------------------------------------------

    def routing(self) -> dict[str, list[tuple[str, SiteFilter, set[str]]]]:
        """site_id -> [(group_id, filter, watch_titles)]，只含启用站点与有效订阅。"""
        enabled = {r["id"] for r in self.store.list_sites(enabled_only=True)}
        watch_all = self.store.list_watch_all()
        result: dict[str, list[tuple[str, SiteFilter, set[str]]]] = {}
        for (gid, sid), flt in self.store.list_subs().items():
            if sid not in enabled:
                continue
            result.setdefault(sid, []).append(
                (gid, flt, watch_all.get((gid, sid), set()))
            )
        return result

    def audit(self, group_id: str, user: str, action: str, detail: str = "") -> None:
        try:
            self.store.add_audit(action, group_id=group_id, user_id=user, detail=detail)
        except Exception as exc:  # noqa: BLE001 — 审计失败不影响主流程
            LOG.warning("写审计失败：%s", exc)


class BridgeView:
    """某群的一条 Discord 转载绑定视图。"""

    def __init__(
        self, source: RepostSource, flt: RepostFilter, *, active: bool = True
    ) -> None:
        self.source = source
        self.filter = flt
        self.active = active


class BridgeService:
    """海外转载绑定/过滤的唯一写入口（Discord / Twitter-RSS）。"""

    PLATFORM_DISCORD = "discord"
    PLATFORM_TWITTER = "twitter"
    PLATFORM_RSS = "rss"

    _HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")

    def __init__(
        self,
        store: Store,
        cfg: Any,
        *,
        discord: DiscordConfig | None = None,
        rsshub: RsshubConfig | None = None,
        engine: Any = None,
    ) -> None:
        self.store = store
        self.cfg = cfg
        self.discord_cfg = discord or getattr(cfg, "discord", DiscordConfig())
        self.rsshub_cfg = rsshub or getattr(cfg, "rsshub", RsshubConfig())
        self.engine = engine

    def source_id(self, platform: str, ref: str) -> str:
        if platform == self.PLATFORM_RSS:
            digest = hashlib.sha1(ref.encode("utf-8")).hexdigest()[:12]
            return f"rss_{digest}"
        return f"{platform}_{ref}"

    def _make_discord_client(self) -> DiscordClient:
        token = self.discord_cfg.token
        if not token:
            raise CommandError(
                "未配置 Discord Bot Token（环境变量 DIFFBOT_DISCORD_TOKEN 或 YAML discord.token）。"
            )
        return DiscordClient(token, timeout=self.discord_cfg.timeout)

    @classmethod
    def _twitter_handle(cls, handle: str) -> str:
        handle = handle.strip().lstrip("@")
        if not handle or not cls._HANDLE_RE.match(handle):
            raise CommandError(
                f"「{handle}」不是合法 Twitter 用户名（1–15 位字母/数字/下划线，不含空格）。"
                "若只有显示名，请到 x.com 个人页复制 @handle。"
            )
        return handle

    @staticmethod
    def parse_source_ref(raw: str) -> tuple[str, str]:
        """解析绑定目标 → (platform, id)。

        - 纯数字 / discord.com/channels/... → (discord, channel_id)
        - @handle / twitter.com|x.com/handle → (twitter, handle)
        - 其它 http(s) RSS/Atom URL → (rss, url)
        """
        text = (raw or "").strip()
        if not text:
            raise CommandError("绑定目标不能为空。")
        if text.isdigit():
            return BridgeService.PLATFORM_DISCORD, text

        if text.startswith("@") and len(text) > 1:
            return BridgeService.PLATFORM_TWITTER, BridgeService._twitter_handle(text)

        low = text.lower()
        if any(d in low for d in ("twitter.com/", "x.com/")):
            parts = [p for p in text.rstrip("/").split("/") if p]
            for i, p in enumerate(parts):
                if p.lower() in ("twitter.com", "x.com", "www.twitter.com", "www.x.com"):
                    if i + 1 < len(parts):
                        handle = parts[i + 1]
                        if handle.lower() not in (
                            "i",
                            "home",
                            "search",
                            "intent",
                            "share",
                        ):
                            return BridgeService.PLATFORM_TWITTER, BridgeService._twitter_handle(
                                handle
                            )
            raise CommandError("无法从推特链接解析用户名。")

        if low.startswith(("http://", "https://")):
            # 排除 Discord 频道链接后再当 RSS
            if "discord.com/" not in low and "discordapp.com/" not in low:
                return BridgeService.PLATFORM_RSS, text

        parts = [p for p in text.rstrip("/").split("/") if p]
        if "channels" in parts:
            idx = parts.index("channels")
            rest = parts[idx + 1 :]
            if len(rest) >= 2 and rest[1].isdigit():
                return BridgeService.PLATFORM_DISCORD, rest[1]
            if len(rest) == 1 and rest[0].isdigit():
                return BridgeService.PLATFORM_DISCORD, rest[0]
        raise CommandError(
            "无法解析绑定目标。\n"
            "Discord：纯数字 channel_id 或 https://discord.com/channels/<guild>/<channel>\n"
            "Twitter：@handle（用户名，不是显示名）\n"
            "任意 RSS：完整 feed URL（公共 rsshub.app 的 /twitter 已失效，请用可用 RSS 或自建实例）"
        )

    @staticmethod
    def parse_channel_ref(raw: str) -> str:
        """兼容旧接口：仅 Discord channel_id。"""
        platform, ref = BridgeService.parse_source_ref(raw)
        if platform != BridgeService.PLATFORM_DISCORD:
            raise CommandError("该目标不是 Discord 频道。")
        return ref

    def list_bindings(self, group_id: str) -> list[BridgeView]:
        rows = self.store.list_repost_bindings(group_id, active_only=False)
        out: list[BridgeView] = []
        for (gid, sid), flt in rows.items():
            if gid != group_id:
                continue
            src = self.store.repost_source_object(sid)
            if src is None:
                continue
            raw = self.store.get_repost_binding(gid, sid)
            active = bool(raw["active"]) if raw else False
            out.append(BridgeView(src, flt, active=active))
        return sorted(out, key=lambda v: v.source.id)

    def resolve_source(self, group_id: str, query: str | None) -> RepostSource:
        views = self.list_bindings(group_id)
        if not query:
            if len(views) == 1:
                return views[0].source
            raise CommandError("本群绑定了多个源，请指定源 ID。用 /bridge list 查看。")
        key = query.strip().lower()
        bare = key[1:] if key.startswith("@") else key
        for v in views:
            if v.source.id.lower() == key or v.source.channel_id.lower() == bare:
                return v.source
            if v.source.name.lower() == key:
                return v.source
        hits = [
            v.source
            for v in views
            if v.source.id.lower().startswith(key)
            or v.source.channel_id.lower().startswith(bare)
            or key in v.source.name.lower()
        ]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            names = "、".join(s.id for s in hits)
            raise CommandError(f"「{query}」匹配到多个源：{names}，请用精确 ID。")
        raise CommandError(f"本群未绑定「{query}」。用 /bridge list 查看。")

    async def add_binding(
        self,
        group_id: str,
        user: str,
        target_ref: str,
        *,
        name: str | None = None,
    ) -> tuple[RepostSource, bool]:
        """校验目标并绑定；返回 (source, created_new_source)。"""
        platform, ref = self.parse_source_ref(target_ref)
        sid = self.source_id(platform, ref)
        created = False
        existing = self.store.get_repost_source_by_channel(platform, ref)

        if platform == self.PLATFORM_TWITTER:
            display = name or f"@{ref}"
            client = TwitterClient(timeout=self.rsshub_cfg.timeout)
            try:
                _uid, real_name = await client.resolve_user(ref)
                display = name or f"@{ref}"
                _ = real_name
            except TwitterError as exc:
                raise CommandError(
                    f"无法读取推特账号 @{ref}：{exc}\n"
                    "请确认 handle 正确（x.com 个人页地址栏用户名）。"
                ) from exc
            finally:
                await client.close()
            guild_id = ""
        elif platform == self.PLATFORM_RSS:
            display = name or urlparse(ref).netloc or "rss"
            client = RssClient(timeout=self.rsshub_cfg.timeout)
            try:
                await client.fetch_feed(ref, channel_id=ref, author_name=display)
            except RssError as exc:
                raise CommandError(f"RSS 拉取失败：{exc}") from exc
            finally:
                await client.close()
            guild_id = ""
        else:
            client = self._make_discord_client()
            try:
                meta = await client.get_channel(ref)
            except DiscordError as exc:
                if exc.unavailable:
                    raise CommandError(f"Discord 频道不可读：{exc}") from exc
                raise CommandError(f"Discord API 失败：{exc}") from exc
            finally:
                await client.close()
            display = name or str(meta.get("name") or ref)
            guild_id = str(meta.get("guild_id") or "")

        if existing is None:
            source = RepostSource(
                id=sid,
                platform=platform,
                channel_id=ref,
                guild_id=guild_id,
                name=display,
                enabled=True,
                available=True,
            )
            self.store.upsert_repost_source(source, added_by=user)
            created = True
        else:
            source = self.store.repost_source_object(existing["id"])  # type: ignore[index]
            if source is None:
                raise CommandError("源数据损坏，请先 /bridge delete 再添加。")
            source.name = name or source.name or display
            source.guild_id = guild_id or source.guild_id
            source.enabled = True
            source.available = True
            self.store.upsert_repost_source(source, added_by=user)
            sid = source.id

        self.store.upsert_repost_binding(group_id, sid, None, origin="command")
        self.audit(group_id, user, "bridge_add", f"{sid} #{source.name}")
        await self.sync_engine()
        return (self.store.repost_source_object(sid) or source), created

    async def remove_binding(self, group_id: str, user: str, query: str | None) -> str:
        source = self.resolve_source(group_id, query)
        self.store.remove_repost_binding(group_id, source.id)
        self.audit(group_id, user, "bridge_remove", source.id)
        # 若无任何群仍绑定且无 tombstone 需求，源可保留给其它群；仅当无绑定时停止轮询
        remaining = self.store.list_repost_bindings(active_only=False)
        still = any(sid == source.id for (_, sid) in remaining)
        if not still:
            await self.sync_engine()
        return source.id

    async def set_binding_active(
        self, group_id: str, user: str, query: str | None, active: bool
    ) -> str:
        source = self.resolve_source(group_id, query)
        ok = self.store.set_repost_binding_active(group_id, source.id, active)
        if not ok:
            raise CommandError("绑定不存在。")
        if active:
            # enable 时恢复源可用性，允许重新轮询
            self.store.set_repost_source_flags(source.id, available=True)
        self.audit(
            group_id,
            user,
            "bridge_enable" if active else "bridge_disable",
            source.id,
        )
        await self.sync_engine()
        return source.id

    async def delete_source(self, user: str, query: str) -> str:
        key = (query or "").strip()
        row = self.store.get_repost_source(key)
        if row is None:
            if key.isdigit():
                row = self.store.get_repost_source_by_channel(self.PLATFORM_DISCORD, key)
            elif key.startswith("@") and len(key) > 1:
                row = self.store.get_repost_source_by_channel(
                    self.PLATFORM_TWITTER, key[1:]
                )
            if row is None:
                matches = [
                    r
                    for r in self.store.list_repost_sources()
                    if r["id"].lower() == key.lower() or key.lower() in (r["name"] or "").lower()
                ]
                if len(matches) == 1:
                    row = matches[0]
                elif len(matches) > 1:
                    names = "、".join(r["id"] for r in matches)
                    raise CommandError(f"「{query}」匹配到多个源：{names}")
        if row is None:
            raise CommandError(f"未找到源「{query}」。")
        sid = row["id"]
        if self.engine is not None and hasattr(self.engine, "remove_source"):
            await self.engine.remove_source(sid)
        self.store.delete_repost_source(sid)
        self.store.add_audit("bridge_delete", user_id=user, detail=sid)
        return sid

    def get_filter(self, group_id: str, source_id: str) -> RepostFilter:
        return self.store.list_repost_bindings(group_id).get(
            (group_id, source_id), RepostFilter()
        )

    def set_filter(self, group_id: str, user: str, query: str | None, **fields: Any) -> RepostFilter:
        source = self.resolve_source(group_id, query)
        current = self.get_filter(group_id, source.id)
        keywords = fields.get("keywords", current.keywords)
        ignore = fields.get("ignore_authors", current.ignore_authors)
        ignore_bots = fields.get("ignore_bots", current.ignore_bots)
        new = RepostFilter(
            keywords=None if keywords is None else list(keywords),
            ignore_authors=list(ignore or []),
            ignore_bots=bool(ignore_bots),
        )
        self.store.upsert_repost_binding(group_id, source.id, new, origin="command")
        self.audit(group_id, user, "bridge_filter", f"{source.id} {fields}")
        return new

    def keyword_add(self, group_id: str, user: str, query: str | None, word: str) -> RepostFilter:
        source = self.resolve_source(group_id, query)
        flt = self.get_filter(group_id, source.id)
        words = list(flt.keywords or [])
        if word not in words:
            words.append(word)
        return self.set_filter(group_id, user, source.id, keywords=words)

    def keyword_remove(self, group_id: str, user: str, query: str | None, word: str) -> RepostFilter:
        source = self.resolve_source(group_id, query)
        flt = self.get_filter(group_id, source.id)
        words = [w for w in (flt.keywords or []) if w != word]
        return self.set_filter(group_id, user, source.id, keywords=words)

    def keyword_clear(self, group_id: str, user: str, query: str | None) -> RepostFilter:
        return self.set_filter(group_id, user, query, keywords=None)

    def ignore_add(self, group_id: str, user: str, query: str | None, author: str) -> RepostFilter:
        source = self.resolve_source(group_id, query)
        flt = self.get_filter(group_id, source.id)
        names = list(flt.ignore_authors)
        if author not in names:
            names.append(author)
        return self.set_filter(group_id, user, source.id, ignore_authors=names)

    def ignore_remove(self, group_id: str, user: str, query: str | None, author: str) -> RepostFilter:
        source = self.resolve_source(group_id, query)
        flt = self.get_filter(group_id, source.id)
        names = [a for a in flt.ignore_authors if a != author]
        return self.set_filter(group_id, user, source.id, ignore_authors=names)

    def set_ignore_bots(
        self, group_id: str, user: str, query: str | None, on: bool
    ) -> RepostFilter:
        return self.set_filter(group_id, user, query, ignore_bots=on)

    def audit(self, group_id: str, user: str, action: str, detail: str = "") -> None:
        try:
            self.store.add_audit(action, group_id=group_id, user_id=user, detail=detail)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("写审计失败：%s", exc)

    async def sync_engine(self) -> None:
        if self.engine is not None and hasattr(self.engine, "sync"):
            await self.engine.sync()

