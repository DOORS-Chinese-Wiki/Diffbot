"""数据模型：变更事件与差异内容。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Change:
    """一条 recentchanges 变更。"""

    site_id: str
    rc_id: int
    page_id: int
    title: str
    ns: int
    revid: int
    old_revid: int
    user: str
    timestamp: str  # ISO8601 UTC，MediaWiki 格式：2026-09-09T14:30:25Z
    comment: str = ""
    new_len: int = 0
    old_len: int = 0
    sizediff: int = 0
    type: str = "edit"  # edit | new
    minor: bool = False


@dataclass
class DiffLine:
    """一行差异：kind in {add, del, ctx}。"""

    kind: str
    text: str


@dataclass
class DiffInfo:
    """compare API 的解析结果。"""

    lines: list[DiffLine] = field(default_factory=list)
    fromrevid: int = 0
    torevid: int = 0


@dataclass
class DiscordAttachment:
    """Discord 消息附件。"""

    id: str
    filename: str
    url: str
    content_type: str = ""
    size: int = 0

    @property
    def is_image(self) -> bool:
        return self.content_type.startswith("image/")


@dataclass
class DiscordMessage:
    """一条 Discord 频道消息（转载引擎的最小输入）。"""

    id: str
    channel_id: str
    guild_id: str
    author_id: str
    author_name: str
    content: str
    timestamp: str  # ISO8601 UTC
    attachments: list[DiscordAttachment] = field(default_factory=list)
    is_bot: bool = False
    embeds_count: int = 0


@dataclass
class RepostFilter:
    """群对某一 Discord 源的过滤规则。"""

    keywords: list[str] | None = None
    ignore_authors: list[str] = field(default_factory=list)
    ignore_bots: bool = True

    def passes(self, msg: DiscordMessage) -> bool:
        if self.ignore_bots and msg.is_bot:
            return False
        author_keys = {msg.author_id, msg.author_name}
        if any(a in author_keys for a in self.ignore_authors):
            return False
        if not msg.content.strip() and not any(a.is_image for a in msg.attachments):
            return False
        if self.keywords:
            hay = f"{msg.content}\n{msg.author_name}".lower()
            if not any(k.lower() in hay for k in self.keywords):
                return False
        return True


@dataclass
class RepostSource:
    """一个可被多群绑定的海外消息源（当前仅 Discord 频道）。"""

    id: str
    platform: str
    channel_id: str
    guild_id: str
    name: str
    enabled: bool = True
    available: bool = True
