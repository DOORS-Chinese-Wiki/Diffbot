"""MediaWiki Action API 客户端（aiohttp 异步实现）。

- 统一 format=json & formatversion=2
- 429/5xx 指数退避重试
- compare：difftype=inline 为主，unified 兜底
"""

from __future__ import annotations

import asyncio
import html
import re
from typing import Any

import aiohttp

from .models import DiffInfo, DiffLine

DEFAULT_UA = "DiffBot/0.1 (MediaWiki change notifier)"


class MediaWikiError(RuntimeError):
    """API 请求最终失败。"""


# inline diff：<div class="mw-diff-inline-added|deleted|changed|context|header">…</div>
_INLINE_RE = re.compile(
    r'<div\s+class="mw-diff-inline-(added|deleted|changed|context|header)[^"]*"[^>]*>(.*?)</div>',
    re.S,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(fragment: str) -> str:
    """HTML 片段 -> 纯文本（保留 <br> 为换行，实体解码）。"""
    fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    fragment = _TAG_RE.sub("", fragment)
    return html.unescape(fragment).strip()


def parse_inline_diff(body: str) -> list[DiffLine]:
    """解析 difftype=inline 的 HTML 响应为差异行列表。"""
    out: list[DiffLine] = []
    for kind, frag in _INLINE_RE.findall(body):
        if kind == "header":
            continue
        if kind == "changed":
            # changed 容器内含 <del>（删除文本）与 <ins>（新增文本）
            del_text = _clean(re.sub(r"<ins\b.*?</ins>", "", frag, flags=re.S | re.I))
            add_text = _clean(re.sub(r"<del\b.*?</del>", "", frag, flags=re.S | re.I))
            if del_text:
                out.append(DiffLine("del", del_text))
            if add_text:
                out.append(DiffLine("add", add_text))
            continue
        text = _clean(frag)
        if not text:
            continue
        if kind == "added":
            out.append(DiffLine("add", text))
        elif kind == "deleted":
            out.append(DiffLine("del", text))
        else:
            out.append(DiffLine("ctx", text))
    return out


def parse_unified_diff(body: str) -> list[DiffLine]:
    """解析 difftype=unified 的纯文本响应（兜底方案）。"""
    out: list[DiffLine] = []
    for raw in body.splitlines():
        if raw.startswith(("@@", "+++", "---", "index ")):
            continue
        if raw.startswith("+"):
            out.append(DiffLine("add", raw[1:].strip()))
        elif raw.startswith("-"):
            out.append(DiffLine("del", raw[1:].strip()))
        elif raw.startswith(" "):
            out.append(DiffLine("ctx", raw[1:].strip()))
    return out


# 表格 diff（MediaWiki 默认格式）：diff-deletedline / diff-addedline 单元格
_TABLE_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_TD_RE = re.compile(r"<td[^>]*class=\"([^\"]*)\"[^>]*>(.*?)</td>", re.S)


def parse_table_diff(body: str) -> list[DiffLine]:
    """解析 difftype=table 的 HTML 表格响应（多数站点的默认格式）。"""
    out: list[DiffLine] = []
    for row in _TABLE_ROW_RE.findall(body):
        cells = _TD_RE.findall(row)
        for cls, frag in cells:
            if "diff-deletedline" in cls:
                text = _clean(frag)
                if text and not text.endswith("：") or "diffchange" in frag:
                    out.append(DiffLine("del", text))
            elif "diff-addedline" in cls:
                text = _clean(frag)
                if text:
                    out.append(DiffLine("add", text))
            # diff-context / diff-lineno 跳过
    return out


class MediaWikiClient:
    """单站点 API 客户端。支持 async with 用法；可注入外部 session。"""

    def __init__(
        self,
        api_url: str,
        *,
        timeout: float = 15.0,
        user_agent: str = DEFAULT_UA,
        session: aiohttp.ClientSession | None = None,
        retries: int = 3,
        auth_username: str | None = None,
        auth_password: str | None = None,
    ) -> None:
        self.api_url = api_url
        self._timeout = timeout
        self._ua = user_agent
        self._session = session
        self._owns_session = session is None
        self.retries = retries
        # BotPassword 凭证（只用于登录维持读取权限；本客户端绝不调用任何写接口）
        self._auth_username = auth_username
        self._auth_password = auth_password
        self._logged_in = False

    async def __aenter__(self) -> "MediaWikiClient":
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

    async def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        """GET api.php，自动重试；API error 响应抛 MediaWikiError。"""
        await self._ensure_session()
        query = {"format": "json", "formatversion": "2", **params}
        headers = {"User-Agent": self._ua}
        last_exc: Exception | None = None
        for attempt in range(self.retries):
            try:
                async with self._session.get(  # type: ignore[union-attr]
                    self.api_url, params=query, headers=headers
                ) as resp:
                    if resp.status == 429 or resp.status >= 500:
                        last_exc = MediaWikiError(f"HTTP {resp.status}")
                        await asyncio.sleep(0.5 * 2**attempt)
                        continue
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
            except aiohttp.ClientError as e:
                last_exc = e
                await asyncio.sleep(0.5 * 2**attempt)
                continue
            if isinstance(data, dict) and data.get("error"):
                err = data["error"]
                raise MediaWikiError(
                    f"{err.get('code', 'unknown')}: {err.get('info', '')}"
                )
            return data
        raise MediaWikiError(f"请求失败（重试 {self.retries} 次）: {last_exc}")

    async def login(self) -> None:
        """BotPassword 登录（action=login + login token），仅用于获得读取权限。

        本客户端的所有请求都是只读查询（query/compare），不会调用任何编辑类
        action（edit/submit/move/...）。BotPassword 本身也应只授予读取权限。
        """
        if not (self._auth_username and self._auth_password):
            return
        await self._ensure_session()
        # 1. 取 login token
        data = await self._get({"action": "query", "meta": "tokens", "type": "login"})
        token = data.get("query", {}).get("tokens", {}).get("logintoken", "")
        if not token:
            raise MediaWikiError("登录失败：拿不到 login token")
        # 2. POST 登录（POST 必须走 body，MediaWiki 要求）
        async with self._session.post(  # type: ignore[union-attr]
            self.api_url,
            data={
                "format": "json",
                "formatversion": "2",
                "action": "login",
                "lgname": self._auth_username,
                "lgpassword": self._auth_password,
                "lgtoken": token,
            },
            headers={"User-Agent": self._ua},
        ) as resp:
            resp.raise_for_status()
            result = await resp.json(content_type=None)
        lr = result.get("login", {}).get("result", "")
        if lr != "Success":
            reason = result.get("login", {}).get("reason", lr)
            raise MediaWikiError(f"登录失败: {reason}")
        self._logged_in = True

    # ------------------------------------------------------------------
    # 高层接口
    # ------------------------------------------------------------------

    async def recentchanges(
        self,
        *,
        newer_than: str | None = None,
        limit: int = 50,
        namespaces: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        """拉取最近变更。

        newer_than=None 时按 rcdir=older 取最新 limit 条（手动查询用）；
        给定 ISO 时间戳时按 rcdir=newer 增量拉取（轮询用）。
        """
        params: dict[str, Any] = {
            "action": "query",
            "list": "recentchanges",
            "rcprop": "title|user|timestamp|comment|sizes|ids|flags",
            "rclimit": limit,
            "rctype": "edit|new",
        }
        if newer_than:
            params["rcstart"] = newer_than
            params["rcdir"] = "newer"
        else:
            params["rcdir"] = "older"
        if namespaces:
            params["rcnamespace"] = "|".join(str(n) for n in namespaces)
        data = await self._get(params)
        return (data.get("query") or {}).get("recentchanges") or []

    async def compare(
        self, fromrev: int, torev: int, *, uselang: str | None = None
    ) -> DiffInfo:
        """获取两个修订版本的差异；inline 解析为空时 unified 兜底。"""
        params: dict[str, Any] = {
            "action": "compare",
            "fromrev": fromrev,
            "torev": torev,
            "difftype": "inline",
        }
        if uselang:
            params["uselang"] = uselang
        data = await self._get(params)
        cmp = data.get("compare") or {}
        raw_body = cmp.get("body") or ""
        # 三级解析：inline -> table（部分站点忽略 difftype，总返回默认表格）-> unified
        lines = parse_inline_diff(raw_body) or parse_table_diff(raw_body)
        if not lines:
            try:
                uni = await self._get(
                    {"action": "compare", "fromrev": fromrev, "torev": torev,
                     "difftype": "unified", **({"uselang": uselang} if uselang else {})}
                )
                uni_body = (uni.get("compare") or {}).get("body") or ""
                lines = parse_unified_diff(uni_body) or parse_table_diff(uni_body)
            except MediaWikiError:
                pass
        return DiffInfo(
            lines=lines,
            fromrevid=int(cmp.get("fromrevid") or fromrev),
            torevid=int(cmp.get("torevid") or torev),
        )

    async def latest_revisions(self, title: str, limit: int = 2) -> list[dict[str, Any]]:
        """查询页面最近 limit 个修订（旧→新顺序）。"""
        data = await self._get(
            {
                "action": "query",
                "prop": "revisions",
                "titles": title,
                "rvprop": "ids|timestamp|user|comment|size|flags",
                "rvlimit": limit,
                "rvdir": "older",
            }
        )
        pages = (data.get("query") or {}).get("pages") or []
        if not pages:
            return []
        return list(reversed(pages[0].get("revisions") or []))

    async def revisions_around(self, title: str, revid: int) -> list[dict[str, Any]]:
        """查询 revid 及其前一个修订（用于手动 diff 指定版本）。"""
        data = await self._get(
            {
                "action": "query",
                "prop": "revisions",
                "titles": title,
                "rvprop": "ids|timestamp|user|comment|size|flags",
                "rvlimit": 2,
                "rvstart": str(revid),
                "rvdir": "older",
            }
        )
        pages = (data.get("query") or {}).get("pages") or []
        if not pages:
            return []
        return list(reversed(pages[0].get("revisions") or []))

    async def page_content(self, revid: int) -> str | None:
        """获取指定修订的 wikitext 内容（新页面预览用）。"""
        data = await self._get(
            {
                "action": "query",
                "prop": "revisions",
                "revids": revid,
                "rvprop": "content",
                "rvslots": "main",
            }
        )
        pages = (data.get("query") or {}).get("pages") or []
        if not pages:
            return None
        revs = pages[0].get("revisions") or []
        if not revs:
            return None
        slots = revs[0].get("slots") or {}
        return (slots.get("main") or {}).get("content")

    async def server_time(self) -> str | None:
        """获取站点当前时间（ISO8601），首次游标初始化用。"""
        data = await self._get({"action": "query", "meta": "siteinfo"})
        return ((data.get("query") or {}).get("general") or {}).get("time")


def change_from_rc(site_id: str, rc: dict[str, Any]) -> "Any":
    """recentchanges 原始条目 -> Change 模型。延迟导入避免循环依赖。"""
    from .models import Change

    old_revid = int(rc.get("old_revid") or 0)
    new_len = int(rc.get("newlen") or 0)
    old_len = int(rc.get("oldlen") or 0)
    return Change(
        site_id=site_id,
        rc_id=int(rc.get("rcid") or 0),
        page_id=int(rc.get("pageid") or 0),
        title=rc.get("title") or "",
        ns=int(rc.get("ns") or 0),
        revid=int(rc.get("revid") or 0),
        old_revid=old_revid,
        user=rc.get("user") or "",
        timestamp=rc.get("timestamp") or "",
        comment=rc.get("comment") or "",
        new_len=new_len,
        old_len=old_len,
        sizediff=new_len - old_len,
        type="new" if (rc.get("type") == "new" or old_revid == 0) else "edit",
        minor="minor" in (rc.get("flags") or ""),
    )
