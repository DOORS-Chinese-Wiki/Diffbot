"""消息格式化：Change + DiffInfo -> 本地化 QQ 消息文本。"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote

from .config import PushConfig, WikiSite
from .i18n import get_message
from .models import Change, DiffInfo


def local_time(ts: str, tz_name: str) -> str:
    """ISO8601 UTC -> 指定时区可读时间；时区无效时原样返回。"""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        try:
            from zoneinfo import ZoneInfo

            dt = dt.astimezone(ZoneInfo(tz_name))
        except Exception:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z").strip()
    except ValueError:
        return ts


def diff_url(site: WikiSite, change: Change) -> str:
    q = quote(change.title, safe="")
    old = change.old_revid if change.old_revid else "prev"
    return f"{site.base_url}/w/index.php?title={q}&diff={change.revid}&oldid={old}"


def format_change(
    site: WikiSite,
    change: Change,
    info: DiffInfo | None = None,
    preview: list[str] | None = None,
    cfg: PushConfig | None = None,
) -> str:
    """格式化单条变更消息（计划书 §4.3 / §6.3 结构）。"""
    cfg = cfg or PushConfig()
    lang = site.language
    L = lambda key, **kw: get_message(lang, key, **kw)  # noqa: E731

    head_key = "page_created" if change.type == "new" else "page_updated"
    site_tag = "[EN]" if lang.startswith("en") else ("[ZH]" if lang.startswith("zh") else f"[{lang.upper()}]")
    lines = [f"{site.icon}{site_tag} {L(head_key, title=change.title)}"]
    lines.append(f"{L('editor')}: {change.user}")
    lines.append(f"{L('time')}: {local_time(change.timestamp, site.timezone)}")
    if change.type == "new":
        size_txt = f"+{change.sizediff} {L('bytes')} {L('new_page_tag')}"
    else:
        sign = "+" if change.sizediff >= 0 else ""
        size_txt = f"{sign}{change.sizediff} {L('bytes')}"
    lines.append(f"{L('changes')}: {size_txt}")
    if change.comment:
        lines.append(f"{L('summary')}: {change.comment}")

    body: list[str] = []
    if info and info.lines:
        changed = [x for x in info.lines if x.kind in ("add", "del")]
        shown = changed[: cfg.max_diff_lines]
        for item in shown:
            if item.kind == "add":
                body.append(f"🟩➕ {item.text}")
            else:
                body.append(f"🟥➖ {item.text}")
        if len(changed) > len(shown):
            body.append(L("lines_more", n=len(changed) - len(shown)))
    elif preview:
        body = [f"🟩➕ {p.strip()}" for p in preview if p.strip()]

    if body:
        lines.append("")
        lines.append(L("diff_header"))
        lines.extend(body)

    lines.append("")
    lines.append(f"{L('view_detail')}: {diff_url(site, change)}")
    return "\n".join(lines)


def format_digest(site: WikiSite, changes: list[Change], cfg: PushConfig | None = None) -> str:
    """批量变更聚合为一条摘要（防消息轰炸）。"""
    lang = site.language
    L = lambda key, **kw: get_message(lang, key, **kw)  # noqa: E731
    site_tag = "[EN]" if lang.startswith("en") else ("[ZH]" if lang.startswith("zh") else f"[{lang.upper()}]")
    lines = [f"{site.icon}{site_tag} " + L("digest_header", n=len(changes))]
    for c in changes[:10]:
        delta = f"+{c.sizediff}" if c.sizediff >= 0 else str(c.sizediff)
        lines.append(L("digest_item", title=c.title, user=c.user, delta=delta))
    if len(changes) > 10:
        lines.append(L("lines_more", n=len(changes) - 10))
    return "\n".join(lines)
