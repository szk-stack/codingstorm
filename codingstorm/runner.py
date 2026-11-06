"""执行器外壳：拉起子进程、tail NDJSON 输出、把语义事件落库。

**进程管理与事件解释是分开的。** 这一层只管前者，某一行 JSON 什么意思交给
`codingstorm.agents` 里的执行器（Claude Code / Pi）。换执行器不需要动这里。

设计依据来自 Phase 0 实测（见 docs/phase0-report.md）：

- **stdout 落文件不落 PIPE**：消费端慢会阻塞子进程；且崩溃后要靠这份原始日志
  把 `result` 事件捞回来（它是唯一权威的成败判据）。
- **stderr 单独一个文件**：不合并进 stdout，否则杂音混进 JSON 流。但两边都要看 ——
  `--verbose` 缺失这类错误只走 stderr。
- **`stdin=DEVNULL`**：不设的话子进程会继承调用者的 stdin 并把内容读成上下文。
- **`thinking_tokens` 与 `stream_event` 一律不落库**：实测前者占 99.6%。
- **认证类重试出现时立即失败**：否则会白等退避（约 3 分钟）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from codingstorm.agents import (
    MAX_PAYLOAD_BYTES,
    AgentRun,
    RunOutcome,
    make_runtime,
    truncate_payload,
)
from codingstorm.config import Config
from codingstorm.store import Store, utcnow

log = logging.getLogger("codingstorm.runner")

# 落库的语义事件；其余（thinking_tokens / stream_event / message_update ...）一律丢弃
PERSISTED_EVENT_TYPES = frozenset({"init", "assistant", "tool_result", "result"})

TAIL_POLL_S = 0.1

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]



def read_pid_starttime(pid: int) -> str | None:
    """读 /proc/<pid>/stat 的 starttime（第 22 个字段）。

    用于在崩溃恢复时确认「这个 pid 还是当初那个进程」—— 单看 pid 会被复用坑到。
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    try:
        after_comm = raw.split(") ", 1)[1]
        return after_comm.split()[19]
    except (IndexError, ValueError):
        return None


def pid_is_alive(pid: int, expected_starttime: str | None) -> bool:
    """确认这个 pid 就是当初那个进程。

    平台被 SIGKILL / OOM 后子进程不会跟着死，会 reparent 到 PID 1 继续改文件、继续烧钱。
    但单看 pid 存在与否会误判（pid 会被复用），所以要比对 starttime。
    """
    if not pid:
        return False
    actual = read_pid_starttime(pid)
    if actual is None:
        return False
    if expected_starttime is None:
        return True
    return actual == expected_starttime


