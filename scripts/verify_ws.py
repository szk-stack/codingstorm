#!/usr/bin/env python3
"""验证 WebSocket 实时推送：提交任务，边跑边收，跑完再连一次验证历史回放。"""
import asyncio
import json
import urllib.request

import websockets

API = "http://127.0.0.1:8788"
WS = "ws://127.0.0.1:8788"


def get(path):
    return json.loads(urllib.request.urlopen(API + path, timeout=10).read())


def post(path, body):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


async def collect(tid, deadline_s, label):
    kinds, events, statuses = {}, [], []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_s
    async with websockets.connect(f"{WS}/api/ws/tasks/{tid}") as ws:
        while loop.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=3)
            except asyncio.TimeoutError:
                continue
            msg = json.loads(raw)
            k = msg.get("kind")
            kinds[k] = kinds.get(k, 0) + 1
            if k == "status":
                statuses.append(msg["status"])
                print(f"  [{label}] 状态 -> {msg['status']}")
                if msg["status"] in ("awaiting_review", "failed", "interrupted"):
                    break
            elif k == "event":
                events.append(msg["type"])
            elif k == "events":
                for e in msg["events"]:
                    events.append(e.get("type"))
    print(f"  [{label}] 消息类型 {kinds}")
    print(f"  [{label}] 事件类型 {events}")
    return statuses


async def main():
    projects = get("/api/projects")
    pid = next(p["id"] for p in projects if p["name"] == "demo")

    task = post(f"/api/projects/{pid}/tasks", {
        "title": "创建 textutil.py，实现 truncate(text, n) 截断并加省略号",
        "kind": "requirement",
    })
    tid = task["id"]
    print(f"任务 {tid} 已提交")

    print("\n--- 实时连接 ---")
    statuses = await collect(tid, 120, "live")

    print("\n--- 跑完后重连（验证历史回放）---")
    await collect(tid, 5, "replay")

    print("\n--- diff ---")
    d = get(f"/api/tasks/{tid}/diff")
    print("  stat:", (d.get("stat") or "").strip().replace("\n", " | "))
    print("  html 长度:", len(d.get("html") or ""))
    print("  状态序列:", statuses)


asyncio.run(main())
