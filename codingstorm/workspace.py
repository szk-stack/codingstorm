"""任务的工作区。

Phase 1 里就是被调度的仓库目录本身 —— 够跑通全流程。
Phase 2 换成「每任务一个 git worktree + 一条 cs/<id> 分支」，届时只需替换这里的实现，
调度器不用动。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from codingstorm.config import Config


@dataclass(frozen=True)
class Workspace:
    path: Path
    branch: str | None = None
    base_commit: str | None = None
    ephemeral: bool = False  # True 表示这是本任务独有的，可以随时删掉重建


class WorkspaceManager:
    def __init__(self, config: Config):
        self.config = config

    async def prepare(self, project_name: str, repo_path: str, task_id: str) -> Workspace:
        path = Path(repo_path)
        if not path.is_dir():
            raise FileNotFoundError(f"项目仓库不存在: {path}")
        return Workspace(path=path, ephemeral=False)

    async def cleanup(self, workspace: Workspace) -> None:
        """只清理一次性工作区；共享目录绝不动。"""
        if not workspace.ephemeral:
            return
        if workspace.path.exists():
            shutil.rmtree(workspace.path, ignore_errors=True)