def kill_process_group(pid: int, sig: int = signal.SIGTERM) -> bool:
    """向整个进程组发信号，覆盖它拉起的 bash / ripgrep 等子进程。"""
    try:
        os.killpg(os.getpgid(pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


class Runner:
    def __init__(
        self,
        config: Config,
        store: Store,
        *,
        on_event: EventHandler | None = None,
        runtime: str | None = None,
    ):
        self.config = config
        self.store = store
        self.on_event = on_event
        # 默认执行器；单条任务可以在 run(runtime=...) 里覆盖（项目级配置）
        self.runtime_name = runtime or config.runtime.default
        self.runtime = make_runtime(config, self.runtime_name)
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        # 每任务的事件序号，在内存里自增（见 _emit 的说明）
        self._seq: dict[str, int] = {}
        # 每任务上次推送 thinking 进度的时刻，用于限流
        self._progress_at: dict[str, float] = {}
        # 每任务的事件解释器
        self._runs: dict[str, tuple[RunOutcome, AgentRun]] = {}

    # ---------- 命令行 ----------

    def build_command(
        self,
        prompt: str,
        *,
        session_id: str,
        context_dir: Path | None,
        resume: bool = False,
        context_dirs: Sequence[Path] | None = None,
    ) -> list[str]:
        # 保留旧的单目录参数，不打破已有调用方
        dirs = list(context_dirs) if context_dirs is not None else (
            [context_dir] if context_dir is not None else []
        )
        return self.runtime.build_command(
            prompt, session_id=session_id, context_dirs=dirs, resume=resume
        )

    # ---------- 主流程 ----------

    async def run(
        self,
        task_id: str,
        project_name: str,
        *,
        prompt: str,
        session_id: str,
        cwd: Path,
        attempt_no: int,
        context_dir: Path | None = None,
        context_dirs: Sequence[Path] | None = None,
        origin: str = "task",
        resume: bool = False,
        runtime: str | None = None,
    ) -> RunOutcome:
        cfg = self.config
        log_path = cfg.task_log_path(project_name, task_id)
        err_path = cfg.task_stderr_path(project_name, task_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # 项目级 runtime 覆盖全局默认值。名字没变就复用实例。
        run_runtime = (
            self.runtime
            if not runtime or runtime == self.runtime_name
            else make_runtime(cfg, runtime)
        )
        dirs = list(context_dirs) if context_dirs is not None else (
            [context_dir] if context_dir is not None else []
        )
        cmd = run_runtime.build_command(
            prompt, session_id=session_id, context_dirs=dirs, resume=resume
        )
        outcome = RunOutcome(session_id=session_id)
        run = run_runtime.new_run(outcome)
        self._runs[task_id] = (outcome, run)

        # 记下实际用的参数（prompt 太长，略去）——
        # 排查「hook / 扩展没生效」这类问题时，第一件事就是确认参数到底传没传
        log.info(
            "启动 %s: cwd=%s %s",
            run_runtime.name,
            cwd,
            _redact_command(cmd, prompt),
        )

        env = os.environ.copy()
        env.update(run_runtime.env(project_name=project_name, task_id=task_id))

        try:
            with log_path.open("wb") as out_f, err_path.open("wb") as err_f:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(cwd),
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,  # 必须：否则会继承调用者的 stdin
                    stdout=out_f,
                    stderr=err_f,
                    start_new_session=True,  # 独立进程组，便于整组回收
                )
                self._procs[task_id] = proc
                await self.store.finish_attempt(
                    task_id,
                    attempt_no,
                    pid=proc.pid,
                    pid_starttime=read_pid_starttime(proc.pid),
                    raw_log_path=str(log_path),
                )
                await self.store.touch_task(task_id)
                await self._supervise(proc, log_path, task_id, outcome)
        finally:
            self._procs.pop(task_id, None)
            self._seq.pop(task_id, None)
            self._progress_at.pop(task_id, None)
            self._runs.pop(task_id, None)

        outcome.exit_code = proc.returncode
        # 先从原始日志捞，再收尾：捞到了就不该再报「缺少 result」
        await self._fill_from_log_tail(log_path, outcome, task_id=task_id, run=run)
        run.finish(exit_code=proc.returncode, stderr_text=_read_text(err_path))
        return outcome

    async def _supervise(
        self,
        proc: asyncio.subprocess.Process,
        log_path: Path,
        task_id: str,
        outcome: RunOutcome,
    ) -> None:
        """tail 日志 + 看门狗。"""
        cfg = self.config.task
        started = asyncio.get_running_loop().time()
        last_data = started

        async def consume() -> None:
            nonlocal last_data
            pos = 0
            carry = b""
            while True:
                # 墙钟上限每轮都查 —— 持续有输出也不能无限跑下去
                if asyncio.get_running_loop().time() - started > cfg.wall_clock_timeout_s:
                    outcome.error_text = f"任务超过墙钟上限 {cfg.wall_clock_timeout_s}s，已终止"
                    await self.kill(task_id)
                    return

                chunk, pos = await asyncio.to_thread(_read_from, log_path, pos)
                if chunk:
                    last_data = asyncio.get_running_loop().time()
                    carry += chunk
                    parts = carry.split(b"\n")
                    carry = parts.pop()
                    for raw_line in parts:
                        await self._handle_line(raw_line, task_id, outcome)
                elif proc.returncode is not None:
                    if carry.strip():
                        await self._handle_line(carry, task_id, outcome)
                    return
                elif asyncio.get_running_loop().time() - last_data > cfg.idle_timeout_s:
                    outcome.error_text = (
                        f"任务超过 {cfg.idle_timeout_s}s 没有任何输出，判定卡死，已终止"
                    )
                    await self.kill(task_id)
                    return
                await asyncio.sleep(TAIL_POLL_S)

        consumer = asyncio.create_task(consume())
        try:
            await proc.wait()
            # 进程退出后把最后一段读完
            await asyncio.wait_for(consumer, timeout=10)
        except TimeoutError:
            consumer.cancel()
        except asyncio.CancelledError:
            consumer.cancel()
            raise

    # ---------- 事件处理 ----------

    async def _handle_line(self, raw: bytes, task_id: str, outcome: RunOutcome) -> None:
        line = raw.strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # 单行坏掉不能打挂整个 tail 循环
            return
        if not isinstance(event, dict):
            return

        run = self._run_for(task_id, outcome)
        handled = run.handle(event)

        for etype, payload in handled.events:
            await self._emit(task_id, etype, payload)

        if handled.fatal:
            # 由执行器判定「重试无意义」或「超轮数」；不只是记一笔，要真的停掉进程组
            outcome.error_text = handled.fatal
            await self.kill(task_id)
            return

        # 进度只用于实时提示，不落库，而且限流到每秒一条 —— Claude 的
        # thinking_tokens 实测占事件量 99.6%，Pi 的 message_update 也是逐 token，
        # 逐条转发会淹掉队列、把 result 这类关键事件挤丢。
        tokens = run.progress(event)
        if tokens is not None and self.on_event is not None:
            now = asyncio.get_running_loop().time()
            last = self._progress_at.get(task_id, 0.0)
            if now - last >= 1.0:
                self._progress_at[task_id] = now
                await self.on_event({
                    "task_id": task_id,
                    "type": "progress",
                    "thinking_tokens": tokens,
                })

    def _run_for(self, task_id: str, outcome: RunOutcome) -> AgentRun:
        """拿这条任务当前的事件解释器；测试里直接调 _handle_line 时按需创建。"""
        entry = self._runs.get(task_id)
        if entry is not None and entry[0] is outcome:
            return entry[1]
        run = self.runtime.new_run(outcome)
        self._runs[task_id] = (outcome, run)
        return run

    async def _emit(self, task_id: str, etype: str, payload: dict[str, Any]) -> None:
        if etype not in PERSISTED_EVENT_TYPES:
            return
        # seq 必须在这里自增，不能每次去库里查 MAX(seq) —— 事件是批量提交的，
        # 前一条还没落盘时查出来的是旧值，连续几条会拿到同一个 seq，
        # 然后被 INSERT OR REPLACE 互相覆盖，静默丢事件。
        if task_id not in self._seq:
            self._seq[task_id] = await self.store.next_event_seq(task_id)
        seq = self._seq[task_id]
        self._seq[task_id] = seq + 1

        await self.store.db.append_events(
            [(task_id, seq, utcnow(), etype, json.dumps(payload, ensure_ascii=False))]
        )
        if self.on_event is not None:
            await self.on_event({"task_id": task_id, "type": etype, "payload": payload, "seq": seq})

    async def _fill_from_log_tail(
        self,
        log_path: Path,
        outcome: RunOutcome,
        *,
        task_id: str | None = None,
        run: AgentRun | None = None,
    ) -> None:
        """崩在收尾事件落库与状态更新之间时，从原始日志把结果捞回来。

        各执行器的收尾事件不同（Claude 是 `result`，Pi 是 `agent_settled`），
        所以扫描逻辑交给 `AgentRun.recover`。
        """
        if outcome.saw_result:
            return
        try:
            raw = log_path.read_bytes()
        except OSError:
            return
        events: list[dict[str, Any]] = []
        # 按时间顺序交给执行器：Pi 要看到 assistant 消息才能合成结果，
        # 倒着喂会让它先遇到 agent_settled、误判成「没有助手输出」。
        # 第一行单独留着 —— 那是 Pi 的 session header（会话 id 在这一行）。
        lines = raw.split(b"\n")
        selected = lines if len(lines) <= 200 else lines[:1] + lines[-200:]
        for line in selected:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        runner = run or (
            self._run_for(task_id, outcome) if task_id is not None
            else self.runtime.new_run(outcome)
        )
        runner.recover(events)

    # ---------- 终止 ----------

    async def kill(self, task_id: str) -> None:
        """向整个进程组发信号，覆盖它拉起的 bash / ripgrep 等子进程。"""
        proc = self._procs.get(task_id)
        if proc is None or proc.returncode is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except TimeoutError:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                pass

    @property
    def active(self) -> dict[str, asyncio.subprocess.Process]:
        return dict(self._procs)


def _read_from(path: Path, pos: int) -> tuple[bytes, int]:
    try:
        with path.open("rb") as f:
            f.seek(pos)
            data = f.read()
    except OSError:
        return b"", pos
    return data, pos + len(data)


def _read_text(path: Path) -> str:
    try:
        # 子进程的 stderr 是 UTF-8；Windows 上默认按 GBK 读会把中文读成乱码，
        # 而这段文字要进 error_text 给人看
        return path.read_bytes().decode("utf-8", "replace")
    except OSError:
        return ""


def _redact_command(cmd: list[str], prompt: str) -> str:
    """日志里不落整段 prompt（可能很长，也可能含项目内情）。"""
    return " ".join("<prompt>" if part == prompt else part for part in cmd)
