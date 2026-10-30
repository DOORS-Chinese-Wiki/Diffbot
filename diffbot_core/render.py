"""Diff 图片渲染：PIL 把变更差异画成红绿对比卡片（图文可视化模式）。

仅依赖 Pillow；字体按优先级探测（Linux 常见中文字体 -> 默认）。
"""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import WikiSite
from .formatter import local_time
from .models import Change, DiffInfo

# 配色（深色卡片）
BG = (24, 26, 32)
HEADER_FG = (240, 240, 245)
META_FG = (160, 165, 175)
ADD_BG = (28, 46, 34)
ADD_FG = (140, 230, 160)
DEL_BG = (52, 30, 32)
DEL_FG = (240, 150, 150)
LINK_FG = (110, 160, 235)
LINE_H = 30
PAD = 22
MAX_W = 900

_FONT_CANDIDATES = [
    # 系统级 MiSans（服务器 /usr/local/share/fonts/MiSans）
    "/usr/local/share/fonts/MiSans/MiSans-Regular.ttf",
    "/usr/local/share/fonts/MiSans/MiSans-Normal.ttf",
    "/usr/share/fonts/truetype/misans/MiSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "C:\\Windows\\Fonts\\msyh.ttc",
    "C:\\Windows\\Fonts\\simhei.ttf",
]

_FONT_BOLD_CANDIDATES = [
    "/usr/local/share/fonts/MiSans/MiSans-Semibold.ttf",
    "/usr/local/share/fonts/MiSans/MiSans-Demibold.ttf",
    "/usr/local/share/fonts/MiSans/MiSans-Bold.ttf",
    "/usr/share/fonts/truetype/misans/MiSans-Bold.ttf",
]


def _font(size: int, bold: bool = False):
    candidates = list(_FONT_BOLD_CANDIDATES if bold else []) + list(_FONT_CANDIDATES)
    for p in candidates:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _wrap(draw, text, font, max_w):
    """按像素宽度折行。"""
    lines = []
    for raw in text.split("\n"):
        cur = ""
        for ch in raw:
            if draw.textlength(cur + ch, font=font) > max_w and cur:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
        lines.append(cur)
    return lines


def render_change_image(
    site: WikiSite,
    change: Change,
    info: DiffInfo | None,
    out_path: str | Path,
    *,
    max_lines: int = 12,
) -> Path:
    """把单条变更渲染为 PNG 卡片。返回图片路径。"""
    lang = site.language
    zh = lang.startswith("zh")
    site_tag = "[EN]" if lang.startswith("en") else ("[ZH]" if zh else f"[{lang.upper()}]")

    head_title = (
        f"{site_tag} {change.title}"
        + (" · " + (zh and "新页面" or "new page") if change.type == "new" else "")
    )
    meta = [
        ((zh and "编辑者" or "Editor") + ": " + change.user),
        ((zh and "时间" or "Time") + ": " + local_time(change.timestamp, site.timezone)),
        (
            (zh and "变更" or "Changes") + ": "
            + (("+" if change.sizediff >= 0 else "") + str(change.sizediff) + " bytes")
        ),
    ]
    if change.comment:
        meta.append((zh and "摘要" or "Summary") + ": " + change.comment)

    # diff 行
    diff_rows: list[tuple[str, str]] = []  # (kind, text)
    if info and info.lines:
        changed = [x for x in info.lines if x.kind in ("add", "del")]
        for item in changed[:max_lines]:
            diff_rows.append(("add" if item.kind == "add" else "del", item.text))
        hidden = len(changed) - len(diff_rows)
        if hidden > 0:
            diff_rows.append(("more", zh and f"… 其余 {hidden} 行" or f"… {hidden} more lines"))
    if change.type == "new" and info is None:
        diff_rows.append(("add", zh and "（新页面）" or "(new page)"))

    # 布局
    W = MAX_W
    font_head = _font(26)
    font_meta = _font(20)
    font_diff = _font(20)
    font_link = _font(18)
    tmp = Image.new("RGB", (10, 10))
    td = ImageDraw.Draw(tmp)
    inner_w = W - PAD * 2 - 20

    head_lines = _wrap(td, head_title, font_head, inner_w)
    meta_lines = []
    for mtext in meta:
        meta_lines.extend(_wrap(td, mtext, font_meta, inner_w))
    diff_lines_all = []
    for kind, text in diff_rows:
        for seg in _wrap(td, text, font_diff, inner_w - 40):
            diff_lines_all.append((kind, seg))
    url = f"{site.base_url}/w/index.php?diff={change.revid}"
    link_lines = _wrap(td, url, font_link, inner_w)

    h = PAD * 2
    h += len(head_lines) * 38 + 10
    h += len(meta_lines) * 28 + 14
    if diff_lines_all:
        h += 18 + 8 + len(diff_lines_all) * LINE_H + 14
    h += len(link_lines) * 24 + 6

    img = Image.new("RGB", (W, h), BG)
    d = ImageDraw.Draw(img)
    y = PAD
    for seg in head_lines:
        d.text((PAD, y), seg, font=font_head, fill=HEADER_FG)
        y += 38
    y += 10
    for seg in meta_lines:
        d.text((PAD, y), seg, font=font_meta, fill=META_FG)
        y += 28
    y += 14

    if diff_lines_all:
        d.line([(PAD, y), (W - PAD, y)], fill=(60, 64, 74), width=2)
        y += 18
        for kind, seg in diff_lines_all:
            if kind == "add":
                bg, fg, marker = ADD_BG, ADD_FG, "+"
            elif kind == "del":
                bg, fg, marker = DEL_BG, DEL_FG, "-"
            else:
                bg, fg, marker = (30, 32, 40), META_FG, " "
            d.rounded_rectangle(
                (PAD, y - 4, W - PAD, y - 4 + LINE_H), radius=8, fill=bg
            )
            d.text((PAD + 12, y), marker + " " + seg, font=font_diff, fill=fg)
            y += LINE_H
        y += 14

    for seg in link_lines:
        d.text((PAD, y), seg, font=font_link, fill=LINK_FG)
        y += 24

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    return out_path


