"""执行器：拉起 Claude Code 子进程、tail 它的 NDJSON 输出、把语义事件落库。

设计依据全部来自 Phase 0 实测（见 docs/phase0-report.md）：

- **stdout 落文件不落 PIPE**：消费端慢会阻塞子进程；且崩溃后要靠这份原始日志
  把 `result` 事件捞回来（它是唯一权威的成败判据）。
- **stderr 单独一个文件**：不合并进 stdout，否则杂音混进 JSON 流。但两边都要看 ——
  `--verbose` 缺失这类错误只走 stderr。
- **`stdin=DEVNULL`**：不设的话子进程会继承调用者的 stdin 并把内容读成上下文。
- **`thinking_tokens` 与 `stream_event` 一律不落库**：实测前者占 99.6%。
- **`api_retry` 出现不可恢复错误时立即失败**：否则会白等 10 次退避（约 3 分钟）。
- **`--disallowed-tools AskUserQuestion`**：不禁掉的话无人值守时它会挂在那里等回答。
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codingstorm.config import Config
from codingstorm.store import Store, utcnow

# 落库的语义事件；其余（thinking_tokens / stream_event / api_retry ...）一律丢弃
PERSISTED_EVENT_TYPES = frozenset({"init", "assistant", "tool_result", "result"})

# 单条 payload 的落库上限，超出只留预览
MAX_PAYLOAD_BYTES = 8 * 1024

# 这些错误重试也没用，见到就立刻停
FATAL_HTTP_STATUS = frozenset({401, 403})

TAIL_POLL_S = 0.1

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class RunOutcome:
    exit_code: int | None = None
    session_id: str | None = None
    model: str | None = None
    result_subtype: str | None = None
    is_error: bool = False
    result_text: str | None = None
    num_turns: int | None = None
    duration_ms: int | None = None
    duration_api_ms: int | None = None
    total_cost_usd: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    error_text: str | None = None
    permission_denials: list[Any] = field(default_factory=list)
    saw_result: bool = False

    @property
    def ok(self) -> bool:
        return self.saw_result and not self.is_error


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


def truncate_payload(obj: Any) -> Any:
    """把过大的 payload 换成预览 + 原始大小。"""
    try:
        text = json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return {"preview": str(obj)[:MAX_PAYLOAD_BYTES], "truncated_bytes": None}
    if len(text.encode("utf-8")) <= MAX_PAYLOAD_BYTES:
        return obj
    return {
        "preview": text[:MAX_PAYLOAD_BYTES],
        "truncated_bytes": len(text.encode("utf-8")),
    }


class Runner:
    def __init__(
        self,
        config: Config,
        store: Store,
        *,
        on_event: EventHandler | None = None,
    ):
        self.config = config
        self.store = store
        self.on_event = on_event
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        # 每任务的事件序号，在内存里自增（见 _emit 的说明）
        self._seq: dict[str, int] = {}
        # 每任务上次推送 thinking 进度的时刻，用于限流
        self._progress_at: dict[str, float] = {}

    # ---------- 命令行 ----------

    def build_command(self, prompt: str, *, session_id: str, context_dir: Path | None) -> list[str]:
        cfg = self.config
        cmd = [
            cfg.claude.binary,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            # 不传 --verbose 时 stream-json 会直接报错退出（Phase 0 实测）
            "--verbose",
            "--session-id",
            session_id,
            "--max-turns",
            str(cfg.task.max_turns),
            "--permission-mode",
            cfg.claude.permission_mode,
            # 无人值守时必须禁掉：否则它会停下来等一个永远不会来的回答
            "--disallowed-tools",
            "AskUserQuestion",
        ]
        if context_dir is not None:
            cmd += ["--add-dir", str(context_dir)]
        if cfg.claude.settings_file is not None:
            cmd += ["--settings", str(cfg.claude.settings_file)]
        return cmd

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
        origin: str = "task",
    ) -> RunOutcome:
        cfg = self.config
        log_path = cfg.task_log_path(project_name, task_id)
        err_path = cfg.task_stderr_path(project_name, task_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = self.build_command(prompt, session_id=session_id, context_dir=context_dir)
        outcome = RunOutcome(session_id=session_id)

        with log_path.open("wb") as out_f, err_path.open("wb") as err_f:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(cwd),
                stdin=asyncio.subprocess.DEVNULL,  # 必须：否则会继承调用者的 stdin
                stdout=out_f,
                stderr=err_f,
                start_new_session=True,  # 独立进程组，便于整组回收
            )
            self._procs[task_id] = proc
            try:
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

        outcome.exit_code = proc.returncode
        if not outcome.saw_result:
            outcome.is_error = True
            if outcome.error_text is None:
                outcome.error_text = self._describe_missing_result(proc, err_path)
        await self._fill_from_log_tail(log_path, outcome)
        return outcome

    def _describe_missing_result(self, proc: asyncio.subprocess.Process, err_path: Path) -> str:
        """没有 result 事件时，尽量给出有用的原因。

        注意「进程非零退出」和「没收到 result」是两件事：stderr 上的启动期错误
        （比如缺 --verbose）只会有后者。
        """
        detail = ""
        try:
            raw = err_path.read_text(errors="replace").strip()
        except OSError:
            raw = ""
        if raw:
            detail = f"stderr: {raw[:500]}"
        if proc.returncode not in (0, None):
            kind = "被信号终止" if proc.returncode < 0 else "非零退出"
            return f"进程{kind}（{proc.returncode}）且未产生 result 事件。{detail}".strip()
        return f"进程结束但未产生 result 事件。{detail}".strip()

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

        etype = event.get("type")
        subtype = event.get("subtype")

        if etype == "system" and subtype == "init":
            outcome.session_id = event.get("session_id") or outcome.session_id
            outcome.model = event.get("model")
            await self._emit(task_id, "init", {
                "session_id": outcome.session_id,
                "model": outcome.model,
                "cwd": event.get("cwd"),
                "tools": event.get("tools"),
            })
            return

        if etype == "system" and subtype == "api_retry":
            # 不可恢复的认证类错误：退避重试 10 次要等约 3 分钟，直接停
            if event.get("error_status") in FATAL_HTTP_STATUS:
                outcome.error_text = (
                    f"认证失败（HTTP {event.get('error_status')}: {event.get('error')}），"
                    "重试无意义，已终止"
                )
                await self.kill(task_id)
            return

        if etype == "result":
            self._absorb_result(event, outcome)
            await self._emit(task_id, "result", {
                "subtype": outcome.result_subtype,
                "is_error": outcome.is_error,
                "num_turns": outcome.num_turns,
                "duration_ms": outcome.duration_ms,
                "result": outcome.result_text,
                "usage": {
                    "input_tokens": outcome.input_tokens,
                    "output_tokens": outcome.output_tokens,
                    "cache_creation_input_tokens": outcome.cache_creation_tokens,
                    "cache_read_input_tokens": outcome.cache_read_tokens,
                },
                "permission_denials": outcome.permission_denials,
            })
            return

        if etype == "assistant":
            message = event.get("message") or {}
            tool_uses = [
                {"id": b.get("id"), "name": b.get("name"), "input": truncate_payload(b.get("input"))}
                for b in (message.get("content") or [])
                if isinstance(b, dict) and b.get("type") == "tool_use"
            ]
            text = "\n".join(
                b.get("text", "")
                for b in (message.get("content") or [])
                if isinstance(b, dict) and b.get("type") == "text"
            ).strip()
            if not text and not tool_uses:
                # 纯 thinking 的块不带信息，落了只是噪音
                return
            await self._emit(task_id, "assistant", {
                "text": text[:MAX_PAYLOAD_BYTES] or None,
                "tool_uses": tool_uses or None,
            })
            return

        if etype == "user":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    await self._emit(task_id, "tool_result", {
                        "tool_use_id": block.get("tool_use_id"),
                        "is_error": block.get("is_error", False),
                        "content": truncate_payload(block.get("content")),
                    })
            return

        # thinking_tokens 每秒最多推一次进度；stream_event 完全不推。
        # 实测 thinking_tokens 占事件量的 99.6%，逐条转发会淹掉队列、
        # 把 result 这类关键事件挤丢。
        if etype == "system" and subtype == "thinking_tokens":
            if self.on_event is not None:
                now = asyncio.get_running_loop().time()
                last = self._progress_at.get(task_id, 0.0)
                if now - last >= 1.0:
                    self._progress_at[task_id] = now
                    await self.on_event({
                        "task_id": task_id,
                        "type": "progress",
                        "thinking_tokens": event.get("estimated_tokens") or 0,
                    })
            return

        if etype == "stream_event":
            return

    @staticmethod
    def _absorb_result(event: dict[str, Any], outcome: RunOutcome) -> None:
        """result 是唯一权威判据，退出码粒度太粗。"""
        usage = event.get("usage") or {}
        outcome.saw_result = True
        outcome.result_subtype = event.get("subtype")
        outcome.is_error = bool(event.get("is_error"))
        outcome.result_text = event.get("result")
        outcome.num_turns = event.get("num_turns")
        outcome.duration_ms = event.get("duration_ms")
        outcome.duration_api_ms = event.get("duration_api_ms")
        outcome.total_cost_usd = event.get("total_cost_usd")
        outcome.input_tokens = usage.get("input_tokens") or 0
        outcome.output_tokens = usage.get("output_tokens") or 0
        outcome.cache_creation_tokens = usage.get("cache_creation_input_tokens") or 0
        outcome.cache_read_tokens = usage.get("cache_read_input_tokens") or 0
        denials = event.get("permission_denials") or []
        outcome.permission_denials = denials
        if event.get("session_id"):
            outcome.session_id = event["session_id"]

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

    async def _fill_from_log_tail(self, log_path: Path, outcome: RunOutcome) -> None:
        """崩在 result 落库与状态更新之间时，从原始日志把结果捞回来。"""
        if outcome.saw_result:
            return
        try:
            raw = log_path.read_bytes()
        except OSError:
            return
        for line in reversed(raw.split(b"\n")[-200:]):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                self._absorb_result(event, outcome)
                return

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
