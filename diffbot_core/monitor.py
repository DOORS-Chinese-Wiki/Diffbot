"""监控引擎：每站点轮询协程、过滤、去重、diff 拉取、路由与聚合推送。"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from pathlib import Path
from typing import Any

from .config import BotConfig, SiteFilter, WikiSite
from .formatter import format_change, format_digest
from .render import render_change_image, render_digest_image
from .models import Change
from .mw_api import MediaWikiClient, MediaWikiError, change_from_rc
from .pusher import Sender
from .store import Store

LOG = logging.getLogger("diffbot.monitor")

HEARTBEAT_FILE = ".heartbeat"


def merge_subs(cfg: BotConfig, store: Store) -> dict[str, list[tuple[str, SiteFilter, set[str]]]]:
    """按站点索引订阅：DB 为准，YAML 只补齐 DB 中缺失的条目。

    返回：site_id -> [(group_id, filter, watch_titles), ...]
    注意（修复）：DB 条目优先，YAML 不再覆盖群级过滤器；已退订（tombstone）的
    YAML 种子不会被复活。
    """
    result: dict[str, list[tuple[str, SiteFilter, set[str]]]] = {}
    watch_all = store.list_watch_all()

    for (gid, sid), flt in store.list_subs().items():
        result.setdefault(sid, []).append((gid, flt, watch_all.get((gid, sid), set())))

    for sub in cfg.subscriptions:
        for sid in sub.sites:
            if any(e[0] == sub.group_id for e in result.get(sid, [])):
                continue
            if store.has_tombstone(sub.group_id, sid):
                continue
            flt = sub.filters.get(sid, SiteFilter())
            watch = watch_all.get((sub.group_id, sid), set())
            result.setdefault(sid, []).append((sub.group_id, flt, watch))
    return result


class MonitorEngine:
    """多站点轮询引擎。sender: async (group_id, text) -> None。"""

    def __init__(
        self, cfg: BotConfig, store: Store, sender: Sender, manage: Any = None
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.sender = sender
        self.manage = manage
        self.clients: dict[str, MediaWikiClient] = {}
        self.stats = {"polls": 0, "pushed": 0, "errors": 0}
        self._site_tasks: dict[str, asyncio.Task[None]] = {}
        self._stopping = False
        cfg.data_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._stopping = False
        if self.manage is not None:
            self.manage.seed_from_config()
        await self.sync()
        LOG.info("监控引擎启动：%d 个站点", len(self._site_tasks))

    def _desired_sites(self) -> dict[str, WikiSite]:
        """以 DB 为准的应监控站点集合。"""
        return {
            row["id"]: self.store._site_from_row(row)
            for row in self.store.list_sites(enabled_only=True)
        }

    async def sync(self) -> None:
        """按 DB 现状增删站点任务（运行期生效，无需重启）。"""
        desired = self._desired_sites()
        for sid in list(self._site_tasks):
            if sid not in desired:
                await self.remove_site(sid)
        for sid, site in desired.items():
            if sid not in self._site_tasks:
                await self.add_site(site)
        LOG.info("站点任务同步完成：%d 个", len(self._site_tasks))

    async def add_site(self, site: WikiSite) -> None:
        if site.id in self._site_tasks:
            return
        client = self._make_client(site)
        self.clients[site.id] = client
        if site.auth_username:
            try:
                await client.login()
                LOG.info("站点 %s 已以 %s 登录（只读）", site.id, site.auth_username)
            except MediaWikiError as e:
                LOG.error("站点 %s 登录失败：%s（将以匿名方式尝试）", site.id, e)
        self._site_tasks[site.id] = asyncio.create_task(
            self._loop(site), name=f"poll-{site.id}"
        )

    async def remove_site(self, site_id: str) -> None:
        task = self._site_tasks.pop(site_id, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        client = self.clients.pop(site_id, None)
        if client is not None:
            await client.close()

    def _make_client(self, site: WikiSite) -> MediaWikiClient:
        """构造带凭证的客户端（手动查询也复用，修复匿名 403）。"""
        return MediaWikiClient(
            site.api_url,
            timeout=self.cfg.poll.timeout,
            user_agent=self.cfg.user_agent,
            auth_username=site.auth_username or None,
            auth_password=site.auth_password or None,
        )

    def client_for(self, site: WikiSite) -> MediaWikiClient:
        client = self.clients.get(site.id)
        if client is None:
            client = self._make_client(site)
            self.clients[site.id] = client
        return client

    async def stop(self) -> None:
        self._stopping = True
        tasks = list(self._site_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._site_tasks.clear()
        for client in self.clients.values():
            await client.close()
        self.clients.clear()
        LOG.info("监控引擎已停止")

    # ------------------------------------------------------------------

    async def _loop(self, site: WikiSite) -> None:
        backoff = 0
        first = True
        while not self._stopping:
            try:
                await self._poll_once(site, first=first)
                first = False
                backoff = 0
                self.stats["polls"] += 1
                self._touch_heartbeat()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                backoff = min(backoff + 1, 5)
                self.stats["errors"] += 1
                LOG.warning("站点 %s 轮询失败（第 %d 次）: %s", site.id, backoff, exc)
            base = site.poll_interval_override or self.cfg.poll.interval
            delay = base + random.uniform(0, self.cfg.poll.jitter)
            if backoff:
                delay += min(600.0, base * (2**backoff))
            await asyncio.sleep(delay)

    async def _poll_once(self, site: WikiSite, *, first: bool = False) -> None:
        client = self.clients[site.id]
        last_ts, last_rc = self.store.get_cursor(site.id)
        raw = await client.recentchanges(
            newer_than=last_ts or None, limit=self.cfg.poll.batch_limit
        )
        changes = [change_from_rc(site.id, r) for r in raw]
        # 双条件去重：时间戳递增 + 同秒内 rc_id 递增
        changes = [
            c
            for c in changes
            if c.timestamp > last_ts or (c.timestamp == last_ts and c.rc_id > last_rc)
        ]

        if first:
            # 首次运行只建立游标，不回放历史
            if changes:
                newest = max(changes, key=lambda c: (c.timestamp, c.rc_id))
            else:
                ts = await client.server_time()
                newest_ts = ts or ""
                self.store.set_cursor(site.id, newest_ts, 0)
                LOG.info("站点 %s 首次游标初始化：%s", site.id, newest_ts or "（空）")
                return
            self.store.set_cursor(site.id, newest.timestamp, newest.rc_id)
            LOG.info("站点 %s 首次游标初始化：%s(%d)", site.id, newest.timestamp, newest.rc_id)
            return

        if not changes:
            return

        rc_ids = [c.rc_id for c in changes if c.rc_id]
        fresh = self.store.filter_seen(site.id, rc_ids)
        changes = [c for c in changes if (not c.rc_id) or (c.rc_id in set(fresh))]
        self.store.mark_seen(site.id, rc_ids)
        if not changes:
            return

        newest = max(changes, key=lambda c: (c.timestamp, c.rc_id))
        self.store.set_cursor(site.id, newest.timestamp, newest.rc_id)

        if self.manage is not None:
            subs = self.manage.routing().get(site.id) or []
        else:
            subs = merge_subs(self.cfg, self.store).get(site.id) or []
        if not subs:
            return

        # 站点级预筛：至少有一个群接受该变更才拉 diff
        eligible = [c for c in changes if any(f.passes(c) for _, f, _ in subs)]
        if not eligible:
            return

        # 拉取 diff（限量），新页面取内容预览
        infos: dict[int, object] = {}
        previews: dict[int, list[str]] = {}
        for c in eligible[: self.cfg.poll.max_diff_fetch]:
            try:
                if c.type == "new":
                    content = await client.page_content(c.revid)
                    previews[c.rc_id] = (content or "").splitlines()[:8]
                else:
                    infos[c.rc_id] = await client.compare(
                        c.old_revid, c.revid, uselang=site.language
                    )
            except MediaWikiError as exc:
                LOG.warning("站点 %s 获取 %s 的 diff 失败: %s", site.id, c.title, exc)

        # 按群路由 + 聚合
        per_group: dict[str, list[tuple[Change, str, object]]] = {}
        for c in eligible:
            for gid, flt, watch in subs:
                if not flt.passes(c):
                    continue
                if watch and c.title not in watch:
                    continue
                info = infos.get(c.rc_id)
                preview = previews.get(c.rc_id)
                text = format_change(site, c, info, preview, self.cfg.push)
                per_group.setdefault(gid, []).append((c, text, (info, preview)))

        for gid, items in per_group.items():
            digest_th = (
                (self.manage.get_group_digest(gid) if self.manage else None)
                or site.digest_threshold_override
                or self.cfg.poll.digest_threshold
            )
            if len(items) >= digest_th:
                digest = format_digest(site, [c for c, _t, _i in items], self.cfg.push)
                visual = (
                    self.manage.get_group_visual(gid)
                    if self.manage is not None
                    else bool(self.cfg.push.visual)
                )
                image_path = None
                if visual:
                    image_path = self._render_digest_visual(
                        site, [c for c, _t, _i in items]
                    )
                await self.sender(
                    gid, digest, image_path=image_path,
                    visual=visual,
                    throttle_per_minute=(
                        self.manage.get_group_throttle(gid) if self.manage else None
                    ),
                )
                self.stats["pushed"] += 1
            else:
                visual = (
                    self.manage.get_group_visual(gid)
                    if self.manage is not None
                    else bool(self.cfg.push.visual)
                )
                for c, text, (info, preview) in items:
                    image_path = None
                    if visual:
                        image_path = self._render_visual(site, c, info, preview)
                    await self.sender(
                        gid, text, image_path=image_path,
                        visual=visual,
                        throttle_per_minute=(
                            self.manage.get_group_throttle(gid) if self.manage else None
                        ),
                    )
                    self.stats["pushed"] += 1

    def _render_digest_visual(self, site, changes):
        """图文模式：渲染聚合摘要卡片；失败返回 None。"""
        try:
            out = self.cfg.data_dir / "visual"
            path = render_digest_image(
                site, changes, out / f"{site.id}_digest_{int(time.time())}.png",
                max_items=10,
            )
            return str(path)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("渲染聚合卡片失败（退回纯文本）: %s", exc)
            return None

    def _render_visual(self, site, change, info, preview):
        """图文模式：渲染 diff 卡片；失败返回 None（退回纯文本）。"""
        try:
            out = self.cfg.data_dir / "visual"
            path = render_change_image(
                site, change, info, out / f"{site.id}_{change.rc_id}.png",
                max_lines=self.cfg.push.max_diff_lines,
            )
            return str(path)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("渲染图文卡片失败（退回纯文本）: %s", exc)
            return None

    def _touch_heartbeat(self) -> None:
        """更新心跳文件（容器 healthcheck 依据）。"""
        hb = self.cfg.data_dir / HEARTBEAT_FILE
        try:
            hb.write_text(str(time.time()), encoding="utf-8")
        except OSError:
            pass
