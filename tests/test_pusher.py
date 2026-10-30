import asyncio

from diffbot_core.config import PushConfig
from diffbot_core.pusher import PushItem, Pusher, TokenBucket, split_message


def test_bucket_burst_then_empty():
    b = TokenBucket(5)
    assert b.try_acquire()
    for _ in range(4):
        assert b.try_acquire()
    assert not b.try_acquire()  # 突发额度耗尽


def test_split_message_short():
    assert split_message("hello", 100) == ["hello"]


def test_split_message_by_lines():
    text = "\n".join(f"line{i}" for i in range(50))
    parts = split_message(text, 50)
    assert all(len(p) <= 50 for p in parts)
    assert "\n".join(parts) == text


def test_split_message_single_long_line():
    parts = split_message("x" * 250, 100)
    assert sum(len(p) for p in parts) == 250
    assert all(len(p) <= 100 for p in parts)


async def test_pusher_queue_overflow_coalesce():
    cfg = PushConfig(max_pending=2, throttle_per_minute=600, max_message_chars=3000)
    sent = []
    pusher = Pusher(lambda g, t, image_path=None: _rec(sent, g, t, image_path), cfg)
    await pusher.start()
    # 不启动 worker，直接往队列塞满触发聚合
    q = pusher._queues.setdefault("g", asyncio.Queue(maxsize=cfg.max_pending))
    await q.put(PushItem(text="消息A"))
    await q.put(PushItem(text="消息B"))
    pusher._coalesce("g", q)
    item = q.get_nowait()
    assert "聚合" in item.text
    await pusher.stop()


async def test_pusher_end_to_end():
    cfg = PushConfig(max_pending=10, throttle_per_minute=6000, max_message_chars=3000)
    sent = []
    pusher = Pusher(lambda g, t, image_path=None: _rec(sent, g, t, image_path), cfg)
    await pusher.start()
    for i in range(3):
        await pusher.push("g1", f"消息{i}")
    # 等 worker 排空
    for _ in range(50):
        await asyncio.sleep(0.02)
        if all(q.empty() for q in pusher._queues.values()):
            break
    assert len(sent) == 3
    await pusher.stop()


async def _rec(sent, gid, text, image_path=None):
    await asyncio.sleep(0)
    sent.append((gid, text))
