"""执行器抽象。

`runner` 只负责和进程打交道的那部分 —— 拉起子进程、tail 日志文件、看门狗、
把语义事件落库。**某一行 JSON 是什么意思，是执行器自己的事**。

这样 Claude Code 的 stream-json 与 Pi 的 JSONL 事件流不会渗进调度器：
调度器只认 `init / assistant / tool_result / result` 四种语义事件，
Web UI、成本记账、多轮会话都不用知道底下换了谁。
"""

from __future__ import annotations

import json
import os
import re
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Sequence

from codingstorm.config import Config

# 单条 payload 的落库上限，超出只留预览
MAX_PAYLOAD_BYTES = 8 * 1024


def resolve_executable(binary: str, *, windows: bool | None = None) -> str:
    """把 npm 的 Windows 垫片解析成真正可执行的 `.exe`。

    npm 全局安装会同时生成 `claude`（POSIX sh 脚本）、`claude.cmd`、`claude.ps1`。
    Python 的 `create_subprocess_exec` 在 Windows 上：

    - 不能执行无扩展名的 sh 脚本 → `[WinError 2] 系统找不到指定的文件`
    - 能执行 `.cmd`，但要经过 `cmd.exe`，而 cmd.exe 的命令行上限只有 8191 字符，
      prompt 一长（项目约定 + 变更记录）就会被截断/报错

    Claude Code 的 `.cmd` 里指向包内的 `bin\\claude.exe`，解析出来直接启动即可。
    解析失败就退回 `.cmd`（短 prompt 仍能跑），至少不会因为路径形态直接失败。
    """
    if windows is None:
        windows = os.name == "nt"
    if not windows:
        return binary

    path = Path(binary)
    if not path.is_file():
        found = shutil.which(binary)
        if not found:
            return binary
        path = Path(found)

    if path.suffix.lower() in (".exe", ".com"):
        return str(path)

    if path.suffix.lower() in (".cmd", ".bat"):
        target = _exe_from_cmd(path)
        return str(target) if target is not None else str(path)

    # 无扩展名的 npm sh 脚本：同目录有 .cmd 就走它（再尝试解析 .exe）
    sibling = path.with_suffix(".cmd")
    if sibling.is_file():
        return resolve_executable(str(sibling), windows=True)
    return str(path)


def _exe_from_cmd(path: Path) -> Path | None:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None

    # npm 垫片里是 "%dp0%\node_modules\...\claude.exe"
    text = text.replace("%~dp0", str(path.parent) + os.sep)
    text = text.replace("%dp0%", str(path.parent))
    for match in re.finditer(r'"([^"]+\.exe)"', text, re.IGNORECASE):
        candidate = Path(match.group(1))
        if candidate.is_file():
            return candidate
    return None



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
        model: str | None = None,
    ) -> list[str]:
        """构造一次任务执行的命令行。

        `model` 是项目级覆盖（None = 用执行器自己的全局配置）。不是每个执行器
        都有可切换的模型，忽略它的实现要在注释里说清楚，别静默吞掉。
        """

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
