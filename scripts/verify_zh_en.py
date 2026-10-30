#!/usr/bin/env python3
"""集成验证：对真实 MediaWiki 站点执行 recentchanges -> compare -> 格式化，打印消息文本。

用法：
    python scripts/verify_zh_en.py                          # 默认 zh/en 维基百科
    python scripts/verify_zh_en.py --api-url https://zh.wikipedia.org/w/api.php --lang zh
    python scripts/verify_zh_en.py --limit 3

退出码：至少一个站点成功为 0。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diffbot_core.config import PushConfig, WikiSite
from diffbot_core.formatter import format_change, format_digest
from diffbot_core.mw_api import MediaWikiClient, MediaWikiError, change_from_rc

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

DEFAULT_SITES = [
    ("zh_wikipedia", "https://zh.wikipedia.org/w/api.php", "zh", "Asia/Shanghai"),
    ("en_wikipedia", "https://en.wikipedia.org/w/api.php", "en", "UTC"),
]


async def verify_site(site: WikiSite, limit: int) -> None:
    print(f"\n{'=' * 60}")
    print(f"站点：{site.name}（{site.id}，{site.language}）")
    print("=" * 60)
    async with MediaWikiClient(site.api_url, timeout=20.0) as client:
        server_time = await client.server_time()
        print(f"服务器时间：{server_time}")

        raw = await client.recentchanges(limit=limit)
        changes = [change_from_rc(site.id, r) for r in raw]
        if not changes:
            print("（最近没有变更）")
            return

        # 聚合摘要
        print("\n--- 聚合摘要（/recent 输出样式）---")
        print(format_digest(site, changes, PushConfig()))

        # 逐条 diff
        for change in changes[:3]:
            print(f"\n--- 变更消息（rc_id={change.rc_id}, {change.type}）---")
            try:
                if change.type == "new":
                    content = await client.page_content(change.revid)
                    preview = (content or "").splitlines()[:8]
                    print(format_change(site, change, None, preview, PushConfig()))
                else:
                    info = await client.compare(
                        change.old_revid, change.revid, uselang=site.language
                    )
                    print(format_change(site, change, info, None, PushConfig()))
            except MediaWikiError as exc:
                print(f"⚠ diff 获取失败：{exc}")


async def amain() -> int:
    parser = argparse.ArgumentParser(description="DiffBot 集成验证")
    parser.add_argument("--api-url", help="单个站点 API 地址")
    parser.add_argument("--lang", default="zh", help="站点语言（配合 --api-url）")
    parser.add_argument("--limit", type=int, default=5, help="拉取变更条数")
    args = parser.parse_args()

    from urllib.parse import urlparse

    def origin(url: str) -> str:
        u = urlparse(url)
        return f"{u.scheme}://{u.netloc}"

    sites: list[WikiSite] = []
    if args.api_url:
        sites.append(
            WikiSite(id="custom", name="自定义站点", api_url=args.api_url,
                     base_url=origin(args.api_url), language=args.lang)
        )
    else:
        sites = [
            WikiSite(id=sid, name=sid, api_url=api, base_url=origin(api),
                     language=lang, timezone=tz)
            for sid, api, lang, tz in DEFAULT_SITES
        ]

    ok = False
    for site in sites:
        try:
            await verify_site(site, args.limit)
            ok = True
        except Exception as exc:  # noqa: BLE001
            print(f"\n❌ {site.id} 验证失败：{exc}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
