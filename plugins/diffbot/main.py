"""DiffBot — NcatBot 插件适配层（薄层）。

核心逻辑全部在 diffbot_core 包（框架无关），本文件只负责：
- 装配：配置 / 存储 / 管理服务 / 推送器 / 监控引擎
- 指令注册：单一声明式表（commands.py）→ ncatbot 装饰器（bare + "/" 双名）
- 统一包装：权限校验 / 参数解析 / 异常回执 / 审计
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from ncatbot.core import registrar
from ncatbot.plugin import NcatBotPlugin
from ncatbot.utils import get_log

from diffbot_core.commands import (
    ArgSpec,
    CommandError,
    CommandSpec,
    FlagSpec,
    Level,
    ParsedArgs,
    find_spec,
    parse_args,
    render_help,
    usage_line,
)
from diffbot_core.config import BotConfig
from diffbot_core.formatter import format_change, format_digest
from diffbot_core.i18n import get_message
from diffbot_core.manage import BridgeService, ManagementService
from diffbot_core.monitor import MonitorEngine
from diffbot_core.mw_api import MediaWikiError, change_from_rc
from diffbot_core.pusher import Pusher
from diffbot_core.render import render_change_image, render_digest_image
from diffbot_core.repost import RepostEngine
from diffbot_core.store import Store

LOG = get_log("DiffBot")

Handler = Callable[["Ctx", ParsedArgs], Awaitable[None]]


# ======================================================================
# 指令上下文
# ======================================================================


@dataclass
class Ctx:
    """一次指令调用的上下文（便于测试时用假 event 构造）。"""

    plugin: "DiffBotPlugin"
    event: Any
    gid: str
    uid: str
    is_group: bool = True

    @property
    def manage(self) -> ManagementService:
        return self.plugin.manage

    @property
    def bridge(self) -> BridgeService:
        return self.plugin.bridge

    @property
    def cfg(self) -> BotConfig:
        return self.plugin.cfg

    @property
    def lang(self) -> str:
        return self.plugin.group_lang(self.gid)

    def L(self, key: str, **kw: Any) -> str:
        return get_message(self.lang, key, **kw)

    async def reply(self, text: str | None = None, *, image: str | None = None) -> None:
        try:
            await self.event.reply(text=text, image=image)
        except Exception as exc:  # noqa: BLE001 — 回复失败不能拖垮插件
            LOG.error("回复失败：%s", exc)


def _site_lookup(ctx: Ctx):
    return lambda token: ctx.manage.lookup_site(token, ctx.gid)


def _site(ctx: Ctx, args: ParsedArgs):
    """解析本次指令的站点（--site 优先于 @站点）。"""
    query = args.flag("site") or args.site_query
    return ctx.manage.resolve_site(ctx.gid, query)


# ======================================================================
# 指令实现
# ======================================================================


async def h_wiki_list(ctx: Ctx, args: ParsedArgs) -> None:
    views = ctx.manage.list_subs(ctx.gid)
    if not views:
        await ctx.reply(ctx.L("wiki_list_empty"))
        return
    lines = [ctx.L("wiki_list_header")]
    for v in views:
        state = ctx.L("wiki_state_on" if v.site.enabled else "wiki_state_off")
        lines.append(
            ctx.L(
                "wiki_list_item",
                name=v.site.name,
                id=v.site.id,
                language=v.site.language,
                state=state,
            )
        )
    await ctx.reply("\n".join(lines))


async def h_wiki_add(ctx: Ctx, args: ParsedArgs) -> None:
    url = args.get("url")
    auth: tuple[str, str] | None = None
    raw_auth = args.flag("user") or args.flag("auth")
    if raw_auth:
        if "@" not in raw_auth or ":" not in raw_auth:
            raise CommandError("凭证格式：--user 用户名@机器人名:密码")
        user_part, pwd = raw_auth.rsplit(":", 1)
        auth = (user_part, pwd)
    site = await ctx.manage.add_site(
        ctx.gid,
        ctx.uid,
        url,
        site_id=args.flag("id"),
        lang=args.flag("lang"),
        name=args.flag("name"),
        api_url=args.flag("api"),
        auth=auth,
        interval=args.flag("interval"),
        digest=args.flag("digest"),
    )
    await ctx.reply(ctx.L("wiki_added", id=site.id, api=site.api_url))


async def h_wiki_remove(ctx: Ctx, args: ParsedArgs) -> None:
    site = ctx.manage.resolve_site(ctx.gid, args.get("query"))
    if not ctx.manage.unsubscribe(ctx.gid, site.id):
        raise CommandError(f"本群未订阅 {site.id}。")
    ctx.manage.audit(ctx.gid, ctx.uid, "unsubscribe", site.id)
    await ctx.reply(ctx.L("wiki_removed", id=site.id))


async def h_wiki_edit(ctx: Ctx, args: ParsedArgs) -> None:
    site = await ctx.manage.update_site(
        ctx.gid,
        ctx.uid,
        args.get("query"),
        name=args.flag("name"),
        lang=args.flag("lang"),
        tz=args.flag("tz"),
        api=args.flag("api"),
        icon=args.flag("icon"),
        auth=args.flag("user") or args.flag("auth"),
        interval=args.flag("interval"),
        digest=args.flag("digest"),
    )
    await ctx.reply(ctx.L("wiki_updated", id=site.id))


async def h_wiki_toggle(ctx: Ctx, args: ParsedArgs) -> None:
    enable = args.get("_action") == "enable"
    site = await ctx.manage.set_enabled(ctx.gid, ctx.uid, args.get("query"), enable)
    key = "wiki_enabled" if enable else "wiki_disabled"
    await ctx.reply(ctx.L(key, id=site.id))


async def h_wiki_delete(ctx: Ctx, args: ParsedArgs) -> None:
    site_id = await ctx.manage.delete_site(ctx.gid, ctx.uid, args.get("query"))
    await ctx.reply(ctx.L("wiki_deleted", id=site_id))


async def h_page_list(ctx: Ctx, args: ParsedArgs) -> None:
    site = _site(ctx, args)
    titles = ctx.manage.watch_list(ctx.gid, site.id)
    if not titles:
        await ctx.reply(ctx.L("page_list_empty", site=site.id))
        return
    lines = [ctx.L("page_list_header", site=site.id)]
    lines.extend(f"• {t}" for t in titles)
    await ctx.reply("\n".join(lines))


async def h_page_watch(ctx: Ctx, args: ParsedArgs) -> None:
    site = _site(ctx, args)
    title = args.get("title").strip()
    ctx.manage.watch_add(ctx.gid, site.id, title)
    ctx.manage.audit(ctx.gid, ctx.uid, "watch_add", f"{site.id}:{title}")
    await ctx.reply(ctx.L("page_watched", title=title))


async def h_page_unwatch(ctx: Ctx, args: ParsedArgs) -> None:
    site = _site(ctx, args)
    title = args.get("title").strip()
    if not ctx.manage.watch_remove(ctx.gid, site.id, title):
        raise CommandError(ctx.L("page_not_watched", title=title))
    ctx.manage.audit(ctx.gid, ctx.uid, "watch_remove", f"{site.id}:{title}")
    await ctx.reply(ctx.L("page_unwatched", title=title))


async def h_page_clear(ctx: Ctx, args: ParsedArgs) -> None:
    site = _site(ctx, args)
    n = ctx.manage.watch_clear(ctx.gid, site.id)
    ctx.manage.audit(ctx.gid, ctx.uid, "watch_clear", site.id)
    await ctx.reply(ctx.L("page_cleared", n=n))


async def h_filter_show(ctx: Ctx, args: ParsedArgs) -> None:
    site = _site(ctx, args)
    flt = ctx.manage.get_filter(ctx.gid, site.id)
    lines = [
        ctx.L("filter_header", site=site.id),
        ctx.L("filter_item_ns", value=flt.namespaces if flt.namespaces else "全部"),
        ctx.L("filter_item_min", value=flt.min_sizediff),
        ctx.L(
            "filter_item_ignore",
            value="、".join(flt.ignore_users) if flt.ignore_users else "无",
        ),
    ]
    await ctx.reply("\n".join(lines))


async def h_filter_ns(ctx: Ctx, args: ParsedArgs) -> None:
    site = _site(ctx, args)
    raw = str(args.get("value")).replace("，", ",")
    if raw.strip() in ("all", "*", "全部"):
        namespaces = None
    else:
        try:
            namespaces = [int(x) for x in raw.split(",") if x.strip()]
        except ValueError as exc:
            raise CommandError("命名空间必须是数字列表，例如 0,1,6") from exc
    ctx.manage.set_filter(ctx.gid, site.id, namespaces=namespaces)
    ctx.manage.audit(ctx.gid, ctx.uid, "filter_ns", f"{site.id}:{namespaces}")
    await ctx.reply(ctx.L("filter_updated", site=site.id))


async def h_filter_minbytes(ctx: Ctx, args: ParsedArgs) -> None:
    site = _site(ctx, args)
    ctx.manage.set_filter(ctx.gid, site.id, min_sizediff=int(args.get("value")))
    ctx.manage.audit(ctx.gid, ctx.uid, "filter_minbytes", f"{site.id}")
    await ctx.reply(ctx.L("filter_updated", site=site.id))


async def h_filter_ignore(ctx: Ctx, args: ParsedArgs) -> None:
    site = _site(ctx, args)
    value = str(args.get("value")).strip()
    current = ctx.manage.get_filter(ctx.gid, site.id)
    users = list(current.ignore_users)
    if value.startswith("-"):
        target = value[1:].strip()
        users = [u for u in users if u != target]
    elif value in ("none", "无", "clear"):
        users = []
    elif value not in users:
        users.append(value)
    ctx.manage.set_filter(ctx.gid, site.id, ignore_users=users)
    ctx.manage.audit(ctx.gid, ctx.uid, "filter_ignore", f"{site.id}:{value}")
    await ctx.reply(ctx.L("filter_updated", site=site.id))


async def _send_change(ctx: Ctx, site, change, info, preview) -> None:
    """按本群图文设置输出变更（图文模式只发卡片图）。"""
    visual = ctx.manage.get_group_visual(ctx.gid)
    if visual:
        path = render_change_image(
            site,
            change,
            info,
            ctx.cfg.data_dir / "visual" / f"cmd_{site.id}_{change.revid}.png",
            max_lines=ctx.cfg.push.max_diff_lines,
        )
        await ctx.reply(image=str(path))
        return
    await ctx.reply(format_change(site, change, info, preview, ctx.cfg.push))


async def h_diff(ctx: Ctx, args: ParsedArgs) -> None:
    site = _site(ctx, args)
    title = args.get("title").strip()
    revid = None
    parts = title.split()
    if len(parts) > 1 and parts[-1].isdigit():
        revid = int(parts[-1])
        title = " ".join(parts[:-1])
    await ctx.reply(ctx.L("diff_querying", site=site.name, title=title))
    client = ctx.plugin.engine.client_for(site)
    try:
        revs = (
            await client.revisions_around(title, revid)
            if revid
            else await client.latest_revisions(title, 2)
        )
        if not revs:
            raise CommandError(ctx.L("diff_not_found"))
        newest = revs[-1]
        old = revs[-2] if len(revs) > 1 else {}
        change = change_from_rc(
            site.id,
            {
                "rcid": 0,
                "pageid": 0,
                "title": title,
                "ns": 0,
                "revid": newest["revid"],
                "old_revid": old.get("revid") or 0,
                "user": newest.get("user") or "",
                "timestamp": newest.get("timestamp") or "",
                "comment": newest.get("comment") or "",
                "newlen": newest.get("size") or 0,
                "oldlen": old.get("size") or 0,
            },
        )
        if change.type == "new":
            content = await client.page_content(change.revid)
            preview = (content or "").splitlines()[:8]
            await _send_change(ctx, site, change, None, preview)
        else:
            info = await client.compare(
                change.old_revid, change.revid, uselang=site.language
            )
            await _send_change(ctx, site, change, info, None)
    except MediaWikiError as exc:
        raise CommandError(ctx.L("diff_failed", error=exc)) from exc


async def h_recent(ctx: Ctx, args: ParsedArgs) -> None:
    site = _site(ctx, args)
    limit = int(args.get("limit") or 5)
    client = ctx.plugin.engine.client_for(site)
    try:
        raw = await client.recentchanges(limit=limit)
    except MediaWikiError as exc:
        raise CommandError(ctx.L("diff_failed", error=exc)) from exc
    changes = [change_from_rc(site.id, r) for r in raw]
    if not changes:
        await ctx.reply(ctx.L("recent_empty"))
        return
    visual = ctx.manage.get_group_visual(ctx.gid)
    if visual:
        path = render_digest_image(
            site,
            changes,
            ctx.cfg.data_dir / "visual" / f"cmd_{site.id}_recent_{int(time.time())}.png",
        )
        await ctx.reply(image=str(path))
        return
    await ctx.reply(format_digest(site, changes, ctx.cfg.push))


async def h_bot_status(ctx: Ctx, args: ParsedArgs) -> None:
    uptime = int(time.time() - ctx.plugin.started_at)
    h, rem = divmod(uptime, 3600)
    m, s = divmod(rem, 60)
    st = ctx.plugin.engine.stats
    visual = ctx.L("on") if ctx.manage.get_group_visual(ctx.gid) else ctx.L("off")
    bridge_src = len(ctx.plugin.store.list_repost_sources(enabled_only=True))
    token_state = ctx.L(
        "bot_status_bridge_token_on"
        if ctx.cfg.discord.token
        else "bot_status_bridge_token_off"
    )
    await ctx.reply(
        ctx.L(
            "bot_status",
            uptime=f"{h}小时{m}分{s}秒",
            sites=len(ctx.plugin.engine.clients),
            subs=len(ctx.manage.list_subs(ctx.gid)),
            visual=visual,
            polls=st["polls"],
            pushed=st["pushed"],
            errors=st["errors"],
        )
        + ctx.L("bot_status_bridge", sources=bridge_src, token=token_state)
    )


async def h_bridge_list(ctx: Ctx, args: ParsedArgs) -> None:
    views = ctx.bridge.list_bindings(ctx.gid)
    if not views:
        await ctx.reply(ctx.L("bridge_list_empty"))
        return
    lines = [ctx.L("bridge_list_header")]
    for v in views:
        state = ctx.L("bridge_state_on") if v.active else ctx.L("bridge_state_off")
        if not v.source.available:
            state = ctx.L("bridge_state_off")
        lines.append(
            ctx.L("bridge_list_item", name=v.source.name, id=v.source.id, state=state)
        )
    await ctx.reply("\n".join(lines))


async def h_bridge_add(ctx: Ctx, args: ParsedArgs) -> None:
    source, created = await ctx.bridge.add_binding(
        ctx.gid, ctx.uid, str(args.get("channel")), name=args.flag("name")
    )
    key = "bridge_added" if created else "bridge_rebound"
    await ctx.reply(ctx.L(key, name=source.name, id=source.id))


async def h_bridge_remove(ctx: Ctx, args: ParsedArgs) -> None:
    sid = await ctx.bridge.remove_binding(ctx.gid, ctx.uid, args.get("source"))
    await ctx.reply(ctx.L("bridge_removed", id=sid))


async def h_bridge_toggle(ctx: Ctx, args: ParsedArgs) -> None:
    enable = args.get("_action") == "enable"
    sid = await ctx.bridge.set_binding_active(
        ctx.gid, ctx.uid, args.get("source"), enable
    )
    await ctx.reply(ctx.L("bridge_enabled" if enable else "bridge_disabled", id=sid))


async def h_bridge_delete(ctx: Ctx, args: ParsedArgs) -> None:
    sid = await ctx.bridge.delete_source(ctx.uid, str(args.get("source")))
    await ctx.reply(ctx.L("bridge_deleted", id=sid))


_FILTER_READ_ACTIONS = frozenset({"show", "list", "显示"})


async def h_bridge_filter(ctx: Ctx, args: ParsedArgs) -> None:
    action = str(args.get("action") or "show").lower()
    source_q = args.flag("source") or args.get("source")

    if action in _FILTER_READ_ACTIONS:
        source = ctx.bridge.resolve_source(ctx.gid, source_q)
        flt = ctx.bridge.get_filter(ctx.gid, source.id)
        on = ctx.L("on") if flt.ignore_bots else ctx.L("off")
        lines = [
            ctx.L("bridge_filter_header", source=source.id),
            ctx.L(
                "bridge_filter_keywords",
                value="、".join(flt.keywords) if flt.keywords else ctx.L("bridge_all"),
            ),
            ctx.L(
                "bridge_filter_ignore",
                value="、".join(flt.ignore_authors) if flt.ignore_authors else "—",
            ),
            ctx.L("bridge_filter_bots", value=on),
        ]
        await ctx.reply("\n".join(lines))
        return

    # 写操作与 wiki /filter 一致：需要群管理员
    if ctx.plugin.user_level(ctx) < Level.GROUP_ADMIN:
        raise CommandError(
            ctx.L("cmd_no_permission", level=Level.GROUP_ADMIN.label)
        )

    value = str(args.get("value") or "").strip()
    if action in ("keyword", "keywords", "关键词"):
        if not value:
            raise CommandError("用法：/bridge filter keyword <词>（-词 移除，all 清空）")
        if value.startswith("-"):
            ctx.bridge.keyword_remove(ctx.gid, ctx.uid, source_q, value[1:].strip())
        elif value.lower() in ("all", "*", "clear", "清空"):
            ctx.bridge.keyword_clear(ctx.gid, ctx.uid, source_q)
        else:
            ctx.bridge.keyword_add(ctx.gid, ctx.uid, source_q, value)
    elif action in ("ignore", "作者"):
        if not value:
            raise CommandError("用法：/bridge filter ignore <作者>（-作者 移除）")
        if value.startswith("-"):
            ctx.bridge.ignore_remove(ctx.gid, ctx.uid, source_q, value[1:].strip())
        else:
            ctx.bridge.ignore_add(ctx.gid, ctx.uid, source_q, value)
    elif action in ("bots", "bot"):
        if value.lower() not in ("on", "off", "开", "关"):
            raise CommandError("用法：/bridge filter bots on|off")
        ctx.bridge.set_ignore_bots(
            ctx.gid, ctx.uid, source_q, value.lower() in ("on", "开")
        )
    else:
        raise CommandError(
            "用法：/bridge filter show|keyword <词>|ignore <作者>|bots on|off"
        )
    source = ctx.bridge.resolve_source(ctx.gid, source_q)
    await ctx.reply(ctx.L("bridge_filter_updated", source=source.id))


async def h_bot_visual(ctx: Ctx, args: ParsedArgs) -> None:
    value = (args.get("value") or "").lower()
    if value in ("on", "off", "开", "关"):
        on = value in ("on", "开")
        ctx.manage.set_group_visual(ctx.gid, ctx.uid, on)
        await ctx.reply(ctx.L("bot_visual_set", action=ctx.L("on" if on else "off")))
        return
    mode = ctx.L("on") if ctx.manage.get_group_visual(ctx.gid) else ctx.L("off")
    await ctx.reply(ctx.L("bot_visual_status", mode=mode))


async def h_bot_lang(ctx: Ctx, args: ParsedArgs) -> None:
    lang = ctx.manage.set_group_lang(ctx.gid, ctx.uid, str(args.get("value")))
    await ctx.reply(get_message(lang, "bot_lang_set", lang=lang))


async def h_bot_admin(ctx: Ctx, args: ParsedArgs) -> None:
    action = args.get("action").lower()
    qq = str(args.get("qq"))
    if action in ("add", "添加"):
        ctx.manage.add_group_admin(ctx.gid, ctx.uid, qq)
        await ctx.reply(ctx.L("bot_admin_added", qq=qq))
    elif action in ("remove", "del", "移除"):
        ctx.manage.remove_group_admin(ctx.gid, ctx.uid, qq)
        await ctx.reply(ctx.L("bot_admin_removed", qq=qq))
    else:
        raise CommandError("用法：/bot admin add|remove <QQ>")


async def h_bot_reload(ctx: Ctx, args: ParsedArgs) -> None:
    ctx.plugin.reload_config()
    ctx.manage.seed_from_config()
    await ctx.plugin.engine.sync()
    if ctx.plugin.repost is not None:
        # 重新读取 discord / rsshub 配置
        ctx.plugin.bridge.discord_cfg = ctx.cfg.discord
        ctx.plugin.bridge.rsshub_cfg = ctx.cfg.rsshub
        ctx.plugin.repost.cfg = ctx.cfg.discord
        ctx.plugin.repost.rsshub_cfg = ctx.cfg.rsshub
        await ctx.plugin.repost.sync()
    ctx.manage.audit(ctx.gid, ctx.uid, "reload")
    await ctx.reply(ctx.L("bot_reloaded"))


async def h_help(ctx: Ctx, args: ParsedArgs) -> None:
    level = ctx.plugin.user_level(ctx)
    raw = (args.get("name") or "").strip()
    if not raw:
        await ctx.reply(render_help(COMMANDS, level, subs_map=SUBS))
        return
    parts = raw.split()
    top = parts[0]
    if top in SUBS and len(parts) == 1:
        await ctx.reply(render_help(SUBS[top], level))
        return
    if top in SUBS and len(parts) > 1:
        await ctx.reply(render_help(SUBS[top], level, name=parts[1]))
        return
    await ctx.reply(render_help(COMMANDS, level, name=top))


# ======================================================================
# 指令表
# ======================================================================

_SITE_FLAG = FlagSpec("site", help="指定站点 ID（等价于 @站点ID）")

COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec(
        name="wiki",
        summary="站点订阅管理",
        args=(ArgSpec("sub", required=False, default="list"),),
        allow_extra=True,
    ),
    CommandSpec(
        name="page",
        summary="页面监视列表",
        args=(ArgSpec("sub", required=False, default="list"),),
        allow_extra=True,
    ),
    CommandSpec(
        name="filter",
        summary="过滤规则",
        args=(ArgSpec("sub", required=False, default="show"),),
        allow_extra=True,
    ),
    CommandSpec(
        name="diff",
        summary="查询页面差异",
        handler=h_diff,
        args=(ArgSpec("title", greedy=True),),
        flags=(_SITE_FLAG,),
    ),
    CommandSpec(
        name="recent",
        summary="查看最近变更",
        handler=h_recent,
        args=(ArgSpec("limit", required=False, type=int, default=5),),
        flags=(_SITE_FLAG,),
    ),
    CommandSpec(
        name="bot",
        summary="运行状态与群设置",
        args=(ArgSpec("sub", required=False, default="status"),),
        allow_extra=True,
    ),
    CommandSpec(
        name="bridge",
        summary="Discord 消息转载绑定",
        args=(ArgSpec("sub", required=False, default="list"),),
        allow_extra=True,
    ),
    CommandSpec(
        name="help",
        summary="查看指令帮助",
        handler=h_help,
        args=(ArgSpec("name", required=False, greedy=True),),
    ),
)

_ADMIN = Level.GROUP_ADMIN
_TOP = Level.SUPER_ADMIN

SUBS: dict[str, tuple[CommandSpec, ...]] = {
    "wiki": (
        CommandSpec(name="list", summary="列出本群订阅", handler=h_wiki_list),
        CommandSpec(
            name="add",
            summary="添加并订阅站点（立即开始监控）",
            handler=h_wiki_add,
            level=_ADMIN,
            args=(ArgSpec("url"),),
            flags=(
                FlagSpec("lang", help="语言，如 zh/en"),
                FlagSpec("name", help="显示名称"),
                FlagSpec("api", help="直接指定 api.php 地址"),
                FlagSpec("id", help="自定义站点 ID"),
                FlagSpec("user", help="只读凭证 用户名@机器人名:密码"),
                FlagSpec("interval", type=float, help="该站轮询间隔（秒）"),
                FlagSpec("digest", type=int, help="该站聚合阈值"),
            ),
            examples=("https://zh.doorsgame.wiki --lang zh",),
        ),
        CommandSpec(
            name="remove",
            summary="退订站点（停止本群推送）",
            handler=h_wiki_remove,
            level=_ADMIN,
            args=(ArgSpec("query", greedy=True),),
            examples=("doors_en",),
        ),
        CommandSpec(
            name="edit",
            summary="修改站点配置",
            handler=h_wiki_edit,
            level=_ADMIN,
            args=(ArgSpec("query", greedy=True),),
            flags=(
                FlagSpec("name"), FlagSpec("lang"), FlagSpec("tz"),
                FlagSpec("api"), FlagSpec("icon"),
                FlagSpec("user"), FlagSpec("auth"),
                FlagSpec("interval", type=float), FlagSpec("digest", type=int),
            ),
        ),
        CommandSpec(
            name="enable",
            summary="恢复监控",
            handler=h_wiki_toggle,
            level=_ADMIN,
            args=(ArgSpec("query", greedy=True),),
        ),
        CommandSpec(
            name="disable",
            summary="暂停监控",
            handler=h_wiki_toggle,
            level=_ADMIN,
            args=(ArgSpec("query", greedy=True),),
        ),
        CommandSpec(
            name="delete",
            summary="删除站点及其全部数据",
            handler=h_wiki_delete,
            level=_TOP,
            args=(ArgSpec("query", greedy=True),),
        ),
    ),
    "page": (
        CommandSpec(name="list", summary="查看监视列表", handler=h_page_list, flags=(_SITE_FLAG,)),
        CommandSpec(
            name="watch",
            summary="只推送指定页面",
            handler=h_page_watch,
            level=_ADMIN,
            args=(ArgSpec("title", greedy=True),),
            flags=(_SITE_FLAG,),
        ),
        CommandSpec(
            name="unwatch",
            summary="取消页面监视",
            handler=h_page_unwatch,
            level=_ADMIN,
            args=(ArgSpec("title", greedy=True),),
            flags=(_SITE_FLAG,),
        ),
        CommandSpec(
            name="clear",
            summary="清空监视列表",
            handler=h_page_clear,
            level=_ADMIN,
            flags=(_SITE_FLAG,),
        ),
    ),
    "filter": (
        CommandSpec(name="show", summary="显示过滤规则", handler=h_filter_show, flags=(_SITE_FLAG,)),
        CommandSpec(
            name="ns",
            summary="按命名空间过滤（all 取消）",
            handler=h_filter_ns,
            level=_ADMIN,
            args=(ArgSpec("value"),),
            flags=(_SITE_FLAG,),
            examples=("0,1",),
        ),
        CommandSpec(
            name="minbytes",
            summary="最小字节变化阈值",
            handler=h_filter_minbytes,
            level=_ADMIN,
            args=(ArgSpec("value", type=int),),
            flags=(_SITE_FLAG,),
        ),
        CommandSpec(
            name="ignore",
            summary="忽略编辑者（-名字 移除）",
            handler=h_filter_ignore,
            level=_ADMIN,
            args=(ArgSpec("value"),),
            flags=(_SITE_FLAG,),
        ),
    ),
    "bot": (
        CommandSpec(name="status", summary="运行状态", handler=h_bot_status),
        CommandSpec(
            name="visual",
            summary="本群图文开关",
            handler=h_bot_visual,
            level=_ADMIN,
            args=(ArgSpec("value", required=False),),
            examples=("on", "off"),
        ),
        CommandSpec(
            name="lang",
            summary="本群回复语言",
            handler=h_bot_lang,
            level=_ADMIN,
            args=(ArgSpec("value"),),
            examples=("zh", "en"),
        ),
        CommandSpec(
            name="admin",
            summary="群管理员白名单",
            handler=h_bot_admin,
            level=_TOP,
            args=(ArgSpec("action"), ArgSpec("qq")),
            examples=("add 10001",),
        ),
        CommandSpec(name="reload", summary="重载配置种子并同步任务", handler=h_bot_reload, level=_TOP),
    ),
    "bridge": (
        CommandSpec(name="list", summary="列出本群绑定", handler=h_bridge_list),
        CommandSpec(
            name="add",
            summary="绑定 Discord 频道或 Twitter 账号并开始转载",
            handler=h_bridge_add,
            level=_ADMIN,
            args=(ArgSpec("channel"),),
            flags=(FlagSpec("name", help="显示名称"),),
            examples=("123456789012345678", "@SomeAccount", "https://x.com/SomeAccount"),
        ),
        CommandSpec(
            name="remove",
            summary="本群取消绑定",
            handler=h_bridge_remove,
            level=_ADMIN,
            args=(ArgSpec("source", required=False, greedy=True),),
        ),
        CommandSpec(
            name="enable",
            summary="恢复接收",
            handler=h_bridge_toggle,
            level=_ADMIN,
            args=(ArgSpec("source", required=False, greedy=True),),
        ),
        CommandSpec(
            name="disable",
            summary="暂停接收",
            handler=h_bridge_toggle,
            level=_ADMIN,
            args=(ArgSpec("source", required=False, greedy=True),),
        ),
        CommandSpec(
            name="delete",
            summary="删除源（超级管理员）",
            handler=h_bridge_delete,
            level=_TOP,
            args=(ArgSpec("source", greedy=True),),
        ),
        CommandSpec(
            name="filter",
            summary="转载过滤规则（show 所有人；keyword/ignore/bots 需群管理员）",
            handler=h_bridge_filter,
            args=(
                ArgSpec("action", required=False, default="show"),
                ArgSpec("value", required=False, greedy=True),
            ),
            flags=(FlagSpec("source", help="指定源 ID"),),
            examples=("show", "keyword patch", "bots off"),
        ),
    ),
}


# ======================================================================
# 插件
# ======================================================================


class DiffBotPlugin(NcatBotPlugin):
    name = "diffbot"
    version = "0.2.0"
    author = "diffbot"
    description = "MediaWiki 变更监控与 Diff 推送"

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def on_load(self):
        self.cfg = self._load_config()
        self.store = Store(self.cfg.data_dir / "diffbot.db")
        self.store.init()
        self.manage = ManagementService(self.store, self.cfg)
        self.bridge = BridgeService(
            self.store, self.cfg, discord=self.cfg.discord, rsshub=self.cfg.rsshub
        )

        self.pusher = Pusher(self._send_text, self.cfg.push)
        await self.pusher.start()

        self.engine = MonitorEngine(
            self.cfg, self.store, self.pusher.push, manage=self.manage
        )
        self.manage.engine = self.engine
        await self.engine.start()

        self.repost = RepostEngine(
            self.store,
            self.pusher.push,
            self.cfg.discord,
            self.cfg.data_dir,
            rsshub_cfg=self.cfg.rsshub,
            lang_of=self.group_lang,
        )
        self.bridge.engine = self.repost
        await self.repost.start()

        self.started_at = time.time()
        LOG.info("DiffBot 已加载：站点 %s", [s.id for s in self.manage.list_sites()])

    async def on_close(self):
        await self.repost.stop()
        await self.engine.stop()
        await self.pusher.stop()
        self.store.close()
        LOG.info("DiffBot 已卸载")

    def reload_config(self) -> None:
        self.cfg = self._load_config()

    def _load_config(self) -> BotConfig:
        cfg_path = os.environ.get("DIFFBOT_CONFIG")
        if not cfg_path:
            for cand in (Path("config/diffbot.yaml"), Path(__file__).parent / "config.yaml"):
                if cand.exists():
                    cfg_path = str(cand)
                    break
        cfg = BotConfig.load(Path(cfg_path) if cfg_path else None)
        env_admins = os.environ.get("DIFFBOT_ADMINS")
        if env_admins:
            cfg.admins += [x.strip() for x in env_admins.split(",") if x.strip()]
        return cfg

    async def _send_text(self, group_id, text, image_path=None, **_kw):
        try:
            if image_path:
                await self.api.qq.send_group_image(int(group_id), str(image_path))
                if text:
                    await self.api.qq.send_group_text(int(group_id), text)
            elif text:
                await self.api.qq.send_group_text(int(group_id), text)
        except Exception as exc:  # noqa: BLE001 — 发送失败不能拖垮引擎
            LOG.error("发送到群 %s 失败: %s", group_id, exc)
            raise

    # ------------------------------------------------------------------
    # 权限
    # ------------------------------------------------------------------

    def group_lang(self, group_id: str) -> str:
        try:
            return self.manage.get_group_lang(group_id)
        except Exception:  # noqa: BLE001
            return "zh"

    @staticmethod
    def _sender_role(event) -> str:
        sender = getattr(event.data, "sender", None)
        role = getattr(sender, "role", None)
        if role is None and isinstance(sender, dict):
            role = sender.get("role")
        return str(role or "")

    def user_level(self, ctx: Ctx) -> Level:
        if ctx.uid and ctx.uid in self.cfg.admins:
            return Level.SUPER_ADMIN
        if not ctx.is_group:
            return Level.USER
        if ctx.uid in self.manage.get_group_admins(ctx.gid):
            return Level.GROUP_ADMIN
        if self._sender_role(ctx.event) in ("owner", "admin"):
            return Level.GROUP_ADMIN
        return Level.USER

    # ------------------------------------------------------------------
    # 分发
    # ------------------------------------------------------------------

    async def _dispatch(self, name: str, event) -> None:
        is_group = bool(getattr(event.data, "group_id", None))
        gid = str(getattr(event.data, "group_id", "") or "")
        uid = str(getattr(event.data, "user_id", "") or "")
        ctx = Ctx(plugin=self, event=event, gid=gid, uid=uid, is_group=is_group)
        raw = str(getattr(event.data, "raw_message", "") or "")

        if name == "status":  # 私聊 /status 映射到 /bot status
            name = "bot"
            raw = "bot status"

        spec = find_spec(COMMANDS, name)
        if spec is None:
            return
        try:
            level = self.user_level(ctx)
            args = parse_args(raw, spec, site_lookup=_site_lookup(ctx))

            if name in SUBS:
                sub = str(args.get("sub") or "").lower()
                sub_spec = find_spec(SUBS[name], sub)
                if sub_spec is None:
                    raise CommandError(
                        ctx.L("cmd_unknown", usage=usage_line(spec)),
                        usage=usage_line(spec),
                    )
                if sub_spec.level > level:
                    raise CommandError(
                        ctx.L("cmd_no_permission", level=sub_spec.level.label)
                    )
                sub_raw = " ".join([sub, *args.tokens[2:]])
                sub_args = parse_args(sub_raw, sub_spec, site_lookup=_site_lookup(ctx))
                sub_args.positionals["_action"] = sub
                await sub_spec.handler(ctx, sub_args)
                return

            if spec.level > level:
                raise CommandError(ctx.L("cmd_no_permission", level=spec.level.label))
            await spec.handler(ctx, args)
        except CommandError as exc:
            text = exc.user_message
            if exc.usage:
                text += "\n" + ctx.L("cmd_usage", usage=exc.usage)
            await ctx.reply(text)
        except Exception:  # noqa: BLE001 — 任何异常都要回执，不能静默
            LOG.exception("指令 %s 执行失败（群=%s 用户=%s）", name, gid, uid)
            await ctx.reply(ctx.L("cmd_error", message="内部错误，已记录日志"))


# ======================================================================
# 注册：单表 → ncatbot 装饰器（bare + "/" 双名）
# ======================================================================

_GROUP_NAMES = ("wiki", "page", "filter", "diff", "recent", "bot", "bridge", "help")
_PRIVATE_NAMES = ("help", "status", "bot")


def _make_entry(command: str):
    async def _entry(self, event):
        await self._dispatch(command, event)

    _entry.__name__ = "cmd_" + command
    return _entry


for _name in _GROUP_NAMES:
    setattr(
        DiffBotPlugin,
        "cmd_group_" + _name,
        registrar.qq.on_group_command(_name, "/" + _name)(_make_entry(_name)),
    )

for _name in _PRIVATE_NAMES:
    setattr(
        DiffBotPlugin,
        "cmd_private_" + _name,
        registrar.qq.on_private_command(_name, "/" + _name)(_make_entry(_name)),
    )
