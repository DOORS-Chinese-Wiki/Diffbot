"""消息格式化：海外转载 → QQ 文本。"""

from __future__ import annotations

from .formatter import local_time
from .i18n import get_message
from .models import DiscordMessage, RepostSource

_PLATFORM_LABEL = {
    "discord": "Discord",
    "twitter": "Twitter",
    "rss": "RSS",
}


def message_url(source: RepostSource, msg: DiscordMessage) -> str:
    if source.platform == "twitter":
        handle = source.channel_id
        if msg.id.isdigit():
            return f"https://x.com/{handle}/status/{msg.id}"
        return f"https://x.com/{handle}"
    if source.platform == "rss":
        # 优先用消息自身链接（解析时已尽量写入 id/guid）
        if msg.id.startswith("http"):
            return msg.id
        return source.channel_id
    guild = msg.guild_id or source.guild_id or "@me"
    return f"https://discord.com/channels/{guild}/{msg.channel_id or source.channel_id}/{msg.id}"


def format_discord_message(
    source: RepostSource,
    msg: DiscordMessage,
    *,
    lang: str = "zh",
    max_chars: int = 3000,
    image_paths: list[str] | None = None,
) -> str:
    """格式化一条转载消息；lang 跟随群语言。"""
    L = lambda key, **kw: get_message(lang, key, **kw)  # noqa: E731
    label = _PLATFORM_LABEL.get(source.platform, source.platform)
    if source.platform == "twitter":
        display = f"@{source.channel_id}"
    elif source.platform == "rss":
        display = source.name
    else:
        display = source.name
    lines = [
        L("repost_header", platform=label, name=display),
        f"{L('repost_author')}: {msg.author_name or msg.author_id}",
        f"{L('repost_time')}: {local_time(msg.timestamp, 'UTC')}",
    ]
    content = (msg.content or "").strip()
    if content:
        if len(content) > max_chars:
            content = content[: max_chars - 1] + "…"
        lines.append(f"{L('repost_content')}: {content}")
    images = image_paths or []
    if images:
        lines.append(f"{L('repost_image')} 1/{len(images)}")
    elif any(a.is_image for a in msg.attachments):
        lines.append(L("repost_image_skipped"))
    if msg.embeds_count:
        lines.append(L("repost_embeds", n=msg.embeds_count))
    lines.append(L("repost_link", url=message_url(source, msg)))
    return "\n".join(lines)
