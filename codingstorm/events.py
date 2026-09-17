"""极简事件总线，用于把执行过程实时推给前端。

设计取舍：**队列满了就丢**。实时流是「看了就行」的东西，为了它去阻塞执行器
完全不划算 —— 真正的记录在数据库和原始 NDJSON 日志里，一条都不会少。
"""

from __future__ import annotations

import asyncio
from typing import Any


class EventBus:
    def __init__(self, maxsize: int = 200):
        self._subs: dict[str, set[asyncio.Queue]] = {}
        self._maxsize = maxsize
        self._dropped = 0

    def subscribe(self, channel: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._subs.setdefault(channel, set()).add(q)
        return q

    def unsubscribe(self, channel: str, q: asyncio.Queue) -> None:
        subs = self._subs.get(channel)
        if not subs:
            return
        subs.discard(q)
        if not subs:
            self._subs.pop(channel, None)

    def publish(self, channel: str, event: Any) -> int:
        """返回被丢弃的订阅者数量（调用方可以据此计数）。"""
        subs = self._subs.get(channel)
        if not subs:
            return 0
        dropped = 0
        for q in list(subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dropped += 1
        self._dropped += dropped
        return dropped

    def subscriber_count(self, channel: str) -> int:
        return len(self._subs.get(channel, ()))

    @property
    def dropped_total(self) -> int:
        return self._dropped
