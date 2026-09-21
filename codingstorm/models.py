"""领域模型与状态枚举。"""

from __future__ import annotations

from enum import StrEnum

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


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = ""
    kind: TaskKind = TaskKind.TASK
    priority: int = 0


class TextPayload(BaseModel):
    text: str


class DocOut(BaseModel):
    path: str
    size: int


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
