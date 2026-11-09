"""对话式助手（sidecar 进程）的探测与拉起。

助手本体是 `朴素agent` 的精简 fork，独立进程跑在 8001：

    浏览器 ──iframe──> codingstorm 前端
                            │
                            ├─ 任务队列：走本服务的 REST/WS
                            └─ 助手页：iframe 指向 sidecar 的首页

主服务只负责三件事：
1. 告诉前端助手在哪（`GET /api/assistant/info`）；
2. 需要时把进程拉起来（`autostart` / 前端「启动助手」按钮）；
3. 拉不起来时给出人话原因，而不是让 iframe 白屏。

进程**故意不与主服务同生共死**：助手里有对话历史，重启任务队列不该把正在
写需求的那轮对话掐掉。Windows 上 Popen 出来的子进程本来就独立于父进程的
控制台，主服务退出它继续跑，符合预期。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import AssistantConfig, Config

# 探测超时。助手是本地 stdlib http.server，正常 20ms 内就有响应；
# 给 1.5s 是留出进程刚启动、还在绑端口的余量。
PROBE_TIMEOUT_S = 1.5
# 拉起后等它开始服务的最长时间。过了就判失败，别让前端一直转圈。
START_TIMEOUT_S = 20.0


class AssistantSupervisor:
    """管理 sidecar 的启动与健康状态。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.cfg: AssistantConfig = config.assistant
        self._proc: subprocess.Popen | None = None
        self._last_error = ""

    # ---------------- 基础信息 ----------------

    @property
    def url(self) -> str:
        return self.cfg.url.rstrip("/")

    @property
    def port(self) -> int:
        parsed = urllib.parse.urlparse(self.url)
        return parsed.port or 80

    @property
    def log_path(self) -> Path:
        return self.config.logs_dir / "assistant.log"

    def _resolve_dir(self) -> Path | None:
        if self.cfg.dir is None:
            return None
        return self.cfg.dir.expanduser()

    # ---------------- 探测 ----------------

    def probe(self) -> bool:
        """助手是否已经在端口上应答。同步实现，调用方负责丢线程。"""
        try:
            with urllib.request.urlopen(f"{self.url}/api/state", timeout=PROBE_TIMEOUT_S) as resp:
                return resp.status == 200
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def snapshot(self) -> dict:
        """给前端用的状态快照。running 走实时探测，不用缓存。"""
        target = self._resolve_dir()
        running = self.probe() if self.cfg.enabled else False
        message = ""
        if not self.cfg.enabled:
            message = "助手已在配置里关闭（[assistant] enabled = false）"
        elif not running:
            if target is None:
                message = "还没配置 [assistant] dir，无法自动启动助手"
            elif not (target / "server.py").exists():
                message = f"助手目录里没有 server.py：{target}"
            elif self._last_error:
                message = self._last_error
            else:
                message = "助手没有在运行"
        return {
            "enabled": self.cfg.enabled,
            "url": self.url,
            "running": running,
            "autostart": self.cfg.autostart,
            "managed": target is not None and (target / "server.py").exists(),
            "pid": self._proc.pid if (self._proc and self._proc.poll() is None) else None,
            "message": message,
        }

    async def snapshot_async(self) -> dict:
        return await asyncio.to_thread(self.snapshot)

    # ---------------- 启动 ----------------

    def start(self) -> tuple[bool, str]:
        """确保助手在跑。已在跑就直接返回成功（不重复拉起）。"""
        if not self.cfg.enabled:
            return False, "助手已在配置里关闭"
        if self.probe():
            return True, "助手已在运行"

        target = self._resolve_dir()
        if target is None:
            return False, "还没配置 [assistant] dir（助手代码所在目录）"
        if not (target / "server.py").exists():
            return False, f"助手目录里没有 server.py：{target}"

        python = self.cfg.python or sys.executable
        env = os.environ.copy()
        env["MYWHEEL_PORT"] = str(self.port)
        # 数据目录故意不覆盖：让助手用自己的目录，手动跑 start-assistant.bat
        # 和平台自动拉起用的是同一份对话历史。
        key = (self.cfg.api_key or "").strip() or self.config.claude.env.get(
            "ANTHROPIC_AUTH_TOKEN", ""
        ).strip()
        if key:
            env["MYWHEEL_API_KEY"] = key
        if self.cfg.model.strip():
            env["MYWHEEL_MODEL"] = self.cfg.model.strip()

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = open(self.log_path, "ab")
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        try:
            # 日志写文件：助手崩了要能查，而不是把 traceback 吞进管道
            self._proc = subprocess.Popen(
                [python, "server.py"],
                cwd=str(target),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=flags,
            )
        except OSError as exc:
            self._last_error = f"启动助手失败：{exc}"
            return False, self._last_error
        finally:
            log.close()

        # 等它真的能应答再返回；只看「进程还活着」会误判（端口占用也能活一会儿）
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.probe():
                self._last_error = ""
                return True, "助手已启动"
            if self._proc.poll() is not None:
                break
            time.sleep(0.3)

        self._last_error = (
            f"助手启动失败，{START_TIMEOUT_S:.0f} 秒内没有应答。"
            f"看日志：{self.log_path}"
        )
        return False, self._last_error

    async def start_async(self) -> tuple[bool, str]:
        return await asyncio.to_thread(self.start)

    async def ensure_started(self) -> bool:
        """启动时调用：只在 autostart 打开且还没在跑时才拉。失败不抛异常。"""
        if not self.cfg.enabled or not self.cfg.autostart:
            return False
        try:
            ok, message = await self.start_async()
        except Exception as exc:  # 拉起失败不该影响主服务启动
            self._last_error = f"启动助手出错：{exc}"
            return False
        if not ok:
            print(f"[assistant] {message}")
        return ok
