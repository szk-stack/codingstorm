"""Pi Coding Agent 执行器（`@earendil-works/pi-coding-agent`）。

和 Claude Code 的差异都集中在这里，不要扩散到 runner / scheduler：

| | Claude Code | Pi |
|---|---|---|
| 输出 | Anthropic stream-json | 自己的 JSONL（`--mode json`） |
| 会话 | `--session-id` / `--resume` | `--session-id` 打开或新建 |
| 轮数上限 | `--max-turns` | **没有这个开关**，平台数 `turn_end`，在下一轮开始前拦 |
| 权限 | `bypassPermissions` + PreToolUse hook | **没有权限系统**，边界靠 `tool_call` 扩展 |
| 成败 | `result.is_error` | `agent_settled` + 最后一条 assistant 的 `stopReason` |
| 用量 | `result.usage` 聚合 | 每条 assistant 的 `usage` 逐条累加 |

Pi 的 JSON 模式**在助手回复失败时仍然退出 0**，所以绝对不能拿退出码判成败。

事件参考：https://pi.dev/docs/latest/json
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from codingstorm.agents.base import (
    MAX_PAYLOAD_BYTES,
    AgentRun,
    AgentRuntime,
    Handled,
    RunOutcome,
    describe_missing_result,
    truncate_payload,
)
from codingstorm.config import Config

# 认证类错误重试也没用。Pi 默认最多退避 3 次，比 Claude Code 的 10 次好一些，
# 但无人值守时仍然没必要白等。
_AUTH_ERROR_RE = re.compile(
    r"\b(401|403)\b|unauthorized|invalid[\s_-]*api[\s_-]*key|authentication",
    re.IGNORECASE,
)


class PiRuntime(AgentRuntime):
    name = "pi"

    def build_command(
        self,
        prompt: str,
        *,
        session_id: str,
        context_dirs: Sequence[Path],
        resume: bool,
    ) -> list[str]:
        cfg = self.config.pi
        cmd = [cfg.binary, "--mode", "json", "--session-id", session_id]
        session_dir = self._session_dir()
        if session_dir is not None:
            cmd += ["--session-dir", str(session_dir)]
        cmd += self._trust_args()
        cmd += self._model_args()

        guard = self._guard_extension()
        if guard is not None:
            # 平台生成的 tool_call 扩展：把 bash 命令交给同一个 guard.py 判定。
            # Pi 本身没有 PreToolUse hook，也没有权限系统。
            cmd += ["-e", str(guard)]

        cmd += list(cfg.extra_args)
        # `--` 之后一律当 prompt；否则以 `-` 开头的内容会被当成开关
        cmd += ["--", prompt]
        # context_dirs 不需要授权参数：Pi 的工具按启动用户的权限访问文件系统，
        # 不限制在 cwd 内（见 docs/pi-agent-integration.md）。prompt 里给的是绝对路径。
        return cmd

    def build_oneshot_command(
        self, prompt: str, *, session_id: str, cwd: Path
    ) -> list[str]:
        cfg = self.config.pi
        cmd = [
            cfg.binary,
            "--mode",
            "json",
            # 不写会话文件：沉淀是一次性的，多轮对话不该接在它后面
            "--no-session",
            # 只出文本，不给它任何动手机会
            "--no-tools",
        ]
        cmd += self._trust_args()
        cmd += self._model_args()
        cmd += list(cfg.extra_args)
        cmd += ["--", prompt]
        return cmd

    def env(self, *, project_name: str, task_id: str) -> dict[str, str]:
        """给 Pi 的边界拦截扩展传路径。

        不用把绝对路径写进生成的 .ts 文件里 —— 那样 Windows 盘符和引号都要转义，
        而且平台搬家后文件就失效了。
        """
        guard = self.config.root / "guard"
        return {
            "CODINGSTORM_GUARD_PYTHON": sys.executable,
            "CODINGSTORM_GUARD_SCRIPT": str(guard / "guard.py"),
            "CODINGSTORM_TASK_ID": task_id,
            "CODINGSTORM_PROJECT": project_name,
        }

    def new_run(self, outcome: RunOutcome) -> AgentRun:
        return PiRun(self.config, outcome)

    # ---------- 内部 ----------

    def _session_dir(self) -> Path | None:
        """相对路径按 `{root}` 解析；否则 Pi 会按 worktree 的 cwd 解析，
        每个任务一个目录，多轮对话就找不回来了。"""
        raw = self.config.pi.session_dir
        if raw is None:
            return None
        return raw if raw.is_absolute() else self.config.root / raw

    def _trust_args(self) -> list[str]:
        """项目信任。

        非交互模式下 Pi 不会弹信任框；默认的 `ask` 会**忽略**仓库里的 .pi 设置与
        扩展。这里显式写死，避免不同机器上 defaultProjectTrust 配置不同导致行为漂移。
        `AGENTS.md` / `CLAUDE.md` 不受信任开关影响，仍然会加载。
        """
        trust = self.config.pi.project_trust
        if trust == "approve":
            return ["--approve"]
        if trust == "ignore":
            return ["--no-approve"]
        return []

    def _model_args(self) -> list[str]:
        cfg = self.config.pi
        args: list[str] = []
        if cfg.model:
            args += ["--model", cfg.model]
        if cfg.provider:
            args += ["--provider", cfg.provider]
        if cfg.thinking:
            args += ["--thinking", cfg.thinking]
        return args

    def _guard_extension(self) -> Path | None:
        if not self.config.claude.guard_enabled:
            return None
        path = self.config.root / "guard" / "pi-guard.ts"
        return path if path.exists() else None


class PiRun(AgentRun):
    def __init__(self, config: Config, outcome: RunOutcome):
        super().__init__(config, outcome)
        self._started = time.monotonic()
        self._turns = 0
        self._assistant_count = 0
        self._last_text: str | None = None
        self._stop_reason: str | None = None
        self._error_message: str | None = None
        self._ended = False          # 收到 agent_end
        self._will_retry = False      # agent_end.willRetry
        self._settled = False         # 收到 agent_settled
        self._aborted = False
        self._finalized = False

    def handle(self, event: dict[str, Any]) -> Handled:
        etype = event.get("type")

        if etype == "session":
            # JSON 模式的第一行：{"type":"session","version":3,"id":...}
            self.outcome.session_id = event.get("id") or self.outcome.session_id
            return Handled(events=[("init", {
                "session_id": self.outcome.session_id,
                "model": self.outcome.model,
                "cwd": event.get("cwd"),
                "tools": None,
            })])

        if etype == "message_end":
            message = event.get("message") or {}
            if not isinstance(message, dict):
                return Handled()
            if message.get("role") == "assistant":
                return self._absorb_assistant(message)
            # toolResult 消息与 tool_execution_end 重复，不落库；
            # user 消息是 prompt 回流，也没有展示价值。
            return Handled()

        if etype == "tool_execution_end":
            return Handled(events=[("tool_result", {
                "tool_use_id": event.get("toolCallId"),
                "is_error": bool(event.get("isError")),
                "content": truncate_payload(_result_text(event.get("result"))),
            })])

        if etype == "turn_start":
            # Pi 没有 --max-turns。在**下一轮开始前**拦，而不是等它跑完再杀 ——
            # 跑完再杀等于多烧一次模型调用。
            limit = self.config.task.max_turns
            if self._turns >= limit:
                return Handled(fatal=f"超过 max_turns={limit}，已终止")
            return Handled()

        if etype == "turn_end":
            self._turns += 1
            return Handled()

        if etype == "agent_end":
            self._ended = True
            self._will_retry = bool(event.get("willRetry"))
            return Handled()

        if etype == "agent_settled":
            if self._settled:
                return Handled()
            self._settled = True
            self._aborted = bool(event.get("aborted"))
            self._finalize_result()
            return Handled(events=[("result", self._result_payload())])

        if etype == "auto_retry_start":
            message = str(event.get("errorMessage") or "")
            if message:
                self._error_message = message
            if message and _AUTH_ERROR_RE.search(message):
                return Handled(fatal=f"认证失败（{message}），重试无意义，已终止")
            return Handled()

        if etype == "auto_retry_end" and not event.get("success"):
            final = event.get("finalError")
            if final:
                self._error_message = str(final)
            return Handled()

        # compaction / queue_update / message_start / message_update / 扩展事件……
        # 一律忽略。message_update 是 delta，落库只会淹掉关键事件。
        return Handled()

    def finish(self, *, exit_code: int | None, stderr_text: str) -> None:
        if self._finalized:
            return
        # 已经被看门狗 / 致命错误判死的，不要因为「刚好收到 agent_end」翻成功。
        # 超时和认证失败的 error_text 在这一步之前就写好了。
        if self.outcome.error_text:
            self.outcome.is_error = True
            return
        # agent_end(willRetry=false) 已经声明「不会再有自动续跑」。进程正常退出时
        # 即使没等到 agent_settled，也可以认为这一轮结束了 —— 常见于流被截断。
        if self._ended and not self._will_retry:
            self._finalize_result()
            return
        self.outcome.is_error = True
        self.outcome.error_text = describe_missing_result(
            exit_code, stderr_text, event="agent_settled"
        )

    def recover(self, events: Sequence[dict[str, Any]]) -> None:
        if self.outcome.saw_result:
            return
        probe = PiRun(self.config, RunOutcome())
        for event in events:
            probe.handle(event)
        if not (probe._settled or (probe._ended and not probe._will_retry)):
            return
        probe._finalize_result(duration=False)
        # 只补还空着的字段：活着的 tail 可能已经比日志尾部看到更多内容
        self.outcome.session_id = self.outcome.session_id or probe.outcome.session_id
        self.outcome.model = probe.outcome.model or self.outcome.model
        self.outcome.result_text = probe.outcome.result_text
        self.outcome.result_subtype = probe.outcome.result_subtype
        self.outcome.num_turns = self.outcome.num_turns or probe.outcome.num_turns
        self.outcome.is_error = probe.outcome.is_error
        self.outcome.error_text = probe.outcome.error_text
        self.outcome.saw_result = True
        if not self.outcome.input_tokens and not self.outcome.output_tokens:
            self.outcome.input_tokens = probe.outcome.input_tokens
            self.outcome.output_tokens = probe.outcome.output_tokens
            self.outcome.cache_read_tokens = probe.outcome.cache_read_tokens
            self.outcome.cache_creation_tokens = probe.outcome.cache_creation_tokens

    def progress(self, event: dict[str, Any]) -> int | None:
        # message_update 是逐 token 的 delta；顶层的 usage 是累计值。
        # 只取数字给 UI 显示，事件本身不落库。
        if event.get("type") == "message_update":
            usage = event.get("usage") or {}
            return _int(usage.get("output"))
        return None

    # ---------- 内部 ----------

    def _absorb_assistant(self, message: dict[str, Any]) -> Handled:
        self._assistant_count += 1
        if message.get("model"):
            self.outcome.model = message.get("model")

        usage = message.get("usage") or {}
        self.outcome.input_tokens += _int(usage.get("input"))
        self.outcome.output_tokens += _int(usage.get("output"))
        self.outcome.cache_read_tokens += _int(usage.get("cacheRead"))
        self.outcome.cache_creation_tokens += _int(usage.get("cacheWrite"))

        stop = message.get("stopReason")
        if stop and stop != "pending":
            self._stop_reason = stop
        if message.get("errorMessage"):
            self._error_message = str(message["errorMessage"])

        blocks = message.get("content")
        if isinstance(blocks, str):
            blocks = [{"type": "text", "text": blocks}]
        elif not isinstance(blocks, list):
            blocks = []

        texts: list[str] = []
        tool_uses: list[dict[str, Any]] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text") or ""
                if text.strip():
                    texts.append(text)
            elif block.get("type") == "toolCall":
                tool_uses.append({
                    "id": block.get("id"),
                    "name": block.get("name"),
                    # Pi 用 arguments，Claude 用 input；UI 只认 input
                    "input": truncate_payload(block.get("arguments") or {}),
                })

        text = "\n".join(texts).strip()
        if text:
            self._last_text = text
        if not text and not tool_uses:
            # 纯 thinking 的块不带信息，落了只是噪音
            return Handled()
        return Handled(events=[("assistant", {
            "text": text[:MAX_PAYLOAD_BYTES] or None,
            "tool_uses": tool_uses or None,
        })])

    def _finalize_result(self, *, duration: bool = True) -> None:
        if self._finalized:
            return
        self._finalized = True

        # 只有 stop / toolUse 是正常收尾。未知值按失败处理 —— Pi 升级后新增
        # 一个 stopReason 时，宁可报错也不能悄悄当成成功。
        bad = self._aborted or self._stop_reason not in ("stop", "toolUse")
        if self._assistant_count == 0:
            bad = True
            if not self._error_message:
                self._error_message = (
                    "执行被中止（aborted）" if self._aborted else "没有收到任何助手输出"
                )
        elif self._stop_reason is None and not self._error_message:
            bad = True
            self._error_message = "没有拿到最终回复（缺少 stopReason）"

        if self._aborted:
            subtype = "aborted"
        elif self._stop_reason == "length":
            subtype = "error_max_tokens"
        elif self._stop_reason == "deferred":
            subtype = "error_deferred"
        elif bad:
            subtype = "error_during_execution"
        else:
            subtype = "success"

        outcome = self.outcome
        outcome.saw_result = True
        outcome.is_error = bad
        outcome.result_subtype = subtype
        outcome.result_text = self._last_text
        outcome.num_turns = self._turns or self._assistant_count
        if duration:
            outcome.duration_ms = int((time.monotonic() - self._started) * 1000)
        if bad:
            outcome.error_text = self._error_message or _bad_reason(
                self._aborted, self._stop_reason
            )
        else:
            # 中途的 auto_retry 失败可能留过 error_text；最终成功就把它清掉，
            # 否则任务详情里会挂一条「成功但有错误说明」的噪音
            outcome.error_text = None

    def _result_payload(self) -> dict[str, Any]:
        outcome = self.outcome
        return {
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
            # Pi 没有 permission_denials 概念；被扩展拦下的调用以 tool_result
            # 的 is_error 形式出现，审计留在 guard 的 audit.log 里。
            "permission_denials": [],
        }


def _bad_reason(aborted: bool, stop_reason: str | None) -> str:
    if aborted:
        return "执行被中止（aborted）"
    if stop_reason == "length":
        return "输出达到模型上限被截断（stopReason=length）"
    if stop_reason == "deferred":
        return "模型响应被延迟（stopReason=deferred），本轮没有完成"
    if stop_reason == "error":
        return "模型返回错误（stopReason=error）"
    return f"执行失败（stopReason={stop_reason or '未知'}）"


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _result_text(result: Any) -> Any:
    """把工具结果压成可展示的文本。

    Pi 的形状是 `{content:[{type:"text",text:"..."}], details:{...}}`；
    不同工具的 details 差异很大，只取 content。
    """
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            if texts:
                return "\n".join(texts)
        details = result.get("details")
        if details is not None:
            return details
    return result
