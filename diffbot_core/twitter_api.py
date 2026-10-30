"""X/Twitter Guest GraphQL 客户端：解析用户名并拉取时间线。

无需用户登录；使用公开 Web Bearer + guest token。
queryId 会随前端发版变化，失效时更新常量即可。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import aiohttp

from .models import DiscordAttachment, DiscordMessage

LOG = logging.getLogger("diffbot.twitter")

BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
API = "https://x.com/i/api/graphql"
ACTIVATE = "https://api.twitter.com/1.1/guest/activate.json"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
QID_USER = "G3KGOASz96M-Qu0nwmGXNg"
QID_TWEETS = "E3opETHurmVJflFsUBVuUQ"

FEATURES_USER = {
    "hidden_profile_subscriptions_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "subscriptions_verification_info_is_identity_verified_enabled": True,
    "subscriptions_verification_info_verified_since_enabled": True,
    "highlights_tweets_tab_ui_enabled": True,
    "responsive_web_twitter_article_notes_tab_enabled": True,
    "subscriptions_feature_can_gift_premium": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
}

FEATURES_TWEETS = {
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}

_CREATED_FMT = "%a %b %d %H:%M:%S %z %Y"


class TwitterError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, unavailable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.unavailable = unavailable


def _parse_created(raw: str) -> str:
    if not raw:
        return ""
    try:
        dt = datetime.strptime(raw, _CREATED_FMT)
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        return raw


def _extract_tweets(payload: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def walk(o: Any, depth: int = 0) -> None:
        if depth > 40:
            return
        if isinstance(o, dict):
            node = o
            if o.get("__typename") == "TweetWithVisibilityResults" and "tweet" in o:
                node = o.get("tweet") or o
            if node.get("__typename") == "Tweet" and node.get("rest_id"):
                tid = str(node["rest_id"])
                if tid not in seen:
                    seen.add(tid)
                    found.append(node)
            if "tweet_results" in o and isinstance(o["tweet_results"], dict):
                walk(o["tweet_results"], depth + 1)
            for v in o.values():
                walk(v, depth + 1)
        elif isinstance(o, list):
            for i in o:
                walk(i, depth + 1)

    walk(payload)
    return found


def tweet_to_message(screen_name: str, tw: dict[str, Any]) -> DiscordMessage | None:
    tid = str(tw.get("rest_id") or "")
    if not tid:
        return None
    legacy = tw.get("legacy") or {}
    core = tw.get("core") or {}
    user_results = ((core.get("user_results") or {}).get("result") or {})
    ulegacy = user_results.get("legacy") or {}
    author = (
        ulegacy.get("name")
        or user_results.get("core", {}).get("screen_name")
        or screen_name
    )
    handle = ulegacy.get("screen_name") or screen_name
    text = str(legacy.get("full_text") or "").strip()
    attachments: list[DiscordAttachment] = []
    media = ((legacy.get("extended_entities") or {}).get("media")) or (
        (legacy.get("entities") or {}).get("media")
    ) or []
    for m in media:
        url = m.get("media_url_https") or m.get("media_url") or ""
        if url:
            attachments.append(
                DiscordAttachment(
                    id=str(m.get("id_str") or url),
                    filename=url.rsplit("/", 1)[-1],
                    url=url,
                    content_type="image/*",
                )
            )
    return DiscordMessage(
        id=tid,
        channel_id=handle,
        guild_id="",
        author_id=str(user_results.get("rest_id") or handle),
        author_name=author,
        content=text,
        timestamp=_parse_created(str(legacy.get("created_at") or "")),
        attachments=attachments,
        is_bot=bool(ulegacy.get("verified_type") == "Bot"),
        embeds_count=0,
    )


class TwitterClient:
    def __init__(
        self,
        *,
        timeout: float = 20.0,
        session: aiohttp.ClientSession | None = None,
        retries: int = 3,
    ) -> None:
        self._timeout = timeout
        self._session = session
        self._owns_session = session is None
        self.retries = retries
        self._guest_token = ""
        self._guest_at = 0.0

    async def _ensure_session(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout)
            )

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    def _headers(self, *, with_guest: bool = True) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {BEARER}",
            "User-Agent": UA,
            "content-type": "application/json",
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
        }
        if with_guest and self._guest_token:
            h["x-guest-token"] = self._guest_token
        return h

    async def _refresh_guest(self) -> None:
        await self._ensure_session()
        async with self._session.post(  # type: ignore[union-attr]
            ACTIVATE, headers=self._headers(with_guest=False)
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise TwitterError(f"guest activate HTTP {resp.status}: {body[:120]}", status=resp.status)
            data = await resp.json(content_type=None)
        tok = str(data.get("guest_token") or "")
        if not tok:
            raise TwitterError("guest activate 未返回 token")
        self._guest_token = tok
        self._guest_at = asyncio.get_event_loop().time()

    async def _gql(self, qid: str, op: str, variables: dict[str, Any], features: dict[str, Any]) -> dict[str, Any]:
        await self._ensure_session()
        if not self._guest_token:
            await self._refresh_guest()
        params = {
            "variables": json.dumps(variables, separators=(",", ":")),
            "features": json.dumps(features, separators=(",", ":")),
        }
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                async with self._session.get(  # type: ignore[union-attr]
                    f"{API}/{qid}/{op}",
                    params=params,
                    headers=self._headers(),
                ) as resp:
                    if resp.status in (401, 403) and attempt == 0:
                        await self._refresh_guest()
                        continue
                    if resp.status == 429:
                        await asyncio.sleep(2.0 * (attempt + 1))
                        continue
                    if resp.status >= 500:
                        await asyncio.sleep(0.5 * 2**attempt)
                        continue
                    if resp.status >= 400:
                        body = await resp.text()
                        raise TwitterError(
                            f"HTTP {resp.status}: {body[:160]}",
                            status=resp.status,
                            unavailable=resp.status in (401, 403, 404),
                        )
                    return await resp.json(content_type=None)
            except TwitterError:
                raise
            except aiohttp.ClientError as e:
                last = e
                await asyncio.sleep(0.5 * 2**attempt)
                continue
        raise TwitterError(f"GraphQL 请求失败: {last}")

    async def resolve_user(self, screen_name: str) -> tuple[str, str]:
        """返回 (user_id, display_name)。"""
        name = screen_name.lstrip("@")
        data = await self._gql(
            QID_USER,
            "UserByScreenName",
            {"screen_name": name, "withSafetyModeUserFields": True},
            FEATURES_USER,
        )
        result = (
            ((data.get("data") or {}).get("user") or {}).get("result") or {}
        )
        uid = str(result.get("rest_id") or "")
        if not uid:
            raise TwitterError(f"用户 @{name} 不存在", status=404, unavailable=True)
        core = result.get("core") or {}
        display = str((core.get("screen_name") or name))
        legacy_name = ((result.get("legacy") or {}).get("name")) or display
        return uid, str(legacy_name)

    async def list_tweets(
        self, screen_name: str, *, after: str | None = None, limit: int = 20
    ) -> list[DiscordMessage]:
        uid, display = await self.resolve_user(screen_name)
        data = await self._gql(
            QID_TWEETS,
            "UserTweets",
            {
                "userId": uid,
                "count": max(5, min(limit, 40)),
                "includePromotedContent": True,
                "withQuickPromoteEligibilityTweetFields": True,
                "withVoice": True,
                "withV2Timeline": True,
            },
            FEATURES_TWEETS,
        )
        raw = _extract_tweets(data)
        msgs: list[DiscordMessage] = []
        for tw in raw:
            m = tweet_to_message(screen_name, tw)
            if m is None:
                continue
            if after and after.isdigit() and m.id.isdigit() and int(m.id) <= int(after):
                continue
            msgs.append(m)
        msgs.sort(key=lambda m: int(m.id) if m.id.isdigit() else 0)
        return msgs

    async def download_image(
        self, url: str, dest: Any, *, max_bytes: int = 8 * 1024 * 1024
    ):
        from pathlib import Path

        await self._ensure_session()
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with self._session.get(  # type: ignore[union-attr]
                url, headers={"User-Agent": UA}
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
            LOG.warning("推特图片下载失败 %s: %s", url, exc)
            return None
