"""共享测试夹具。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def init_repo(path: Path) -> Path:
    """建一个有初始提交的 git 仓库。worktree 操作要求至少有一个提交。

    允许往当前分支 push —— 测试用它来模拟「target 被别处推进了」。
    """
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "-b", "main"], path)
    _git(["config", "user.email", "test@localhost"], path)
    _git(["config", "user.name", "test"], path)
    _git(["config", "commit.gpgsign", "false"], path)
    _git(["config", "receive.denyCurrentBranch", "ignore"], path)
    (path / "README.md").write_text("# test repo\n", encoding="utf-8")
    _git(["add", "-A"], path)
    _git(["commit", "-q", "-m", "init"], path)
    return path


@pytest.fixture
def make_repo(tmp_path: Path):
    def _make(name: str = "repo") -> Path:
        return init_repo(tmp_path / name)

    return _make
