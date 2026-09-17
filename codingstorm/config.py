"""配置加载。默认值面向「单用户、低内存的自用执行机」。"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field

DEFAULT_ROOT = Path.home() / "codingstorm"


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8787


class SchedulerConfig(BaseModel):
    # 默认 2 —— 执行机可用内存有限，每个 Claude Code 进程约 200-400MB
    max_concurrent: int = Field(default=2, ge=1, le=8)
    poll_interval_s: float = Field(default=1.0, gt=0)


class TaskConfig(BaseModel):
    max_turns: int = Field(default=50, ge=1)
    wall_clock_timeout_s: int = Field(default=3600, ge=60)
    # 超过这个时长没有任何事件就判定卡死
    idle_timeout_s: int = Field(default=300, ge=30)


class ClaudeConfig(BaseModel):
    binary: str = "claude"
    permission_mode: str = "bypassPermissions"
    # 装配 PreToolUse hook 的 settings 文件；None 表示用 ~/.claude/settings.json
    settings_file: Path | None = None


class Config(BaseModel):
    root: Path = DEFAULT_ROOT
    server: ServerConfig = Field(default_factory=ServerConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    task: TaskConfig = Field(default_factory=TaskConfig)
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)

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

    def ensure_dirs(self) -> None:
        for d in (self.root, self.repos_dir, self.worktrees_dir, self.contexts_dir, self.logs_dir):
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
        return cls.model_validate(data)
