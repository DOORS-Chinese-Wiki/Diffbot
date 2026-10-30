"""限流推送器：令牌桶 + 每群队列 worker + 积压聚合 + 长消息分段。"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from .config import PushConfig
from .i18n import get_message

LOG = logging.getLogger("diffbot.pusher")

Sender = Callable[..., Awaitable[None]]


@dataclass
class PushItem:
    """一条待推送内容及其发送选项。"""

    text: str
    image_path: str | None = None
    visual: bool | None = None          # None 表示沿用全局 push.visual
    throttle_per_minute: int | None = None  # None 表示沿用全局限流


class TokenBucket:
    """令牌桶：容量 = per_minute（允许突发），速率 = per_minute/60。"""

    def __init__(self, per_minute: float) -> None:
        self.rate = max(per_minute, 0.1) / 60.0
        self.capacity = max(per_minute, 1.0)
        self.tokens = self.capacity
        self._last = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
        self._last = now

    def try_acquire(self) -> bool:
        self._refill()
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False

    async def acquire(self) -> None:
        while not self.try_acquire():
            deficit = 1.0 - self.tokens
            await asyncio.sleep(max(deficit / self.rate, 0.05))


def split_message(text: str, limit: int) -> list[str]:
    """按行边界切分长消息，每段不超过 limit 字符。"""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.splitlines():
        extra = len(line) + 1
        if buf and size + extra > limit:
            parts.append("\n".join(buf))
            buf, size = [], 0
        # 单行超限时硬切
        while len(line) > limit:
            parts.append(line[:limit])
            line = line[limit:]
        buf.append(line)
        size += len(line) + 1
    if buf:
        parts.append("\n".join(buf))
    return parts or [""]


class Pusher:
    """按群排队推送，令牌桶限流；队列满时聚合为摘要。"""

    def __init__(self, sender: Sender, cfg: PushConfig) -> None:
        self._sender = sender
        self._cfg = cfg
        self._queues: dict[str, asyncio.Queue[str]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._buckets: dict[str, TokenBucket] = {}
        self._stopping = False

    async def start(self) -> None:
        self._stopping = False

    async def stop(self) -> None:
        self._stopping = True
        for task in self._workers.values():
            task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers.values(), return_exceptions=True)
        self._workers.clear()
        self._queues.clear()

    def pending(self, group_id: str) -> int:
        q = self._queues.get(group_id)
        return q.qsize() if q else 0

    async def push(
        self,
        group_id: str,
        text: str,
        image_path: str | None = None,
        *,
        visual: bool | None = None,
        throttle_per_minute: int | None = None,
    ) -> None:
        """入队一条推送；队列满时与积压内容聚合成摘要。

        visual / throttle_per_minute 允许按群覆盖（None 用全局配置）。
        """
        q = self._queues.get(group_id)
        if q is None:
            q = asyncio.Queue(maxsize=self._cfg.max_pending)
            self._queues[group_id] = q
            rate = throttle_per_minute or self._cfg.throttle_per_minute
            self._buckets[group_id] = TokenBucket(rate)
            self._workers[group_id] = asyncio.create_task(self._worker(group_id, q))
        item = PushItem(
            text=text, image_path=image_path, visual=visual,
            throttle_per_minute=throttle_per_minute,
        )
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            LOG.warning("群 %s 推送积压，触发聚合", group_id)
            self._coalesce(group_id, q)
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:  # 理论不可达：聚合后必然有空位
                pass

    def _coalesce(self, group_id: str, q: "asyncio.Queue[PushItem]") -> None:
        items: list[PushItem] = []
        while not q.empty() and len(items) < self._cfg.max_pending:
            items.append(q.get_nowait())
        header = get_message("zh", "backlog")
        lines = [
            item.text.splitlines()[0] if item.text.splitlines() else ""
            for item in items[:10]
        ]
        digest = header + "\n" + "\n".join(lines)
        q.put_nowait(PushItem(text=digest))

    async def _worker(self, group_id: str, q: "asyncio.Queue[PushItem]") -> None:
        try:
            while True:
                item: PushItem = await q.get()
                try:
                    bucket = self._buckets.get(group_id)
                    if bucket is None:
                        bucket = TokenBucket(
                            item.throttle_per_minute or self._cfg.throttle_per_minute
                        )
                        self._buckets[group_id] = bucket
                    await bucket.acquire()
                    visual = self._cfg.visual if item.visual is None else item.visual
                    if visual and item.image_path:
                        # 图文模式：只发图；图发失败退回纯文本
                        try:
                            await self._sender(group_id, "", image_path=item.image_path)
                        except Exception:
                            for chunk in split_message(
                                item.text, self._cfg.max_message_chars
                            ):
                                await self._sender(group_id, chunk)
                    else:
                        chunks = split_message(item.text, self._cfg.max_message_chars)
                        for i, chunk in enumerate(chunks):
                            img = item.image_path if i == len(chunks) - 1 else None
                            await self._sender(group_id, chunk, image_path=img)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOG.exception("群 %s 推送失败", group_id)
                finally:
                    q.task_done()
        except asyncio.CancelledError:
            return
