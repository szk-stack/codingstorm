#!/usr/bin/env python3
"""用 Chrome 调试协议检查页面 —— 无头浏览器里跑完 JS 后，把渲染结果取出来。

    python scripts/browser_check.py <url> [等待秒数]

比截图有用：能拿到 JS 执行后的真实 DOM、控制台报错、以及任意 JS 表达式的值。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets.asyncio.client as ws_client

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]


def free_port() -> int:
    """每次用不同端口。

    写死端口会踩坑：Chrome 会派生子进程，terminate() 杀不干净时，
    下一次探测会连到**上一个实例**上，读到的是旧页面 —— 排查时会以为是代码问题。
    """
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

PROBES = {
    "连接状态": "document.querySelector('#conn')?.textContent",
    "项目列表": "document.querySelector('#projects')?.innerText",
    "任务数": "document.querySelectorAll('#tasks li').length",
    "任务列表": "document.querySelector('#tasks')?.innerText",
    "详情标题": "document.querySelector('#detail-title')?.textContent",
    "状态徽章": "document.querySelector('#detail-status')?.innerText",
    "元信息": "document.querySelector('#detail-meta')?.innerText",
    "执行流条目": "document.querySelectorAll('#stream .ev').length",
    "执行流文本": "document.querySelector('#stream')?.innerText",
    "工具卡片数": "document.querySelectorAll('#stream .tool').length",
    "diff 行数": "document.querySelectorAll('#diff-body tr').length",
    "diff 首行": "document.querySelector('#diff-body')?.innerText?.slice(0,400)",
    "操作按钮": "document.querySelector('#actions')?.innerText",
    "上下文面板可见": "!document.querySelector('#context-panel')?.hidden",
    "指针图字节提示": "document.querySelector('#ctx-bytes')?.textContent",
    "指针图内容": "document.querySelector('#pointer-text')?.value?.slice(0,200)",
    "变更记录": "document.querySelector('#journal-text')?.textContent?.slice(-300)",
    "对话轮次": "document.querySelectorAll('#chat .chat-turn').length",
    "对话内容": "document.querySelector('#chat')?.innerText?.slice(0,600)",
    "继续输入框可见": "!document.querySelector('#chat-form')?.hidden",
    "文件树条目": "document.querySelectorAll('#file-tree .tree-row').length",
    "文件树文本": "document.querySelector('#file-tree')?.innerText?.slice(0,400)",
    "面包屑": "document.querySelector('#file-crumbs')?.innerText",
    "文件面板可见": "!document.querySelector('#file-panel')?.hidden",
    "文件正文": "document.querySelector('#file-body')?.textContent?.slice(0,200)",
    "JS 报错": "window.__errors ? window.__errors.join(' | ') : '(无)'",
}


def find_chrome() -> str:
    for p in CHROME_CANDIDATES:
        if Path(p).exists():
            return p
    raise SystemExit("找不到 Chrome/Edge")


def fetch_json(url: str):
    with urllib.request.urlopen(url, timeout=3) as r:
        return json.loads(r.read())


async def run(url: str, wait_s: float, pre_script: str = "") -> int:
    chrome = find_chrome()
    port = free_port()
    profile = Path(tempfile.mkdtemp(prefix="cs-browser-"))
    proc = subprocess.Popen(
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-first-run",
            "--disable-dev-shm-usage",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        targets = None
        for _ in range(60):
            try:
                targets = fetch_json(f"http://127.0.0.1:{port}/json")
                if targets:
                    break
            except Exception:
                time.sleep(0.25)
        if not targets:
            print("Chrome 调试端口没起来")
            return 1

        page = next((t for t in targets if t.get("type") == "page"), targets[0])
        ws_url = page["webSocketDebuggerUrl"]

        async with ws_client.connect(ws_url, max_size=32 * 1024 * 1024) as ws:
            counter = {"n": 0}

            async def send(method: str, params: dict | None = None):
                counter["n"] += 1
                mid = counter["n"]
                await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == mid:
                        return msg.get("result")

            await send("Runtime.enable")
            await send("Page.enable")
            # 把 JS 报错收集起来，页面崩了能看见原因
            await send("Page.addScriptToEvaluateOnNewDocument", {
                "source": "window.__errors=[];"
                          "window.addEventListener('error',e=>window.__errors.push(String(e.message)));"
                          "window.addEventListener('unhandledrejection',"
                          "e=>window.__errors.push('rejection: '+String(e.reason)));"
            })
            await send("Page.navigate", {"url": url})
            await asyncio.sleep(wait_s)

            async def evaluate(expr: str):
                res = await send("Runtime.evaluate", {
                    "expression": expr, "returnByValue": True, "awaitPromise": True,
                })
                result = (res or {}).get("result", {})
                if "value" in result:
                    return result["value"]
                return f"<{result.get('type')}: {result.get('description', '')}>"

            print(f"URL: {url}")
            print("=" * 70)

            if pre_script:
                # 探针只能读，读不到「点了之后」的样子 —— 想验交互就先跑一段 JS
                # （点页签、点文件），再让探针去读结果。
                # 脚本里返回的值直接打出来，省得只能靠探针间接推断。
                print(f"\n【预执行脚本返回】\n{await evaluate(pre_script)}")
                await asyncio.sleep(1.5)

            for label, expr in PROBES.items():
                value = await evaluate(expr)
                if isinstance(value, str):
                    value = value.strip()
                    if len(value) > 700:
                        value = value[:700] + " …"
                print(f"\n【{label}】\n{value}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    target = sys.argv[1]
    wait = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0
    pre = sys.argv[3] if len(sys.argv) > 3 else ""
    raise SystemExit(asyncio.run(run(target, wait, pre)))