def render_digest_image(
    site: WikiSite,
    changes: list,
    out_path: str | Path,
    *,
    max_items: int = 10,
) -> Path:
    """把聚合摘要渲染为图片卡片：站点标签 + 每条变更一行。"""
    lang = site.language
    zh = lang.startswith("zh")
    site_tag = "[EN]" if lang.startswith("en") else ("[ZH]" if zh else f"[{lang.upper()}]")
    n = len(changes)

    head = f"{site_tag} " + (zh and f"聚合推送：{n} 条变更" or f"Digest: {n} changes")

    W = MAX_W
    font_head = _font(26)
    font_item = _font(20)
    font_link = _font(18)
    tmp = Image.new("RGB", (10, 10))
    td = ImageDraw.Draw(tmp)
    inner_w = W - PAD * 2 - 20

    rows = []  # (kind, text)
    for c in changes[:max_items]:
        kind = "add" if c.sizediff >= 0 else "del"
        size = ("+" if c.sizediff >= 0 else "") + str(c.sizediff)
        title = f"{c.title}  ({c.user}, {size} bytes)"
        segs = _wrap(td, title, font_item, inner_w - 30)
        rows.append((kind, segs[0]))
        for extra in segs[1:]:
            rows.append(("ctx", extra))
    hidden = n - min(n, max_items)
    if hidden > 0:
        rows.append(("ctx", zh and f"… 其余 {hidden} 条" or f"… {hidden} more"))

    url = f"{site.base_url}/w/index.php?title=Special:RecentChanges"
    link_lines = _wrap(td, url, font_link, inner_w)

    h = PAD * 2 + 38 + 14
    h += len(rows) * LINE_H + 14
    h += len(link_lines) * 24

    img = Image.new("RGB", (W, h), BG)
    d = ImageDraw.Draw(img)
    y = PAD
    d.text((PAD, y), head, font=font_head, fill=HEADER_FG)
    y += 38 + 14
    for kind, seg in rows:
        if kind == "add":
            bg, fg, marker = ADD_BG, ADD_FG, "+"
        elif kind == "del":
            bg, fg, marker = DEL_BG, DEL_FG, "-"
        else:
            bg, fg, marker = (30, 32, 40), META_FG, " "
        d.rounded_rectangle((PAD, y - 4, W - PAD, y - 4 + LINE_H), radius=8, fill=bg)
        d.text((PAD + 12, y), marker + " " + seg, font=font_item, fill=fg)
        y += LINE_H
    y += 14
    for seg in link_lines:
        d.text((PAD, y), seg, font=font_link, fill=LINK_FG)
        y += 24

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    return out_path
