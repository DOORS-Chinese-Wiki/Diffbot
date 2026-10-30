"""DiffBot 宿主入口。

Linux（容器）路径：asyncio + 信号处理，支持 docker stop 优雅退出。
Windows 开发路径：直接 bot.run()。
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys


def _install_uvloop() -> None:
    """Linux 上启用 uvloop 加速事件循环（可选依赖，失败不致命）。"""
    if not sys.platform.startswith("linux") or os.environ.get("DIFFBOT_NO_UVLOOP"):
        return
    try:
        import uvloop

        uvloop.install()
    except ImportError:
        pass


def _tune_reconnect() -> None:
    """提高 WebSocket 重连尝试上限（默认 5 次约 15s，NapCat 重启通常需 20-30s）。"""
    try:
        import ncatbot.adapter.napcat.connection.websocket as ws_mod

        ws_mod._MAX_RECONNECT_ATTEMPTS = 30
    except Exception:
        pass


async def _run_posix() -> None:
    _tune_reconnect()
    from ncatbot.app import BotClient

    bot = BotClient()
    await bot.run_async()

    loop = asyncio.get_running_loop()
    sig_stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, sig_stop.set)

    sig_task = asyncio.create_task(sig_stop.wait())
    listen_task = getattr(bot, "_listen_task", None)

    tasks = [sig_task]
    if listen_task is not None:
        tasks.append(listen_task)

    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    if sig_task in done:
        # 外部信号要求停机（如 systemctl stop / docker stop）
        if listen_task and not listen_task.done():
            listen_task.cancel()
        await bot.shutdown()
        sys.exit(0)
    else:
        # listen_task 提前结束（如适配器断连、重连耗尽或异常崩溃）
        sig_task.cancel()
        await bot.shutdown()
        # 非外部信号触发的监听终止，以退出码 1 退出，让 systemd / 容器管理器自动拉起重启
        sys.exit(1)


def main() -> None:
    _install_uvloop()
    if os.name == "posix":
        asyncio.run(_run_posix())
    else:
        _tune_reconnect()
        from ncatbot.app import BotClient

        BotClient().run()


if __name__ == "__main__":
    main()
