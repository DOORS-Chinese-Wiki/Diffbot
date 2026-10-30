"""Discord REST 客户端（aiohttp）：频道校验、拉消息、下载图片附件。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import aiohttp

from .models import DiscordAttachment, DiscordMessage

LOG = logging.getLogger("diffbot.discord")

API_BASE = "https://discord.com/api/v10"
DEFAULT_UA = "DiffBot/0.1 (Discord repost)"
TEXT_CHANNEL_TYPES = {0, 5}  # GUILD_TEXT / GUILD_ANNOUNCEMENT


class DiscordError(RuntimeError):
    """Discord API 最终失败。"""

    def __init__(self, message: str, *, status: int = 0, unavailable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.unavailable = unavailable


def parse_message(raw: dict[str, Any]) -> DiscordMessage:
    """把 REST 消息 JSON 解析为 DiscordMessage。"""
    author = raw.get("author") or {}
    attachments: list[DiscordAttachment] = []
    for a in raw.get("attachments") or []:
        attachments.append(
            DiscordAttachment(
                id=str(a.get("id") or ""),
                filename=str(a.get("filename") or ""),
                url=str(a.get("url") or ""),
                content_type=str(a.get("content_type") or ""),
                size=int(a.get("size") or 0),
            )
        )
    return DiscordMessage(
        id=str(raw.get("id") or ""),
        channel_id=str(raw.get("channel_id") or ""),
        guild_id=str(raw.get("guild_id") or ""),
        author_id=str(author.get("id") or ""),
        author_name=str(author.get("global_name") or author.get("username") or ""),
        content=str(raw.get("content") or ""),
        timestamp=str(raw.get("timestamp") or ""),
        attachments=attachments,
        is_bot=bool(author.get("bot")),
        embeds_count=len(raw.get("embeds") or []),
    )


class DiscordClient:
    """Bot Token 客户端。可注入外部 session（测试用）。"""

    def __init__(
        self,
        token: str,
        *,
        timeout: float = 15.0,
        user_agent: str = DEFAULT_UA,
        session: aiohttp.ClientSession | None = None,
        retries: int = 3,
        api_base: str = API_BASE,
    ) -> None:
        self.token = token
        self._timeout = timeout
        self._ua = user_agent
        self._session = session
        self._owns_session = session is None
        self.retries = retries
        self.api_base = api_base.rstrip("/")

    async def __aenter__(self) -> "DiscordClient":
        await self._ensure_session()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _ensure_session(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout)
            )

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bot {self.token}", "User-Agent": self._ua}

    async def _request(
        self, method: str, path: str, *, params: dict[str, Any] | None = None
    ) -> Any:
        await self._ensure_session()
        url = f"{self.api_base}{path}"
        last_exc: Exception | None = None
        for attempt in range(self.retries):
            try:
                async with self._session.request(  # type: ignore[union-attr]
                    method, url, params=params, headers=self._headers()
                ) as resp:
                    if resp.status == 429:
                        try:
                            data = await resp.json(content_type=None)
                            delay = float(data.get("retry_after") or 1.0)
                        except Exception:  # noqa: BLE001
                            delay = 1.0
                        last_exc = DiscordError(f"HTTP 429", status=429)
                        await asyncio.sleep(min(delay, 30.0))
                        continue
                    if resp.status >= 500:
                        last_exc = DiscordError(f"HTTP {resp.status}", status=resp.status)
                        await asyncio.sleep(0.5 * 2**attempt)
                        continue
                    if resp.status in (401, 403, 404):
                        body = await resp.text()
                        raise DiscordError(
                            f"HTTP {resp.status}: {body[:200]}",
                            status=resp.status,
                            unavailable=resp.status in (403, 404),
                        )
                    if resp.status >= 400:
                        body = await resp.text()
                        raise DiscordError(f"HTTP {resp.status}: {body[:200]}", status=resp.status)
                    if resp.status == 204:
                        return None
                    return await resp.json(content_type=None)
            except DiscordError:
                raise
            except aiohttp.ClientError as e:
                last_exc = e
                await asyncio.sleep(0.5 * 2**attempt)
                continue
        raise DiscordError(f"请求失败（重试 {self.retries} 次）: {last_exc}")

    async def get_channel(self, channel_id: str) -> dict[str, Any]:
        """拉取频道元数据；类型不是文本/公告频道时抛 DiscordError。"""
        data = await self._request("GET", f"/channels/{channel_id}")
        # 注意：type=0（文本频道）为合法值，不能用 `or -1`
        ctype = int(data.get("type", -1))
        if ctype not in TEXT_CHANNEL_TYPES:
            raise DiscordError(f"不支持的频道类型：{ctype}", status=400)
        return data

    async def list_messages(
        self, channel_id: str, *, after: str | None = None, limit: int = 50
    ) -> list[DiscordMessage]:
        """拉取频道消息。after 为空时取最新；否则取 id 之后的增量。

        Discord 返回按 id 升序；这里统一按 snowflake 升序返回。
        """
        params: dict[str, Any] = {"limit": max(1, min(limit, 100))}
        if after and after != "0":
            params["after"] = after
        data = await self._request("GET", f"/channels/{channel_id}/messages", params=params)
        msgs = [parse_message(x) for x in (data or []) if x.get("id")]
        msgs.sort(key=lambda m: int(m.id))
        return msgs

    async def download_image(
        self, url: str, dest: Path, *, max_bytes: int = 8 * 1024 * 1024
    ) -> Path | None:
        """下载图片到 dest；超限或失败返回 None（不抛）。"""
        await self._ensure_session()
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with self._session.get(  # type: ignore[union-attr]
                url, headers={"User-Agent": self._ua}
            ) as resp:
                if resp.status != 200:
                    LOG.warning("附件下载失败 HTTP %s: %s", resp.status, url)
                    return None
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        LOG.warning("附件超过 %s 字节，跳过: %s", max_bytes, url)
                        return None
                dest.write_bytes(bytes(buf))
                return dest
        except Exception as exc:  # noqa: BLE001
            LOG.warning("附件下载异常: %s (%s)", url, exc)
            return None
