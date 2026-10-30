"""配置：站点、订阅、轮询与推送参数。支持 YAML 加载 + 环境变量覆盖。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# 维基家族域名，用于从 URL 推导站点 id（zh.wikipedia.org -> zh_wikipedia）
WIKI_FAMILIES = (
    "wikipedia",
    "wiktionary",
    "wikibooks",
    "wikiquote",
    "wikisource",
    "wikiversity",
    "wikivoyage",
    "wikidata",
    "mediawiki",
)


def site_id_from_url(url: str) -> str:
    """从站点 URL 推导默认 id。"""
    host = re.sub(r"^https?://", "", url.strip()).split("/")[0].lower()
    labels = host.split(".")
    if len(labels) >= 3 and labels[1] in WIKI_FAMILIES:
        return f"{labels[0]}_{labels[1]}"
    return host.replace(".", "_")


@dataclass
class WikiSite:
    """单个 MediaWiki 站点配置。"""

    id: str
    name: str
    api_url: str
    base_url: str
    language: str = "en"
    timezone: str = "UTC"
    icon: str = "🌐"
    enabled: bool = True
    # BotPassword 只读凭证（仅用于登录读取 API；绝不用于任何编辑操作）
    auth_username: str = ""
    auth_password: str = ""
    # 站点级轮询覆盖（高活跃站点可缩短间隔/提高阈值；None 用全局值）
    poll_interval_override: float | None = None
    digest_threshold_override: int | None = None


@dataclass
class SiteFilter:
    """某群对某站点的过滤规则。namespaces=None 表示不限制。"""

    namespaces: list[int] | None = None
    ignore_users: list[str] = field(default_factory=list)
    min_sizediff: int = 0

    def passes(self, change: "Any") -> bool:
        if self.namespaces is not None and change.ns not in self.namespaces:
            return False
        if change.user in self.ignore_users:
            return False
        if abs(change.sizediff) < self.min_sizediff:
            return False
        return True


@dataclass
class GroupSub:
    """群组订阅：订阅哪些站点、各自过滤器。"""

    group_id: str
    sites: list[str] = field(default_factory=list)
    filters: dict[str, SiteFilter] = field(default_factory=dict)  # site_id -> filter


@dataclass
class PushConfig:
    throttle_per_minute: int = 5
    max_pending: int = 20
    max_message_chars: int = 3000
    max_diff_lines: int = 12
    visual: bool = False  # 图文模式：diff 渲染为图片发送


@dataclass
class PollConfig:
    interval: float = 30.0
    jitter: float = 5.0
    batch_limit: int = 50
    timeout: float = 15.0
    max_diff_fetch: int = 10  # 单批最多拉取的 diff 数，防止突发轰炸
    digest_threshold: int = 6  # 单批超过此数改为聚合摘要


@dataclass
class DiscordConfig:
    """Discord 转载全局配置。"""

    token: str = ""
    poll_interval: float = 20.0
    jitter: float = 3.0
    batch_limit: int = 50
    timeout: float = 15.0
    max_image_bytes: int = 8 * 1024 * 1024


@dataclass
class RsshubConfig:
    """推特等经 RSSHub 转载的配置。"""

    base_url: str = "https://rsshub.app"
    poll_interval: float = 60.0
    jitter: float = 5.0
    timeout: float = 15.0
    batch_limit: int = 20
    max_image_bytes: int = 8 * 1024 * 1024


@dataclass
class BotConfig:
    """全局配置。"""

    sites: dict[str, WikiSite] = field(default_factory=dict)
    subscriptions: list[GroupSub] = field(default_factory=list)
    poll: PollConfig = field(default_factory=PollConfig)
    push: PushConfig = field(default_factory=PushConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    rsshub: RsshubConfig = field(default_factory=RsshubConfig)
    admins: list[str] = field(default_factory=list)
    data_dir: Path = field(default_factory=lambda: Path("./data"))
    user_agent: str = "DiffBot/0.1 (MediaWiki change notifier)"

    @classmethod
    def load(cls, path: Path | str | None = None) -> "BotConfig":
        """从 YAML 加载；文件不存在时使用内置默认值。

        环境变量：DIFFBOT_DATA_DIR 覆盖数据目录。
        """
        data: dict[str, Any] = {}
        if path and Path(path).exists():
            data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

        cfg = cls()

        # 站点
        for sid, s in (data.get("sites") or {}).items():
            base = (s.get("base_url") or "").rstrip("/")
            api_url = s.get("api_url") or (f"{base}/w/api.php" if base else "")
            cfg.sites[str(sid)] = WikiSite(
                id=str(sid),
                name=s.get("name") or str(sid),
                api_url=api_url,
                base_url=base,
                language=s.get("language", "en"),
                timezone=s.get("timezone", "UTC"),
                icon=s.get("icon", "🌐"),
                enabled=bool(s.get("enabled", True)),
                auth_username=str(s.get("auth_username") or ""),
                auth_password=str(s.get("auth_password") or ""),
                poll_interval_override=(float(s["poll_interval"]) if s.get("poll_interval") is not None else None),
                digest_threshold_override=(int(s["digest_threshold"]) if s.get("digest_threshold") is not None else None),
            )

        # 订阅
        for gid, sub in (data.get("subscriptions") or {}).items():
            filters: dict[str, SiteFilter] = {}
            for fsid, f in (sub.get("filters") or {}).items():
                filters[str(fsid)] = SiteFilter(
                    namespaces=f.get("namespaces"),
                    ignore_users=list(f.get("ignore_users") or []),
                    min_sizediff=int(f.get("min_sizediff", 0)),
                )
            cfg.subscriptions.append(
                GroupSub(
                    group_id=str(gid),
                    sites=[str(x) for x in (sub.get("sites") or [])],
                    filters=filters,
                )
            )

        # 轮询/推送参数
        poll = data.get("poll") or {}
        cfg.poll = PollConfig(
            interval=float(poll.get("interval", 30.0)),
            jitter=float(poll.get("jitter", 5.0)),
            batch_limit=int(poll.get("batch_limit", 50)),
            timeout=float(poll.get("timeout", 15.0)),
            max_diff_fetch=int(poll.get("max_diff_fetch", 10)),
            digest_threshold=int(poll.get("digest_threshold", 6)),
        )
        push = data.get("push") or {}
        cfg.push = PushConfig(
            throttle_per_minute=int(push.get("throttle_per_minute", 5)),
            max_pending=int(push.get("max_pending", 20)),
            max_message_chars=int(push.get("max_message_chars", 3000)),
            max_diff_lines=int(push.get("max_diff_lines", 12)),
            visual=bool(push.get("visual", False)),
        )
        discord = data.get("discord") or {}
        cfg.discord = DiscordConfig(
            token=str(
                os.environ.get("DIFFBOT_DISCORD_TOKEN")
                or discord.get("token")
                or ""
            ).strip(),
            poll_interval=float(discord.get("poll_interval", 20.0)),
            jitter=float(discord.get("jitter", 3.0)),
            batch_limit=int(discord.get("batch_limit", 50)),
            timeout=float(discord.get("timeout", 15.0)),
            max_image_bytes=int(discord.get("max_image_bytes", 8 * 1024 * 1024)),
        )
        rsshub = data.get("rsshub") or {}
        cfg.rsshub = RsshubConfig(
            base_url=str(
                os.environ.get("DIFFBOT_RSSHUB_URL")
                or rsshub.get("base_url")
                or "https://rsshub.app"
            ).strip(),
            poll_interval=float(rsshub.get("poll_interval", 60.0)),
            jitter=float(rsshub.get("jitter", 5.0)),
            timeout=float(rsshub.get("timeout", 15.0)),
            batch_limit=int(rsshub.get("batch_limit", 20)),
            max_image_bytes=int(rsshub.get("max_image_bytes", 8 * 1024 * 1024)),
        )

        cfg.admins = [str(x) for x in (data.get("admins") or [])]
        cfg.data_dir = Path(os.environ.get("DIFFBOT_DATA_DIR") or data.get("data_dir") or "./data")
        cfg.user_agent = data.get("user_agent") or cfg.user_agent
        return cfg
