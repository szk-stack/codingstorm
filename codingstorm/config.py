"""配置加载。默认值面向「单用户、低内存的自用执行机」。"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_ROOT = Path.home() / "codingstorm"

# 执行器。项目可以覆盖全局默认值（见 ProjectCreate.runtime）。
RuntimeName = Literal["claude", "pi"]


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8788


class WindowConfig(BaseModel):
    """执行窗口 —— 只在指定时段启动新任务。

    动机是**谷时定价**：DeepSeek 之类的中转在凌晨有大幅折扣，白天提交、夜里跑
    比立刻跑便宜得多。它只拦新任务的启动，**不打断已经跑起来的**（中途 kill 掉的
    钱不会退，重跑还要再花一遍）。

    注意它和「定时任务」是两件事：定时任务决定**什么时候入队**，窗口决定
    **什么时候能开始执行**。设成「每天 9 点」的定时任务，配上 00:30–08:30 的窗口，
    入队后要等到第二天凌晨才真正开跑。
    """

    # 默认关 —— 不能因为升了个版本就让所有人的任务突然开始排队等窗口
    enabled: bool = False
    # 必须显式写时区。系统时区可能是 UTC，而用户说的「凌晨」指的是自己那边的凌晨。
    timezone: str = "Asia/Shanghai"
    # 支持多段；start > end 表示跨天（22:00–06:00）
    windows: list[list[str]] = Field(default_factory=lambda: [["00:30", "08:30"]])


class SchedulerConfig(BaseModel):
    # 默认 2 —— 执行机可用内存有限，每个 Claude Code 进程约 200-400MB
    # （Pi 是 Node 进程，占用不同但同样不轻）
    max_concurrent: int = Field(default=2, ge=1, le=8)
    poll_interval_s: float = Field(default=1.0, gt=0)
    # 定时任务迟到多久还算「准时」。
    # 轮询是一秒一次，正常不会晚；只有平台重启/停机才会迟到。宽限期内的补跑一次，
    # 超过就跳过（一次性任务标记为「已错过」留在列表里，不静默消失）。
    misfire_grace_s: int = Field(default=600, ge=0)
    window: WindowConfig = Field(default_factory=WindowConfig)


class TaskConfig(BaseModel):
    max_turns: int = Field(default=50, ge=1)
    wall_clock_timeout_s: int = Field(default=3600, ge=60)
    # 超过这个时长没有任何事件就判定卡死
    idle_timeout_s: int = Field(default=300, ge=30)


class ContextConfig(BaseModel):
    # 任务结束后自动生成变更记录。
    # **这是整套机制能否活过三个月的前提** —— 靠人记得去维护的文档一定会腐烂
    # （Cline 的 Memory Bank 就是这么死的）。代价是每个任务多一次轻量模型调用。
    sediment: bool = True
    sediment_max_diff_bytes: int = Field(default=24 * 1024, ge=1024)
    # 注入 prompt 的变更记录上限；它会一直增长，全量塞进去迟早挤爆上下文
    journal_prompt_chars: int = Field(default=6000, ge=500)


class AgentsConfig(BaseModel):
    """Phase 8 严格模式。

    `enabled` 默认关 —— 跟执行窗口一个道理：升个版本不能让现有任务突然开始等人点头。
    `strict_kinds` 只是便利项（`kind` 命中时自动走 strict），任务真正的流程由 `TaskCreate.mode` 决定。
    """

    enabled: bool = False
    strict_kinds: list[str] = Field(default_factory=list)
    max_review_rounds: int = Field(default=6, ge=1, le=20)


class ClaudeConfig(BaseModel):
    binary: str = "claude"
    permission_mode: str = "bypassPermissions"
    # 装配 PreToolUse hook 的 settings 文件；None 表示用 ~/.claude/settings.json
    settings_file: Path | None = None

    # ---- 边界拦截 ----
    # bypassPermissions 会绕过所有权限检查，所以 permissions.deny 不可靠，
    # 边界只能靠 PreToolUse hook（它不受权限模式影响）。
    guard_enabled: bool = True
    guard_block_push: bool = True
    # 默认关闭：会一并挡掉正常的依赖安装，容易误伤
    guard_block_network: bool = False
    guard_extra_deny: list[str] = []
    guard_allow: list[str] = []


class PricingConfig(BaseModel):
    # 价目表文件。**成本必须自己按 token 算** —— 第三方中转下
    # `total_cost_usd` 是按另一端价目算的，跟实际付费对不上。
    prices_file: Path | None = None


class RuntimeConfig(BaseModel):
    """用哪个执行器跑任务。

    默认 claude —— 升级版本不能让已有部署突然换一套 CLI 行为。项目级可以覆盖
    （注册时的 `runtime` 字段），所以同一台机器上可以一个项目用 Claude Code、
    另一个项目用 Pi。
    """

    default: RuntimeName = "claude"


class PiConfig(BaseModel):
    """Pi Coding Agent（`@earendil-works/pi-coding-agent`）。

    和 Claude Code 的关键差异（详见 docs/pi-agent-integration.md）：

    - 它是 **JSON 事件流**（`--mode json`），不是 Anthropic 的 stream-json；
      事件语义靠 `agents/pi.py` 翻译成平台统一的 init/assistant/tool_result/result。
    - **没有权限系统**，默认就按启动用户的权限跑。边界拦截只能靠一个 `tool_call`
      扩展去做（平台会生成 `{root}/guard/pi-guard.ts`，用 `-e` 挂上）。
    - **没有 `--max-turns`**，所以轮数上限由平台数 `turn_end` 事件来执行。
    - 会话用 `--session-id` 打开或新建，和 Claude Code 的语义一致。
    """

    binary: str = "pi"
    # 留空 = 用 Pi 自己 settings.json 里的默认模型；指定时支持 `provider/id` 与 `:thinking` 后缀
    model: str | None = None
    provider: str | None = None
    thinking: Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None
    # 项目信任：ignore = 不加载仓库里的 .pi 配置与扩展（无人值守的默认，安全）；
    # approve = 信任并加载；inherit = 不传开关，交给 Pi 自己的配置决定。
    project_trust: Literal["ignore", "approve", "inherit"] = "ignore"
    # 会话文件目录。留空 = Pi 的默认位置（~/.pi/agent/sessions，按 cwd 分组）。
    session_dir: Path | None = None
    # 透传参数，用于 Pi 升级后新增的开关；排在平台参数之后、prompt 之前
    extra_args: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _provider_needs_model(self) -> "PiConfig":
        # Pi 的 --provider 必须和 --model 一起用，单独传会直接报错退出
        if self.provider and not self.model:
            raise ValueError("[pi].provider 必须和 [pi].model 一起设置")
        return self

    @field_validator("model", "provider", "thinking", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        # TOML 里留空写成 "" 是常见写法，别让它变成 422/校验错误
        return value or None


class Config(BaseModel):
    root: Path = DEFAULT_ROOT
    server: ServerConfig = Field(default_factory=ServerConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    task: TaskConfig = Field(default_factory=TaskConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    pi: PiConfig = Field(default_factory=PiConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)

    @property
    def db_path(self) -> Path:
        return self.root / "codingstorm.db"

    @property
    def repos_dir(self) -> Path:
        return self.root / "repos"

    @property
    def worktrees_dir(self) -> Path:
        return self.root / "worktrees"

    @property
    def contexts_dir(self) -> Path:
        return self.root / "contexts"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def runs_dir(self) -> Path:
        """Phase 8 的运行目录：**在 worktree 之外**。

        收尾的 `git add -A` 会把 worktree 里的新文件一起提交进 diff，
        阶段产物放里面会污染审查视图（Phase 2 已被 __pycache__ 坑过一次）。
        """
        return self.root / "runs"

    def ensure_dirs(self) -> None:
        for d in (
            self.root, self.repos_dir, self.worktrees_dir,
            self.contexts_dir, self.logs_dir, self.runs_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    def task_log_path(self, project: str, task_id: str) -> Path:
        return self.logs_dir / project / f"{task_id}.ndjson"

    def task_stderr_path(self, project: str, task_id: str) -> Path:
        return self.logs_dir / project / f"{task_id}.stderr"

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        """从 TOML 读配置。文件不存在时全部用默认值。"""
        if path is None:
            return cls()
        if not path.exists():
            raise FileNotFoundError(f"配置文件不存在: {path}")
        with path.open("rb") as f:
            data = tomllib.load(f)
        if "root" in data:
            data["root"] = Path(data["root"]).expanduser()
        # 嵌套的路径字段也要展开，否则 `~` 会被当成字面目录名
        pi = data.get("pi")
        if isinstance(pi, dict) and pi.get("session_dir"):
            pi["session_dir"] = Path(pi["session_dir"]).expanduser()
        return cls.model_validate(data)
