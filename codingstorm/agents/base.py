"""执行器抽象。

`runner` 只负责和进程打交道的那部分 —— 拉起子进程、tail 日志文件、看门狗、
把语义事件落库。**某一行 JSON 是什么意思，是执行器自己的事**。

这样 Claude Code 的 stream-json 与 Pi 的 JSONL 事件流不会渗进调度器：
调度器只认 `init / assistant / tool_result / result` 四种语义事件，
Web UI、成本记账、多轮会话都不用知道底下换了谁。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Sequence

from codingstorm.config import Config

# 单条 payload 的落库上限，超出只留预览
MAX_PAYLOAD_BYTES = 8 * 1024


@dataclass
class RunOutcome:
    """一次执行的结果。字段与 attempts 表对应，两个执行器共用。"""

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


@dataclass
class Handled:
    """处理一行事件的结果。

    `events` 是待落库的语义事件；`fatal` 非空表示要立刻终止进程（比如认证失败
    或超过轮数上限）—— **不能只靠退出码**，Pi 在助手回复失败时仍然退 0。
    """

    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    fatal: str | None = None


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


def describe_missing_result(
    exit_code: int | None, stderr_text: str, *, event: str
) -> str:
    """没有拿到收尾事件时，尽量给出有用的原因。

    注意「进程非零退出」和「没收到收尾事件」是两件事：stderr 上的启动期错误
    （比如模型没配、CLI 参数不支持）只会有后者。
    """
    detail = f"stderr: {stderr_text[:500]}".strip() if stderr_text.strip() else ""
    if exit_code not in (0, None):
        kind = "被信号终止" if exit_code < 0 else "非零退出"
        return f"进程{kind}（{exit_code}）且未产生 {event} 事件。{detail}".strip()
    return f"进程结束但未产生 {event} 事件。{detail}".strip()


class AgentRun(ABC):
    """一次执行的事件解释器。每个任务（每次尝试）一个实例。"""

    def __init__(self, config: Config, outcome: RunOutcome):
        self.config = config
        self.outcome = outcome

    @abstractmethod
    def handle(self, event: dict[str, Any]) -> Handled:
        """吃一行事件，吐出要落库的语义事件。"""

    @abstractmethod
    def finish(self, *, exit_code: int | None, stderr_text: str) -> None:
        """进程退出后收尾：补判成败、补错误原因。"""

    @abstractmethod
    def recover(self, events: Sequence[dict[str, Any]]) -> None:
        """从原始日志尾部把收尾信息捞回来（崩在落库与状态更新之间时用）。"""

    def progress(self, event: dict[str, Any]) -> int | None:
        """可选的进度数字（已生成 token），只用于实时提示，不落库。

        Claude Code 有 thinking_tokens；Pi 用 message_update 里的累计 output。
        """
        return None


class AgentRuntime(ABC):
    """一个 CLI 执行器。无状态，可复用。"""

    name: ClassVar[str]

    def __init__(self, config: Config):
        self.config = config

    @abstractmethod
    def build_command(
        self,
        prompt: str,
        *,
        session_id: str,
        context_dirs: Sequence[Path],
        resume: bool,
    ) -> list[str]:
        """构造一次任务执行的命令行。"""

    @abstractmethod
    def new_run(self, outcome: RunOutcome) -> AgentRun:
        """为一次执行创建事件解释器。"""

    @abstractmethod
    def build_oneshot_command(
        self, prompt: str, *, session_id: str, cwd: Path
    ) -> list[str]:
        """构造一次「只出文本、不动手」的调用（变更记录沉淀用）。"""

    def env(self, *, project_name: str, task_id: str) -> dict[str, str]:
        """子进程需要额外注入的环境变量。"""
        return {}
