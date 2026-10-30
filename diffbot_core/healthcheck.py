"""容器 healthcheck：检查心跳文件新鲜度与 SQLite 可打开。

用法：python -m diffbot_core.healthcheck
退出码 0 = 健康，1 = 异常。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path


def main() -> int:
    data_dir = Path(os.environ.get("DIFFBOT_DATA_DIR", "./data"))
    hb = data_dir / ".heartbeat"
    max_age = int(os.environ.get("DIFFBOT_HEALTHCHECK_MAX_AGE", "300"))

    if not hb.exists():
        print(f"healthcheck: 心跳文件不存在 {hb}", file=sys.stderr)
        return 1
    age = time.time() - hb.stat().st_mtime
    if age > max_age:
        print(f"healthcheck: 心跳过期 {age:.0f}s > {max_age}s", file=sys.stderr)
        return 1

    db = data_dir / "diffbot.db"
    if db.exists():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            conn.execute("SELECT 1")
            conn.close()
        except sqlite3.Error as exc:
            print(f"healthcheck: SQLite 异常 {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
