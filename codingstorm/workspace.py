"""任务的工作区：每任务一个 git worktree + 一条 cs/<id> 分支。

指导原则（来自参考实现 agent-queue）：

> **worktree 是一次性执行空间；分支、任务历史、会话尝试才是持久产物。**

所以 worktree 出任何问题都可以直接删掉重建，分支一旦建立就是记录。

分支模型是**线性**的：任务从 target 当前 HEAD 切出，完成时 rebase 到最新 target，
批准就是一次 `update-ref` 快进。详见 docs/architecture.md 第 5.5 节。
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from codingstorm.config import Config
from codingstorm.git_ops import Git

log = logging.getLogger("codingstorm.workspace")


@dataclass(frozen=True)
class Workspace:
    path: Path
    repo_path: Path
    branch: str
    base_commit: str
    target_branch: str

    @property
    def ephemeral(self) -> bool:
        """worktree 是一次性的，随时可以删掉重建。"""
        return True


@dataclass
class FinalizeResult:
    commit_sha: str
    had_changes: bool
    rebase_conflict: bool = False
    cleaned: list[str] | None = None


class RebaseConflict(RuntimeError):
    pass


def slugify(text: str, max_len: int = 32) -> str:
    """把任务标题压成能进分支名的片段。非 ASCII 会被丢掉，所以中文标题常常为空。"""
    ascii_part = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return ascii_part[:max_len].strip("-")

class WorkspaceManager:
    # 提交前要挡掉的构建产物。写进仓库的 info/exclude（**未跟踪的本地文件**，
    # 不会进用户历史），但对所有 worktree 生效。
    #
    # 为什么需要：AI 为了验证自己的代码常常会跑一遍，于是产生 __pycache__ 之类的东西；
    # 而收尾用的是 `git add -A`，没有 .gitignore 的仓库就会把这些垃圾一起提交。
    DEFAULT_EXCLUDES = (
        "__pycache__/",
        "*.py[cod]",
        "*.so",
        ".pytest_cache/",
        ".ruff_cache/",
        ".mypy_cache/",
        "node_modules/",
        ".venv/",
        "venv/",
        ".DS_Store",
    )
    _EXCLUDE_MARKER = "# codingstorm: 自动生成，防止把构建产物提交进去"

    def __init__(self, config: Config):
        self.config = config
        self._warned_repos: set[str] = set()
        self._excluded_repos: set[str] = set()
        # 按仓库的写锁。批准的 update-ref 与 runner 的自动提交会并发，
        # 都落在同一个仓库上，得串起来。
        self._repo_locks: dict[str, asyncio.Lock] = {}

    async def _ensure_exclude(self, repo_path: str) -> None:
        if repo_path in self._excluded_repos:
            return
        self._excluded_repos.add(repo_path)

        repo = Git(Path(repo_path))
        raw = (await repo.run("rev-parse", "--git-common-dir", check=False)).strip()
        if not raw:
            return
        git_dir = Path(raw)
        if not git_dir.is_absolute():
            git_dir = Path(repo_path) / git_dir

        exclude = git_dir / "info" / "exclude"
        try:
            existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
            if self._EXCLUDE_MARKER in existing:
                return
            exclude.parent.mkdir(parents=True, exist_ok=True)
            block = "\n".join([self._EXCLUDE_MARKER, *self.DEFAULT_EXCLUDES])
            exclude.write_text(
                existing.rstrip("\n") + "\n\n" + block + "\n", encoding="utf-8"
            )
            log.info("已为 %s 写入默认排除规则（info/exclude）", repo_path)
        except OSError as exc:
            log.warning("写 info/exclude 失败（忽略）: %s", exc)

    def repo_lock(self, repo_path: str) -> asyncio.Lock:
        lock = self._repo_locks.get(repo_path)
        if lock is None:
            lock = asyncio.Lock()
            self._repo_locks[repo_path] = lock
        return lock

    def branch_name(self, task_id: str, title: str) -> str:
        slug = slugify(title)
        return f"cs/{task_id}-{slug}" if slug else f"cs/{task_id}"

    def worktree_path(self, project_name: str, task_id: str) -> Path:
        return self.config.worktrees_dir / project_name / task_id

    async def _warn_if_not_bare(self, repo_path: str) -> None:
        """非裸仓库在第一次批准后工作区会和 HEAD 脱节。

        批准走的是 `update-ref`，它只推进引用，**不同步工作树**。所以如果被调度的仓库
        是非裸的、且 target 分支正被检出，那个工作区会一直停在旧提交上，
        `git status` 看起来像"所有文件都被删了"。

        codingstorm 独占仓库的前提下，直接用裸克隆最干净（`git clone --bare`）。
        """
        if repo_path in self._warned_repos:
            return
        self._warned_repos.add(repo_path)
        repo = Git(Path(repo_path))
        bare = (await repo.run("rev-parse", "--is-bare-repository", check=False)).strip()
        if bare != "true":
            log.warning(
                "项目仓库 %s 不是裸仓库。codingstorm 用 update-ref 推进 target 分支、"
                "不同步工作树，该仓库的主工作区会与 HEAD 脱节。"
                "建议改用裸克隆：git clone --bare <源> <目标>",
                repo_path,
            )

    # ---------- 准备 ----------

    async def prepare(
        self, *, project_name: str, repo_path: str, target_branch: str, task_id: str, title: str
    ) -> Workspace:
        await self._warn_if_not_bare(repo_path)
        await self._ensure_exclude(repo_path)
        async with self.repo_lock(repo_path):
            return await self._prepare(
                project_name=project_name,
                repo_path=repo_path,
                target_branch=target_branch,
                task_id=task_id,
                title=title,
            )

    async def _prepare(
        self, *, project_name: str, repo_path: str, target_branch: str, task_id: str, title: str
    ) -> Workspace:
        repo = Git(Path(repo_path))
        # 崩溃会留下 .git/worktrees/*，不清的话同路径再 add 会报 already exists
        await repo.worktree_prune()

        path = self.worktree_path(project_name, task_id)
        branch = self.branch_name(task_id, title)

        if path.exists():
            # 上次崩溃遗留的角落，清干净再重建
            log.warning("工作区已存在，先清理: %s", path)
            await repo.worktree_remove(path)
            await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)
            await repo.worktree_prune()

        await repo.delete_branch(branch)  # 重试时旧分支要清掉

        base = await repo.head_sha(target_branch)
        await repo.worktree_add(path, branch, base)

        workspace = Workspace(
            path=path,
            repo_path=Path(repo_path),
            branch=branch,
            base_commit=base,
            target_branch=target_branch,
        )
        # 全新 worktree 理论上不会有残留状态，兜一下底
        cleaned = await repo.abort_in_progress(path)
        if cleaned:
            log.warning("新工作区里有残留状态（异常）: %s", cleaned)
        return workspace

    # ---------- 收尾 ----------

    async def finalize(self, workspace: Workspace, *, message: str) -> FinalizeResult:
        async with self.repo_lock(str(workspace.repo_path)):
            return await self._finalize(workspace, message=message)

    async def _finalize(self, workspace: Workspace, *, message: str) -> FinalizeResult:
        """任务执行完后：提交改动 → rebase 到最新 target。

        rebase 放在这里而不是批准时，是为了让「审的 diff」就是「将来合入的内容」。
        """
        repo = Git(workspace.repo_path)

        # agent 可能自己跑了 git 命令并留下半途状态，先清掉
        cleaned = await repo.abort_in_progress(workspace.path)
        if cleaned:
            log.warning("清理了 agent 留下的 git 状态: %s", cleaned)

        committed = await repo.commit_all(message, cwd=workspace.path)
        head = await repo.head_sha(cwd=workspace.path)

        target_sha = await repo.head_sha(workspace.target_branch)
        if await repo.is_ancestor(target_sha, head):
            return FinalizeResult(
                commit_sha=head, had_changes=committed is not None, cleaned=cleaned
            )

        if await repo.rebase_onto(workspace.target_branch, workspace.path):
            new_head = await repo.head_sha(cwd=workspace.path)
            return FinalizeResult(
                commit_sha=new_head, had_changes=committed is not None, cleaned=cleaned
            )

        # rebase 冲突：保留原分支内容供人工处理，不覆盖
        return FinalizeResult(
            commit_sha=head, had_changes=committed is not None,
            rebase_conflict=True, cleaned=cleaned,
        )

    # ---------- 批准 / 丢弃 ----------

    async def approve(self, workspace: Workspace, commit_sha: str) -> str:
        async with self.repo_lock(str(workspace.repo_path)):
            return await self._approve(workspace, commit_sha)

    async def _approve(self, workspace: Workspace, commit_sha: str) -> str:
        """把分支快进到 target。

        幂等：如果 target 已经包含这个提交（上次批准成功但状态没写进库），
        `update-ref` 指向同一个 sha，是空操作。
        """
        repo = Git(workspace.repo_path)
        target_sha = await repo.head_sha(workspace.target_branch)

        if not await repo.is_ancestor(target_sha, commit_sha):
            # 排在我们后面的任务被先批了，target 前进了 —— 重新 rebase 再合
            if not await repo.rebase_onto(workspace.target_branch, workspace.path):
                raise RebaseConflict(
                    f"分支 {workspace.branch} 与 {workspace.target_branch} 有冲突，需要人工处理"
                )
            commit_sha = await repo.head_sha(cwd=workspace.path)

        await repo.fast_forward_ref(workspace.target_branch, commit_sha)
        return commit_sha

    async def cleanup(self, workspace: Workspace, *, delete_branch: bool = False) -> None:
        """删掉 worktree。分支默认保留（它是产物）。"""
        async with self.repo_lock(str(workspace.repo_path)):
            repo = Git(workspace.repo_path)
            await repo.worktree_remove(workspace.path)
            await asyncio.to_thread(shutil.rmtree, workspace.path, ignore_errors=True)
            if delete_branch:
                await repo.delete_branch(workspace.branch)
            await repo.worktree_prune()

    # ---------- diff ----------

    async def diff(self, workspace: Workspace, *, base: str | None = None, head: str | None = None) -> str:
        repo = Git(workspace.repo_path)
        base_ref = base or workspace.target_branch
        head_ref = head or f"refs/heads/{workspace.branch}"
        return await repo.diff(base_ref, head_ref)

    async def diff_stat(self, workspace: Workspace, *, base: str | None = None, head: str | None = None) -> str:
        repo = Git(workspace.repo_path)
        base_ref = base or workspace.target_branch
        head_ref = head or f"refs/heads/{workspace.branch}"
        return await repo.diff_stat(base_ref, head_ref)
