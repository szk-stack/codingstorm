"""领域模型与状态枚举。"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    MERGED = "merged"
    DISCARDED = "discarded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class TaskKind(StrEnum):
    """只是标签，不改变执行流程。"""

    REQUIREMENT = "requirement"
    INSTRUCTION = "instruction"
    BUG = "bug"
    TASK = "task"


# 终态：不会再变化
TERMINAL_STATUSES = frozenset(
    {TaskStatus.MERGED, TaskStatus.DISCARDED, TaskStatus.CANCELLED}
)

# 需要人工介入的状态
ACTIONABLE_STATUSES = frozenset({TaskStatus.AWAITING_REVIEW, TaskStatus.FAILED})


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    # 留空则用 {root}/repos/<name>.git；不存在就建一个空裸仓库
    repo_path: str = ""
    target_branch: str = "main"


class ScheduleRuleIn(BaseModel):
    """定时规则的输入形状。三种类型共用一个模型，按 type 取用对应字段。"""

    type: Literal["once", "daily", "weekly"]
    at: str | None = None  # once：「2026-09-23T09:00」，用户时区的本地时刻
    time: str | None = None  # daily / weekly：「09:00」
    days: list[str] = Field(default_factory=list)  # weekly：["mon","wed"]


class ScheduleCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = ""
    kind: TaskKind = TaskKind.TASK
    priority: int = 0
    rule: ScheduleRuleIn


class ScheduleUpdate(BaseModel):
    enabled: bool | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = None
    priority: int | None = None
    rule: ScheduleRuleIn | None = None


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = ""
    kind: TaskKind = TaskKind.TASK
    priority: int = 0


class TaskMessageIn(BaseModel):
    """追加一轮对话。"""

    text: str = Field(min_length=1, max_length=10_000)


class TaskMessageOut(BaseModel):
    seq: int
    text: str
    created_at: str


class TextPayload(BaseModel):
    text: str


class DocOut(BaseModel):
    path: str
    size: int


class FileEntry(BaseModel):
    name: str
    path: str  # 相对仓库根
    type: str  # "dir" / "file"
    size: int | None = None


class TreeOut(BaseModel):
    ref: str
    path: str
    entries: list[FileEntry]


class FileOut(BaseModel):
    ref: str
    path: str
    size: int
    binary: bool
    truncated: bool = False
    text: str = ""


class ContextOut(BaseModel):
    pointer: str
    pointer_bytes: int
    pointer_soft_limit: int
    journal: str
    index: str
    docs: list[DocOut]


class ProjectOut(BaseModel):
    id: str
    name: str
    repo_path: str
    target_branch: str
    enabled: bool
    created_at: str
    # NULL = 走全局窗口；见 timing.WindowOverride
    window_override: str | None = None


class TaskOut(BaseModel):
    id: str
    project_id: str
    title: str
    body: str
    kind: str
    status: str
    priority: int
    branch: str | None = None
    worktree_path: str | None = None
    base_commit: str | None = None
    commit_sha: str | None = None
    merge_commit_sha: str | None = None
    last_event_at: str | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    error_text: str | None = None
    # 由哪条定时任务生成；手工提交的任务为空
    schedule_id: str | None = None


class ScheduleOut(BaseModel):
    """定时任务。

    `next_run_at` 是 UTC，**显示给用户时要用 `window.timezone` 换算** ——
    用户配的「每天 9 点」指的是那个时区的 9 点，不是浏览器所在地的 9 点。
    """

    id: str
    project_id: str
    title: str
    body: str
    kind: str
    priority: int
    rule: str
    rule_text: str = ""
    next_run_at: str | None = None
    enabled: bool
    last_run_at: str | None = None
    last_task_id: str | None = None
    # 上一轮生成的还在待审 —— 界面据此提示「本轮会切在旧主干上」
    last_task_status: str | None = None
    run_count: int
    missed_at: str | None = None
    created_at: str


class WindowOut(BaseModel):
    """执行窗口的当前状态，给界面画那条横幅用。"""

    enabled: bool  # 配置里启用了窗口
    disabled: bool  # 被临时关掉了（内存态，重启恢复）
    open: bool  # 全局此刻是否放行
    timezone: str
    windows: list[list[str]]
    now: str
    next_open_at: str | None = None
    next_close_at: str | None = None


class WindowToggle(BaseModel):
    disabled: bool


class ProjectWindowIn(BaseModel):
    """项目对全局执行窗口的覆盖。"""

    mode: Literal["inherit", "always", "custom"] = "inherit"
    windows: list[list[str]] = Field(default_factory=list)


class AttemptOut(BaseModel):
    attempt_no: int
    session_id: str | None = None
    model: str | None = None
    exit_code: int | None = None
    result_subtype: str | None = None
    is_error: bool | None = None
    # 没跑起来就失败的那种（比如仓库还是空的），只有它说明原因
    error_text: str | None = None
    num_turns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cost_usd: float | None = None
    price_version: str | None = None
    origin: str = "task"
    started_at: str | None = None
    finished_at: str | None = None


class ModelUsage(BaseModel):
    model: str | None = None
    attempts: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float | None = None


class UsageOut(BaseModel):
    """花钱可追溯：token 按模型和来源分开统计。

    沉淀那步的开销单列 —— 它是每个任务的固定成本，混进任务里就看不见了。
    """
    total_attempts: int
    task_attempts: int
    sediment_attempts: int
    by_model: list[ModelUsage]
    price_version: str = ""
    prices_configured: bool = False
    note: str = ""
