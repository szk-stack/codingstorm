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
    # 任务产出。分支被清掉之后（批准/丢弃时），回看 diff 只能靠它
    commit_sha: str | None = None

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


class RepoError(RuntimeError):
    """仓库不能用。注册项目时校验，或任务开始时才发现 —— 消息直接给用户看。"""


def slugify(text: str, max_len: int = 32) -> str:
    """把任务标题压成能进分支名的片段。非 ASCII 会被丢掉，所以中文标题常常为空。"""
    ascii_part = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return ascii_part[:max_len].strip("-")


async def prepare_repo(
    config: Config, *, name: str, repo_path: str, target_branch: str
) -> Path:
    """注册项目时确定仓库路径，并挡住一眼能看出来的错误。

    - 没给路径：用 `{root}/repos/<name>.git`，不存在就顺手建一个空裸仓库
    - 给了路径：必须存在，且必须是个 git 仓库
    - 目标分支：仓库已有提交时必须存在；空仓库放行（显然是还没 push）
    """
    explicit = bool(repo_path.strip())
    path = Path(repo_path).expanduser() if explicit else config.repos_dir / f"{name}.git"
    # 后面的判定要拿它跟 git 的输出比，必须先归一化
    path = path.resolve()

    if not path.exists():
        if explicit:
            raise RepoError(f"仓库路径不存在：{path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        # 用 path.name，因为 cwd 已经是父目录了
        await Git(path.parent).run("init", "--bare", "-b", target_branch, path.name)
        log.info("已创建空裸仓库 %s，等第一次 push", path)
        return path

    repo = Git(path)
    raw = (await repo.run("rev-parse", "--git-dir", check=False)).strip()
    git_dir = Path(raw) if raw else None
    if git_dir is not None and not git_dir.is_absolute():
        git_dir = path / git_dir
    # `.`（裸仓库）或 `.git`（普通仓库）都算「就在这个路径上」。指向别处说明 git 是
    # 往上找到了某个上级仓库 —— 那不是我们要的（实测：主目录是仓库时，随便一个
    # 普通目录都能通过检查）。
    if git_dir is None or not (git_dir == path or git_dir.parent == path):
        raise RepoError(f"不是 git 仓库：{path}")

    branches = await repo.branches()
    if branches and target_branch not in branches:
        raise RepoError(f"仓库里没有 {target_branch} 分支，现有分支：{'、'.join(branches)}")
    return path


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

        # 注册时校验过，但那时仓库可能是空的（还没 push）。到这里才发现的话，
        # 报清楚原因，别让用户对着一句 ambiguous argument 猜。
        if not await repo.ref_exists(f"refs/heads/{target_branch}"):
            raise RepoError(f"仓库里没有 {target_branch} 分支 —— 空仓库要先从本地 push 一次")

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

    async def _diff_refs(self, repo: Git, workspace: Workspace) -> tuple[str, str]:
        """决定拿哪两个提交来比。

        **两种情形要的答案不一样：**

        - 分支还在（待审）：要比的是「合入之后 target 会变成什么样」，
          所以要拿**当前的 target** 当基准 —— 它可能已经被别的任务推进过了。
        - 分支没了（已批准/丢弃）：`main` 这时已经包含那个提交，
          再跟 main 比就是空的。这时要比的是「**这次任务改了什么**」，
          基准得退回任务的 **base_commit**。

        回归：早期只会按 `refs/heads/<分支>` 取，分支一删就报 ambiguous argument，
        合完彻底看不到改动；改成退到 commit_sha 后又发现基准错了，diff 是空的。
        """
        ref = f"refs/heads/{workspace.branch}"
        if await repo.ref_exists(ref):
            return workspace.target_branch, ref
        if workspace.commit_sha and workspace.base_commit:
            return workspace.base_commit, workspace.commit_sha
        return workspace.target_branch, ref

    async def diff(self, workspace: Workspace, *, base: str | None = None, head: str | None = None) -> str:
        repo = Git(workspace.repo_path)
        default_base, default_head = await self._diff_refs(repo, workspace)
        return await repo.diff(base or default_base, head or default_head)

    async def diff_stat(self, workspace: Workspace, *, base: str | None = None, head: str | None = None) -> str:
        repo = Git(workspace.repo_path)
        default_base, default_head = await self._diff_refs(repo, workspace)
        return await repo.diff_stat(base or default_base, head or default_head)
