"""极简 RSS/Atom 客户端：拉取 RSSHub 等源，映射为转载消息。"""

from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp

from .models import DiscordAttachment, DiscordMessage

LOG = logging.getLogger("diffbot.rss")

DEFAULT_UA = "DiffBot/0.1 (RSS repost)"
ATOM_NS = "{http://www.w3.org/2005/Atom}"
_IMG_RE = re.compile(
    r'<img[^>]+src=["\']([^"\']+)["\']', re.I
)
_STATUS_ID_RE = re.compile(r"/status(?:es)?/(\d+)")


class RssError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, unavailable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.unavailable = unavailable


def rsshub_user_feed(base_url: str, handle: str) -> str:
    return f"{base_url.rstrip('/')}/twitter/user/{handle.lstrip('@')}"


def _text(el: ET.Element | None) -> str:
    if el is None or el.text is None:
        return ""
    return el.text.strip()


def _strip_html(raw: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", raw or "", flags=re.I)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
    )
    return text.strip()


def _first_image(raw_html: str) -> str | None:
    m = _IMG_RE.search(raw_html or "")
    return m.group(1) if m else None


def _to_iso(pub: str) -> str:
    if not pub:
        return ""
    # RFC822（RSS）
    try:
        return parsedate_to_datetime(pub).isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    # ISO（Atom）
    try:
        return parsedate_to_datetime(pub).isoformat()  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        return pub


def _item_id(link: str, guid: str) -> str:
    m = _STATUS_ID_RE.search(link or "")
    if m:
        return m.group(1)
    if guid:
        return guid
    return link or ""


def parse_feed(body: str, *, channel_id: str, author_name: str) -> list[DiscordMessage]:
    """解析 RSS2 / Atom 为 DiscordMessage（复用转载管线）。"""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise RssError(f"RSS XML 解析失败: {exc}") from exc

    items: list[DiscordMessage] = []
    # RSS 2.0
    for item in root.findall("./channel/item"):
        title = _text(item.find("title"))
        desc_raw = _text(item.find("description"))
        link = _text(item.find("link"))
        guid = _text(item.find("guid"))
        pub = _text(item.find("pubDate"))
        author = _text(item.find("creator")) or _text(item.find("author")) or author_name
        content = _strip_html(desc_raw) or title
        img = _first_image(desc_raw)
        attachments = []
        if img:
            attachments.append(
                DiscordAttachment(
                    id=img, filename=img.rsplit("/", 1)[-1], url=img, content_type="image/*"
                )
            )
        items.append(
            DiscordMessage(
                id=_item_id(link, guid),
                channel_id=channel_id,
                guild_id="",
                author_id=author,
                author_name=author,
                content=content,
                timestamp=_to_iso(pub),
                attachments=attachments,
                is_bot=False,
                embeds_count=0,
            )
        )

    # Atom
    if not items:
        for entry in root.findall(f"{ATOM_NS}entry"):
            title = _text(entry.find(f"{ATOM_NS}title"))
            summary = _text(entry.find(f"{ATOM_NS}summary"))
            content_el = entry.find(f"{ATOM_NS}content")
            content_raw = _text(content_el) or summary
            link = ""
            for l in entry.findall(f"{ATOM_NS}link"):
                if l.get("rel") in (None, "alternate"):
                    link = l.get("href") or ""
                    break
            guid = _text(entry.find(f"{ATOM_NS}id"))
            pub = _text(entry.find(f"{ATOM_NS}updated")) or _text(
                entry.find(f"{ATOM_NS}published")
            )
            author_el = entry.find(f"{ATOM_NS}author")
            author = _text(author_el.find(f"{ATOM_NS}name")) if author_el is not None else author_name
            author = author or author_name
            content = _strip_html(content_raw) or title
            img = _first_image(content_raw)
            attachments = []
            if img:
                attachments.append(
                    DiscordAttachment(
                        id=img, filename=img.rsplit("/", 1)[-1], url=img, content_type="image/*"
                    )
                )
            items.append(
                DiscordMessage(
                    id=_item_id(link, guid),
                    channel_id=channel_id,
                    guild_id="",
                    author_id=author,
                    author_name=author,
                    content=content,
                    timestamp=pub,
                    attachments=attachments,
                    is_bot=False,
                    embeds_count=0,
                )
            )

    # 去重 + 按时间/id 升序
    seen_ids: set[str] = set()
    uniq: list[DiscordMessage] = []
    for m in items:
        if not m.id or m.id in seen_ids:
            continue
        seen_ids.add(m.id)
        uniq.append(m)
    uniq.sort(key=lambda m: (m.timestamp or "", m.id))
    return uniq


class RssClient:
    def __init__(
        self,
        *,
        timeout: float = 15.0,
        user_agent: str = DEFAULT_UA,
        session: aiohttp.ClientSession | None = None,
        retries: int = 3,
    ) -> None:
        self._timeout = timeout
        self._ua = user_agent
        self._session = session
        self._owns_session = session is None
        self.retries = retries

    async def _ensure_session(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout)
            )

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    async def fetch_feed(
        self, url: str, *, channel_id: str, author_name: str
    ) -> list[DiscordMessage]:
        await self._ensure_session()
        last_exc: Exception | None = None
        for attempt in range(self.retries):
            try:
                async with self._session.get(  # type: ignore[union-attr]
                    url, headers={"User-Agent": self._ua}
                ) as resp:
                    if resp.status == 429 or resp.status >= 500:
                        last_exc = RssError(f"HTTP {resp.status}", status=resp.status)
                        await asyncio.sleep(0.5 * 2**attempt)
                        continue
                    if resp.status in (401, 403, 404):
                        body = await resp.text()
                        raise RssError(
                            f"HTTP {resp.status}: {body[:160]}",
                            status=resp.status,
                            unavailable=True,
                        )
                    if resp.status >= 400:
                        body = await resp.text()
                        raise RssError(f"HTTP {resp.status}: {body[:160]}", status=resp.status)
                    body = await resp.text()
                    return parse_feed(body, channel_id=channel_id, author_name=author_name)
            except RssError:
                raise
            except aiohttp.ClientError as e:
                last_exc = e
                await asyncio.sleep(0.5 * 2**attempt)
                continue
        raise RssError(f"RSS 请求失败（重试 {self.retries} 次）: {last_exc}")

    async def download_image(
        self, url: str, dest: Any, *, max_bytes: int = 8 * 1024 * 1024
    ):
        from pathlib import Path

        await self._ensure_session()
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with self._session.get(  # type: ignore[union-attr]
                url, headers={"User-Agent": self._ua}
            ) as resp:
                if resp.status != 200:
                    return None
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        return None
                dest.write_bytes(bytes(buf))
                return dest
        except Exception as exc:  # noqa: BLE001
            LOG.warning("RSS 附件下载失败 %s: %s", url, exc)
            return None
