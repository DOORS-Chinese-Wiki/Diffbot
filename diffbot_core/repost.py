"""海外平台转载引擎：Discord REST / Twitter-RSSHub 每源一个轮询协程。"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from pathlib import Path
from typing import Any

from .config import DiscordConfig, RsshubConfig
from .discord_api import DiscordClient, DiscordError
from .models import DiscordMessage, RepostSource
from .repost_format import format_discord_message
from .rss_api import RssClient, RssError, rsshub_user_feed
from .store import Store
from .twitter_api import TwitterClient, TwitterError

LOG = logging.getLogger("diffbot.repost")

DISCORD_EPOCH_MS = 1420070400000


def snowflake_now() -> str:
    """把当前时间编码为 Discord snowflake（空频道首次游标占位）。"""
    ms = int(time.time() * 1000)
    return str((ms - DISCORD_EPOCH_MS) << 22)


class RepostEngine:
    """轮询已启用的海外源，过滤后交给 Pusher。"""

    def __init__(
        self,
        store: Store,
        sender: Any,
        discord_cfg: DiscordConfig,
        data_dir: Path,
        *,
        rsshub_cfg: RsshubConfig | None = None,
        lang_of: Any = None,
    ) -> None:
        self.store = store
        self.sender = sender
        self.cfg = discord_cfg
        self.rsshub_cfg = rsshub_cfg or RsshubConfig()
        self.data_dir = Path(data_dir)
        self.lang_of = lang_of or (lambda gid: "zh")
        self.stats = {"polls": 0, "pushed": 0, "errors": 0}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._clients: dict[str, Any] = {}
        self._stopping = False
        self._consecutive_fail: dict[str, int] = {}

    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._stopping = False
        if not self.cfg.token:
            LOG.info("Discord token 未配置，Discord 源不会轮询")
        await self.sync()
        LOG.info("转载引擎启动：%d 个源", len(self._tasks))

    async def stop(self) -> None:
        self._stopping = True
        tasks = list(self._tasks.values())
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        for c in self._clients.values():
            await c.close()
        self._clients.clear()
        LOG.info("转载引擎已停止")

    def _desired_sources(self) -> dict[str, RepostSource]:
        out: dict[str, RepostSource] = {}
        for row in self.store.list_repost_sources(enabled_only=True):
            src = self.store.repost_source_object(row["id"])
            if src is None:
                continue
            if src.platform == "discord" and not self.cfg.token:
                continue
            out[src.id] = src
        return out

    async def sync(self) -> None:
        desired = self._desired_sources()
        for sid in list(self._tasks):
            if sid not in desired:
                await self.remove_source(sid)
        for sid, src in desired.items():
            if sid not in self._tasks:
                self._start_source(src)
        LOG.info("转载源任务同步完成：%d 个", len(self._tasks))

    def _start_source(self, source: RepostSource) -> None:
        if source.platform == "twitter":
            client: Any = TwitterClient(timeout=self.rsshub_cfg.timeout)
        elif source.platform == "rss":
            client = RssClient(timeout=self.rsshub_cfg.timeout)
        else:
            if not self.cfg.token:
                return
            client = DiscordClient(self.cfg.token, timeout=self.cfg.timeout)
        self._clients[source.id] = client
        self._tasks[source.id] = asyncio.create_task(
            self._loop(source), name=f"repost-{source.id}"
        )

    async def remove_source(self, source_id: str) -> None:
        task = self._tasks.pop(source_id, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        client = self._clients.pop(source_id, None)
        if client is not None:
            await client.close()
        self._consecutive_fail.pop(source_id, None)

    # ------------------------------------------------------------------

    def _interval(self, source: RepostSource) -> tuple[float, float]:
        if source.platform in ("twitter", "rss"):
            return self.rsshub_cfg.poll_interval, self.rsshub_cfg.jitter
        return self.cfg.poll_interval, self.cfg.jitter

    async def _loop(self, source: RepostSource) -> None:
        backoff = 0
        first = True
        while not self._stopping:
            try:
                await self._poll_once(source, first=first)
                first = False
                backoff = 0
                self._consecutive_fail[source.id] = 0
                self.stats["polls"] += 1
            except asyncio.CancelledError:
                raise
            except (DiscordError, RssError, TwitterError) as exc:
                self.stats["errors"] += 1
                backoff = min(backoff + 1, 5)
                fails = self._consecutive_fail.get(source.id, 0) + 1
                self._consecutive_fail[source.id] = fails
                LOG.warning("源 %s 轮询失败（第 %d 次）: %s", source.id, backoff, exc)
                if getattr(exc, "unavailable", False) or fails >= 8:
                    self.store.set_repost_source_flags(source.id, available=False)
                    LOG.error("源 %s 暂停轮询（不可用）", source.id)
                    return
            except Exception as exc:  # noqa: BLE001
                self.stats["errors"] += 1
                backoff = min(backoff + 1, 5)
                LOG.warning("源 %s 轮询异常（第 %d 次）: %s", source.id, backoff, exc)
            base, jitter = self._interval(source)
            delay = base + random.uniform(0, jitter)
            if backoff:
                delay += min(600.0, base * (2**backoff))
            await asyncio.sleep(delay)

    async def _poll_once(self, source: RepostSource, *, first: bool = False) -> None:
        if source.platform == "twitter":
            await self._poll_twitter(source, first=first)
        elif source.platform == "rss":
            await self._poll_rss(source, first=first)
        else:
            await self._poll_discord(source, first=first)

    async def _poll_discord(self, source: RepostSource, *, first: bool = False) -> None:
        client = self._clients[source.id]
        cursor = self.store.get_repost_cursor(source.id)
        msgs = await client.list_messages(
            source.channel_id, after=cursor or None, limit=self.cfg.batch_limit
        )

        if first and not cursor:
            if msgs:
                newest = msgs[-1]
            else:
                self.store.set_repost_cursor(source.id, snowflake_now())
                LOG.info("源 %s 首次游标初始化（空频道，从现在起）", source.id)
                return
            self.store.set_repost_cursor(source.id, newest.id)
            LOG.info("源 %s 首次游标初始化：%s", source.id, newest.id)
            return

        await self._ingest(source, msgs)

    async def _poll_rss(self, source: RepostSource, *, first: bool = False) -> None:
        client = self._clients[source.id]
        feed_url = source.channel_id
        msgs = await client.fetch_feed(
            feed_url, channel_id=source.channel_id, author_name=source.name
        )
        msgs = msgs[-self.rsshub_cfg.batch_limit :]
        if first and not self.store.get_repost_cursor(source.id):
            ids = [m.id for m in msgs if m.id]
            self.store.mark_repost_seen(source.id, ids)
            newest = msgs[-1].id if msgs else str(int(time.time()))
            self.store.set_repost_cursor(source.id, newest)
            LOG.info("源 %s 首次游标初始化：%s（不回放）", source.id, newest)
            return
        await self._ingest(source, msgs)

    async def _poll_twitter(self, source: RepostSource, *, first: bool = False) -> None:
        client = self._clients[source.id]
        cursor = self.store.get_repost_cursor(source.id)
        msgs = await client.list_tweets(
            source.channel_id, after=cursor or None, limit=self.rsshub_cfg.batch_limit
        )
        if first and not cursor:
            ids = [m.id for m in msgs if m.id]
            self.store.mark_repost_seen(source.id, ids)
            newest = msgs[-1].id if msgs else str(int(time.time()))
            self.store.set_repost_cursor(source.id, newest)
            LOG.info("源 %s 首次游标初始化：%s（不回放）", source.id, newest)
            return
        await self._ingest(source, msgs)

    async def _ingest(self, source: RepostSource, msgs: list[DiscordMessage]) -> None:
        if not msgs:
            return
        ids = [m.id for m in msgs if m.id]
        fresh = set(self.store.filter_repost_seen(source.id, ids))
        msgs = [m for m in msgs if m.id in fresh]
        self.store.mark_repost_seen(source.id, ids)
        if not msgs:
            return

        newest = msgs[-1]
        self.store.set_repost_cursor(source.id, newest.id)

        routing = self.store.repost_routing().get(source.id) or []
        if not routing:
            return

        for msg in msgs:
            targets = [(gid, flt) for gid, flt in routing if flt.passes(msg)]
            if not targets:
                continue
            for gid, _flt in targets:
                await self._deliver(source, msg, gid)

    async def _download_first_image(
        self, source: RepostSource, msg: DiscordMessage
    ) -> str | None:
        client = self._clients[source.id]
        image_count = sum(1 for a in msg.attachments if a.is_image)
        local_image: str | None = None
        for i, att in enumerate(a for a in msg.attachments if a.is_image):
            if local_image or not att.url:
                continue
            dest = (
                self.data_dir
                / "repost"
                / source.id
                / msg.id
                / f"{i}_{att.filename or 'image'}"
            )
            max_bytes = (
                self.rsshub_cfg.max_image_bytes
                if source.platform in ("twitter", "rss")
                else self.cfg.max_image_bytes
            )
            path = await client.download_image(att.url, dest, max_bytes=max_bytes)
            if path:
                local_image = str(path)
        _ = image_count
        return local_image

    async def _deliver(
        self, source: RepostSource, msg: DiscordMessage, group_id: str
    ) -> None:
        lang = "zh"
        try:
            lang = str(self.lang_of(group_id) or "zh")
        except Exception:  # noqa: BLE001
            pass

        local_image = await self._download_first_image(source, msg)
        text = format_discord_message(
            source,
            msg,
            lang=lang,
            image_paths=[local_image] if local_image else None,
        )
        await self.sender(group_id, text, image_path=local_image, visual=False)
        self.stats["pushed"] += 1
