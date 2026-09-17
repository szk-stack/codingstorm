"""事件总线测试。"""

import asyncio

from codingstorm.events import EventBus


def test_subscribe_and_publish():
    bus = EventBus()
    q = bus.subscribe("t1")
    assert bus.subscriber_count("t1") == 1

    bus.publish("t1", {"type": "assistant"})
    assert q.get_nowait() == {"type": "assistant"}


def test_publish_to_unknown_channel_is_noop():
    bus = EventBus()
    assert bus.publish("nobody", {"x": 1}) == 0


def test_multiple_subscribers_all_receive():
    bus = EventBus()
    a, b = bus.subscribe("t1"), bus.subscribe("t1")
    bus.publish("t1", {"n": 1})
    assert a.get_nowait() == b.get_nowait() == {"n": 1}


def test_channels_are_isolated():
    bus = EventBus()
    a, b = bus.subscribe("t1"), bus.subscribe("t2")
    bus.publish("t1", {"for": "t1"})
    assert a.qsize() == 1
    assert b.qsize() == 0


def test_slow_subscriber_drops_instead_of_blocking():
    """队列满了就丢 —— 实时流不值得为它阻塞执行器。"""
    bus = EventBus(maxsize=3)
    q = bus.subscribe("t1")

    for i in range(3):
        assert bus.publish("t1", {"n": i}) == 0
    # 第 4 条开始丢
    assert bus.publish("t1", {"n": 99}) == 1
    assert q.qsize() == 3
    assert bus.dropped_total == 1


def test_unsubscribe():
    bus = EventBus()
    q = bus.subscribe("t1")
    bus.unsubscribe("t1", q)
    assert bus.subscriber_count("t1") == 0
    assert bus.publish("t1", {}) == 0


def test_unsubscribe_unknown_is_safe():
    bus = EventBus()
    bus.unsubscribe("nope", asyncio.Queue())


def test_one_slow_subscriber_does_not_affect_others():
    """一个订阅者卡住，不能拖累其他订阅者。"""
    bus = EventBus(maxsize=2)
    slow = bus.subscribe("t1")
    fast = bus.subscribe("t1")

    for i in range(2):
        bus.publish("t1", {"n": i})

    while not fast.empty():          # 只清空 fast，slow 保持满
        fast.get_nowait()

    # slow 满了会被丢，但 fast 照样收到
    assert bus.publish("t1", {"n": 3}) == 1, "只该丢 slow 这一个"
    assert fast.get_nowait() == {"n": 3}, "fast 不该受影响"
    assert slow.qsize() == 2
