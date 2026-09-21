"""Git 操作。

这一层只负责把 git 命令跑对、把错误说清楚；「什么时候该做哪一步」由
`workspace.py` 和 `scheduler.py` 决定。

几个刻意的选择（依据见 docs/architecture.md 第 5.5 / 5.7 节）：

- **批准走 `update-ref` 快进，不 checkout 也不 merge。** 分支在批准前已经 rebase 到
  target 上，所以就是快进。这样不碰任何工作树、不产生 MERGE_HEAD、不留下半途状态，
  而且天然原子。
- **清理工作区前先中止残留操作。** 被 kill 的 agent 会留下半途的 rebase/merge 和
  僵尸 `index.lock`，不清掉的话后续所有 git 操作都会被卡死。
- **`clean -fd` 不带 `-x`。** `-x` 会连 gitignore 的缓存一起删（`node_modules`、
  `.venv`），下一个任务要花几分钟重新装。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    def __init__(self, args: tuple[str, ...], returncode: int, stdout: str, stderr: str):
        self.args_ = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"git {' '.join(args)} 失败（退出码 {returncode}）: {stderr.strip() or stdout.strip()}"
        )


@dataclass(frozen=True)
class WorktreeEntry:
    path: Path
    branch: str | None
    head: str | None


@dataclass(frozen=True)
class TreeEntry:
    name: str
    type: str  # "tree" / "blob"
    size: int | None  # 目录没有大小


def check_ref(ref: str) -> None:
    """ref 来自 HTTP，必须先挡住两类东西。

    - **以 `-` 开头**：会被 git 当成选项（实测 `ls-tree --evil` 报 unknown option）
    - 含 `:` 或 `^` `~` 等：`<ref>:<path>` 的语法会被拆错
    """
    if not ref or ref.startswith("-") or any(c in ref for c in " \t\n:^~?*[\\"):
        raise ValueError(f"非法的版本引用：{ref!r}")


class Git:
    def __init__(self, path: Path):
        self.path = Path(path)

    # ---------- 底层 ----------

    async def _exec_raw(self, args: tuple[str, ...], cwd: Path) -> tuple[int, bytes, str]:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return proc.returncode or 0, out, err.decode("utf-8", "replace")

    async def _exec(self, args: tuple[str, ...], cwd: Path) -> tuple[int, str, str]:
        code, out, err = await self._exec_raw(args, cwd)
        return code, out.decode("utf-8", "replace"), err

    async def run(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        code, out, err = await self._exec(args, cwd or self.path)
        if check and code != 0:
            raise GitError(args, code, out, err)
        return out

    async def ok(self, *args: str, cwd: Path | None = None) -> bool:
        """只关心成功与否，不抛异常。"""
        code, _, _ = await self._exec(args, cwd or self.path)
        return code == 0

    # ---------- 查询 ----------

    async def head_sha(self, ref: str = "HEAD", *, cwd: Path | None = None) -> str:
        return (await self.run("rev-parse", ref, cwd=cwd)).strip()

    async def ref_exists(self, ref: str) -> bool:
        return await self.ok("rev-parse", "--verify", "--quiet", ref)

    async def branches(self) -> list[str]:
        out = await self.run("for-each-ref", "--format=%(refname:short)", "refs/heads/")
        return [line.strip() for line in out.splitlines() if line.strip()]

    async def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """用于判断「这个提交是否已经包含在目标分支里」—— 批准幂等的关键。"""
        return await self.ok("merge-base", "--is-ancestor", ancestor, descendant)

    async def has_uncommitted_changes(self, cwd: Path | None = None) -> bool:
        out = await self.run("status", "--porcelain", cwd=cwd)
        return bool(out.strip())

    async def is_clean(self, cwd: Path | None = None) -> bool:
        return not await self.has_uncommitted_changes(cwd)

    # ---------- 浏览仓库内容（不检出工作区） ----------

    async def ls_tree(self, ref: str, path: str = "") -> list[TreeEntry]:
        """列出某个版本下某个目录的直接子项。裸仓库也能用 —— 这是能在
        「项目里有什么」这件事上不落工作区的唯一办法。"""
        check_ref(ref)
        spec = f"{ref}:{path}" if path else ref
        # quotePath=false：否则非 ASCII 文件名会被转义成 \346\226\207 这种八进制
        out = await self.run("-c", "core.quotePath=false", "ls-tree", "-l", spec)
        entries: list[TreeEntry] = []
        for line in out.splitlines():
            meta, _, name = line.partition("\t")
            parts = meta.split()
            if not name or len(parts) < 4:
                continue
            entries.append(
                TreeEntry(
                    name=name,
                    type=parts[1],
                    size=None if parts[3] == "-" else int(parts[3]),
                )
            )
        return entries

    async def blob(self, ref: str, path: str) -> tuple[bytes, bool]:
        """读一个文件，返回 (内容, 是否二进制)。

        用 cat-file 而不是 show —— show 会给内容加一层格式化。
        """
        check_ref(ref)
        args = ("cat-file", "blob", f"{ref}:{path}")
        code, out, err = await self._exec_raw(args, self.path)
        if code != 0:
            raise GitError(args, code, err, err)
        # 判据抄 git 自己的：前 8000 字节里有没有 NUL
        return out, b"\x00" in out[:8000]

    # ---------- worktree ----------

    async def worktree_add(self, path: Path, branch: str, base: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        await self.run("worktree", "add", "-b", branch, str(path), base)

    async def worktree_checkout(self, path: Path, branch: str) -> None:
        """把**已有**分支检出到新工作区。和 worktree_add 的区别是它不新建分支 ——
        多轮对话接着跑时靠它，前几轮的提交都还在那条分支上。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        await self.run("worktree", "add", str(path), branch)

    async def worktree_remove(self, path: Path, *, force: bool = True) -> None:
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(path))
        await self.run(*args, check=False)

    async def worktree_prune(self) -> None:
        """崩溃会留下 .git/worktrees/*，不清的话同路径再 add 会报 already exists。"""
        await self.run("worktree", "prune", check=False)

    async def worktree_list(self) -> list[WorktreeEntry]:
        out = await self.run("worktree", "list", "--porcelain")
        entries: list[WorktreeEntry] = []
        cur: dict[str, str] = {}
        for line in out.splitlines():
            if not line.strip():
                if cur:
                    entries.append(
                        WorktreeEntry(
                            path=Path(cur["worktree"]),
                            branch=cur.get("branch", "").removeprefix("refs/heads/") or None,
                            head=cur.get("HEAD"),
                        )
                    )
                    cur = {}
                continue
            key, _, value = line.partition(" ")
            cur[key] = value
        if cur:
            entries.append(
                WorktreeEntry(
                    path=Path(cur["worktree"]),
                    branch=cur.get("branch", "").removeprefix("refs/heads/") or None,
                    head=cur.get("HEAD"),
                )
            )
        return entries

    # ---------- 提交 ----------

    async def commit_all(self, message: str, cwd: Path | None = None) -> str | None:
        """把所有改动提交掉。没有改动时返回 None（不是错误）。"""
        if not await self.has_uncommitted_changes(cwd):
            return None
        await self.run("add", "-A", cwd=cwd)
        await self.run(
            "-c", "user.name=codingstorm", "-c", "user.email=cs@localhost",
            "commit", "-m", message, cwd=cwd,
        )
        return await self.head_sha(cwd=cwd)

    # ---------- rebase ----------

    async def rebase_onto(self, target: str, cwd: Path) -> bool:
        """把 cwd 所在分支 rebase 到 target 上。返回是否成功（冲突则 False 并自动复原）。"""
        code, _, _ = await self._exec(("rebase", target), cwd)
        if code == 0:
            return True
        await self.run("rebase", "--abort", cwd=cwd, check=False)
        return False

    async def abort_in_progress(self, cwd: Path) -> list[str]:
        """清掉半途状态。必须在复用工作区之前调用。

        被 kill 的 agent 会留下 rebase/merge 的半途状态和 `index.lock`，
        不清的话**后续所有 git 操作都会被卡死**。
        """
        cleaned: list[str] = []
        raw = (await self.run("rev-parse", "--git-dir", cwd=cwd)).strip()
        git_dir = Path(raw)
        if not git_dir.is_absolute():
            git_dir = (cwd / git_dir).resolve()

        if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
            if await self.ok("rebase", "--abort", cwd=cwd):
                cleaned.append("中止了残留的 rebase")

        if (git_dir / "MERGE_HEAD").exists():
            if await self.ok("merge", "--abort", cwd=cwd):
                cleaned.append("中止了残留的 merge")

        lock = git_dir / "index.lock"
        if lock.exists():
            # 工作区由 codingstorm 独占，此刻不该有任何 git 进程在跑，
            # 留着的必定是上一条被 kill 的任务留下的僵尸锁。
            try:
                lock.unlink()
                cleaned.append("删除了僵尸 index.lock")
            except OSError:
                pass
        return cleaned

    async def reset_and_clean(self, mode: str, cwd: Path) -> None:
        """清空工作区的改动。`-fd` 刻意不带 `-x` —— 保留 gitignore 的缓存。"""
        await self.run("reset", mode, cwd=cwd)
        await self.run("clean", "-fd", cwd=cwd)

    # ---------- 分支与引用 ----------

    async def fast_forward_ref(self, ref: str, sha: str) -> None:
        """把分支直接推到某个提交。线性保证下这就是快进合并。

        不 checkout、不 merge —— 不碰工作树，不产生 MERGE_HEAD，
        也不会因为工作树脏而失败或带上脏改动。
        """
        await self.run("update-ref", f"refs/heads/{ref}", sha)

    async def delete_branch(self, branch: str, *, force: bool = True) -> None:
        flag = "-D" if force else "-d"
        await self.run("branch", flag, branch, check=False)

    # ---------- diff ----------

    async def diff(self, base: str, head: str, *, max_bytes: int = 512 * 1024) -> str:
        """输出 base..head 的 diff。超长时截断并注明。"""
        code, out, err = await self._exec(
            ("--no-pager", "diff", "--no-color", "--find-renames", f"{base}..{head}"), self.path
        )
        if code != 0 and not out:
            raise GitError(("diff",), code, out, err)
        if len(out.encode("utf-8")) > max_bytes:
            head_part = out.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")
            return head_part + f"\n\n[diff 已截断，超出 {max_bytes} 字节]\n"
        return out

    async def diff_stat(self, base: str, head: str) -> str:
        return await self.run(
            "--no-pager", "diff", "--no-color", "--stat", f"{base}..{head}", check=False
        )
