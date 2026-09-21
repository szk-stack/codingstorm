"""工作区与 git 生命周期测试。用真实 git 仓库。"""

import asyncio
import subprocess
from pathlib import Path

import pytest

from codingstorm.config import Config
from codingstorm.git_ops import Git
from codingstorm.workspace import RebaseConflict, RepoError, WorkspaceManager


def run(coro):
    return asyncio.run(coro)


def git(args: list[str], cwd: Path, check: bool = True) -> str:
    # 必须显式指定 utf-8 —— Windows 上默认按 GBK 解码，中文提交信息会直接崩
    p = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8"
    )
    if check and p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} 失败: {p.stderr}")
    return (p.stdout or "").strip()


def git_ok(args: list[str], cwd: Path) -> bool:
    p = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8"
    )
    return p.returncode == 0


def rebase_in_progress(worktree: Path) -> bool:
    raw = git(["rev-parse", "--git-dir"], worktree)
    git_dir = Path(raw)
    if not git_dir.is_absolute():
        git_dir = worktree / git_dir
    return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()


@pytest.fixture
def env(tmp_path: Path, make_repo):
    cfg = Config(root=tmp_path / "root")
    cfg.ensure_dirs()
    repo = make_repo()
    return cfg, WorkspaceManager(cfg), repo


def _write(path: Path, name: str, content: str) -> None:
    (path / name).write_text(content, encoding="utf-8")


# ---------- 工作区生命周期 ----------

def test_prepare_reports_missing_target_branch(env, tmp_path):
    """空仓库（用户还没 push）时，别让任务对着一句 ambiguous argument 猜。"""
    _, wm, _ = env
    empty = tmp_path / "empty.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(empty)], check=True)

    with pytest.raises(RepoError, match="要先从本地 push"):
        run(wm.prepare(
            project_name="p", repo_path=str(empty), target_branch="main",
            task_id="t1", title="x",
        ))


def test_worktree_is_isolated_from_main_repo(env):
    """worktree 里改文件不该影响主仓库的工作区。"""
    _, wm, repo = env

    async def main():
        ws = await wm.prepare(
            project_name="p", repo_path=str(repo), target_branch="main",
            task_id="t1", title="add feature",
        )
        assert ws.path != repo
        assert ws.path.exists()
        assert ws.branch == "cs/t1-add-feature"
        assert ws.base_commit == git(["rev-parse", "main"], repo)

        _write(ws.path, "new.txt", "hello")
        assert not (repo / "new.txt").exists(), "主仓库不该被污染"
        assert git(["branch", "--show-current"], ws.path) == ws.branch

    run(main())


def test_prepare_cleans_leftover_branch_and_dir(env):
    """重试时必须重建 —— 旧 worktree 带着上次未提交的改动且基于旧基点。"""
    _, wm, repo = env

    async def main():
        ws1 = await wm.prepare(
            project_name="p", repo_path=str(repo), target_branch="main",
            task_id="t1", title="x",
        )
        _write(ws1.path, "dirty.txt", "未提交的脏改动")

        # 不清理直接再 prepare 同名任务（模拟重试）
        ws2 = await wm.prepare(
            project_name="p", repo_path=str(repo), target_branch="main",
            task_id="t1", title="x",
        )
        assert not (ws2.path / "dirty.txt").exists(), "上次的脏改动必须被清掉"
        assert git(["status", "--porcelain"], ws2.path) == ""

    run(main())


def test_cleanup_removes_worktree_but_keeps_branch(env):
    """分支是持久产物，默认不删。"""
    _, wm, repo = env

    async def main():
        ws = await wm.prepare(
            project_name="p", repo_path=str(repo), target_branch="main",
            task_id="t1", title="x",
        )
        _write(ws.path, "f.txt", "x")
        sha = git(["rev-parse", "HEAD"], ws.path)

        await wm.cleanup(ws, delete_branch=False)
        assert not ws.path.exists()
        assert git(["rev-parse", "--verify", ws.branch], repo)  # 分支还在

    run(main())


def test_slugify_falls_back_for_non_ascii(env):
    """中文会被 slug 丢弃，分支名要退化成合法形式而不是空的或带奇怪字符。"""
    _, wm, _ = env
    assert wm.branch_name("t1", "add login feature") == "cs/t1-add-login-feature"
    assert wm.branch_name("t2", "完全中文的标题") == "cs/t2"
    assert wm.branch_name("t3", "修复 login bug") == "cs/t3-login-bug"
    # 不能出现 git 不允许的字符
    for title in ("a b/c", "with:colon", "trailing---", "  spaces  "):
        name = wm.branch_name("t9", title)
        assert " " not in name and ":" not in name
        assert not name.endswith("-")


# ---------- 收尾：提交与 rebase ----------

def test_finalize_commits_changes(env):
    _, wm, repo = env

    async def main():
        ws = await wm.prepare(
            project_name="p", repo_path=str(repo), target_branch="main",
            task_id="t1", title="x",
        )
        _write(ws.path, "feature.py", "print('hi')")
        result = await wm.finalize(ws, message="加一个 feature")

        assert result.had_changes
        assert not result.rebase_conflict
        assert result.commit_sha != ws.base_commit
        assert git(["log", "-1", "--format=%s"], ws.path) == "加一个 feature"

    run(main())


def test_finalize_without_changes_is_not_an_error(env):
    _, wm, repo = env

    async def main():
        ws = await wm.prepare(
            project_name="p", repo_path=str(repo), target_branch="main",
            task_id="t1", title="x",
        )
        result = await wm.finalize(ws, message="什么都没干")
        assert not result.had_changes
        assert result.commit_sha == ws.base_commit

    run(main())


def test_finalize_rebases_onto_advanced_target(env):
    """任务跑的时候 target 前进了（另一个任务被批准），收尾要 rebase 上去。"""
    _, wm, repo = env

    async def main():
        ws = await wm.prepare(
            project_name="p", repo_path=str(repo), target_branch="main",
            task_id="t1", title="x",
        )
        _write(ws.path, "mine.txt", "来自任务")

        # 别处往 main 推进了一个提交
        other = repo.parent / "other"
        subprocess.run(["git", "clone", "-q", str(repo), str(other)], check=True)
        git(["config", "user.email", "o@l"], other)
        git(["config", "user.name", "o"], other)
        git(["checkout", "-q", "main"], other)
        _write(other, "theirs.txt", "来自别处")
        git(["add", "-A"], other)
        git(["commit", "-q", "-m", "别处的改动"], other)
        git(["push", "-q", "origin", "main"], other)

        result = await wm.finalize(ws, message="加 mine.txt")
        assert not result.rebase_conflict
        # rebase 后 main 应当成为任务分支的祖先，两边内容都在
        assert git_ok(["merge-base", "--is-ancestor", "main", "HEAD"], ws.path)
        assert (ws.path / "theirs.txt").exists()
        assert (ws.path / "mine.txt").exists()

    run(main())


def test_finalize_detects_rebase_conflict(env):
    """两边改同一个文件同一行 → 冲突要被识别出来且不破坏现场。"""
    _, wm, repo = env

    async def main():
        ws = await wm.prepare(
            project_name="p", repo_path=str(repo), target_branch="main",
            task_id="t1", title="x",
        )
        _write(ws.path, "README.md", "任务改的内容\n")

        other = repo.parent / "other2"
        subprocess.run(["git", "clone", "-q", str(repo), str(other)], check=True)
        git(["config", "user.email", "o@l"], other)
        git(["config", "user.name", "o"], other)
        git(["checkout", "-q", "main"], other)
        _write(other, "README.md", "别处改的内容\n")
        git(["add", "-A"], other)
        git(["commit", "-q", "-m", "冲突改动"], other)
        git(["push", "-q", "origin", "main"], other)

        result = await wm.finalize(ws, message="改 README")
        assert result.rebase_conflict, "应当识别出冲突"
        # 冲突后不能留下半途的 rebase 状态，否则后续所有 git 操作都被卡死
        assert not rebase_in_progress(ws.path)
        # 分支内容应当保留（没被 abort 掉）
        assert git(["log", "-1", "--format=%s"], ws.path) == "改 README"

    run(main())


def test_build_artifacts_are_not_committed(env):
    """AI 为了验证代码会跑一遍，产生 __pycache__ 之类的东西。

    收尾用的是 `git add -A`，没有 .gitignore 的仓库会把这些垃圾一起提交。
    我们用仓库的 info/exclude 挡掉（未跟踪文件，不进用户历史）。

    线上就是这么把 `__pycache__/x.pyc` 提进 diff 的。
    """
    _, wm, repo = env

    async def main():
        ws = await wm.prepare(
            project_name="p", repo_path=str(repo), target_branch="main",
            task_id="t1", title="x",
        )
        _write(ws.path, "mod.py", "x = 1\n")
        (ws.path / "__pycache__").mkdir()
        (ws.path / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"\x00\x01")
        (ws.path / "node_modules").mkdir()
        (ws.path / "node_modules" / "dep.js").write_text("//")
        (ws.path / "keep.txt").write_text("这个要提交")

        result = await wm.finalize(ws, message="加 mod.py")
        assert result.had_changes

        diff = await wm.diff(ws)
        assert "mod.py" in diff, "真正的产物要提交"
        assert "keep.txt" in diff
        assert "__pycache__" not in diff, "字节码缓存不该进提交"
        assert "node_modules" not in diff

    run(main())


def test_exclude_block_is_written_once(env):
    _, wm, repo = env

    async def main():
        for i in range(3):
            await wm.prepare(
                project_name="p", repo_path=str(repo), target_branch="main",
                task_id=f"t{i}", title="x",
            )
        exclude = repo / ".git" / "info" / "exclude"
        text = exclude.read_text(encoding="utf-8")
        assert text.count("codingstorm: 自动生成") == 1, "不能每次 prepare 都追加一遍"

    run(main())


def test_exclude_is_not_a_tracked_file(env):
    """不能往用户仓库里塞一个 .gitignore —— 那是tracked 的，会污染他们的历史。"""
    _, wm, repo = env

    async def main():
        await wm.prepare(
            project_name="p", repo_path=str(repo), target_branch="main",
            task_id="t1", title="x",
        )
        assert not (repo / ".gitignore").exists()
        tracked = git(["ls-files"], repo)
        assert ".gitignore" not in tracked
        assert "exclude" not in tracked

    run(main())


# ---------- 批准 ----------

def test_approve_fast_forwards_target(env):
    _, wm, repo = env

    async def main():
        ws = await wm.prepare(
            project_name="p", repo_path=str(repo), target_branch="main",
            task_id="t1", title="x",
        )
        _write(ws.path, "feature.txt", "内容")
        result = await wm.finalize(ws, message="加 feature")

        before = git(["rev-parse", "main"], repo)
        sha = await wm.approve(ws, result.commit_sha)
        after = git(["rev-parse", "main"], repo)

        assert after != before
        assert after == sha
        # update-ref 只推进引用，不动主仓库的工作树 —— 所以查引用内容而不是文件系统
        assert git(["show", "main:feature.txt"], repo) == "内容"

    run(main())


def test_approve_is_idempotent(env):
    """上次批准成功但状态没写进库时，重复批准不能出错也不能改坏东西。"""
    _, wm, repo = env

    async def main():
        ws = await wm.prepare(
            project_name="p", repo_path=str(repo), target_branch="main",
            task_id="t1", title="x",
        )
        _write(ws.path, "f.txt", "x")
        result = await wm.finalize(ws, message="m")

        first = await wm.approve(ws, result.commit_sha)
        second = await wm.approve(ws, result.commit_sha)
        assert first == second
        assert git(["rev-parse", "main"], repo) == first

    run(main())


def test_approve_rebases_when_target_moved(env):
    """排在后面的任务先被批了，target 前进了 —— 批准时要先 rebase 再合。"""
    _, wm, repo = env

    async def main():
        ws = await wm.prepare(
            project_name="p", repo_path=str(repo), target_branch="main",
            task_id="t1", title="x",
        )
        _write(ws.path, "mine.txt", "a")
        result = await wm.finalize(ws, message="m")

        # target 被别的任务推进
        other = repo.parent / "other3"
        subprocess.run(["git", "clone", "-q", str(repo), str(other)], check=True)
        git(["config", "user.email", "o@l"], other)
        git(["config", "user.name", "o"], other)
        git(["checkout", "-q", "main"], other)
        _write(other, "theirs.txt", "b")
        git(["add", "-A"], other)
        git(["commit", "-q", "-m", "别处"], other)
        git(["push", "-q", "origin", "main"], other)

        sha = await wm.approve(ws, result.commit_sha)
        assert git(["show", "main:theirs.txt"], repo) == "b", "别处的改动要保留"
        assert git(["show", "main:mine.txt"], repo) == "a", "自己的改动也要在"
        assert git(["rev-parse", "main"], repo) == sha

    run(main())


def test_approve_raises_on_conflict(env):
    _, wm, repo = env

    async def main():
        ws = await wm.prepare(
            project_name="p", repo_path=str(repo), target_branch="main",
            task_id="t1", title="x",
        )
        _write(ws.path, "README.md", "任务版\n")

        other = repo.parent / "other4"
        subprocess.run(["git", "clone", "-q", str(repo), str(other)], check=True)
        git(["config", "user.email", "o@l"], other)
        git(["config", "user.name", "o"], other)
        git(["checkout", "-q", "main"], other)
        _write(other, "README.md", "别处版\n")
        git(["add", "-A"], other)
        git(["commit", "-q", "-m", "冲突"], other)
        git(["push", "-q", "origin", "main"], other)

        result = await wm.finalize(ws, message="m")
        assert result.rebase_conflict, "前置条件：应当已经冲突"
        with pytest.raises(RebaseConflict):
            await wm.approve(ws, result.commit_sha)
        # 冲突后不能留下半途状态
        assert not rebase_in_progress(ws.path)

    run(main())


# ---------- 僵尸状态清理 ----------

def test_abort_in_progress_clears_stale_index_lock(env):
    """被 kill 的 agent 会留下 index.lock，不清掉后续所有 git 操作都会被卡死。"""
    _, wm, repo = env

    async def main():
        ws = await wm.prepare(
            project_name="p", repo_path=str(repo), target_branch="main",
            task_id="t1", title="x",
        )
        g = Git(ws.path)
        git_dir = Path((await g.run("rev-parse", "--git-dir", cwd=ws.path)).strip())
        if not git_dir.is_absolute():
            git_dir = ws.path / git_dir
        lock = git_dir / "index.lock"
        lock.write_text("")
        (ws.path / "x.txt").write_text("x")

        # 有锁时写操作会被卡住（status 是只读的，不受影响）
        assert not await g.ok("add", "x.txt", cwd=ws.path)

        cleaned = await g.abort_in_progress(ws.path)
        assert any("index.lock" in c for c in cleaned)
        assert not lock.exists()
        assert await g.ok("add", "x.txt", cwd=ws.path), "清理后应当恢复正常"

    run(main())


def test_clean_keeps_gitignored_files(env):
    """`clean -fd` 不带 -x —— 不能把 node_modules / .venv 这种缓存删掉。"""
    _, wm, repo = env

    async def main():
        ws = await wm.prepare(
            project_name="p", repo_path=str(repo), target_branch="main",
            task_id="t1", title="x",
        )
        (ws.path / ".gitignore").write_text("cache/\n")
        (ws.path / "cache").mkdir()
        (ws.path / "cache" / "big.bin").write_text("缓存")
        (ws.path / "untracked.txt").write_text("临时文件")

        g = Git(ws.path)
        await g.reset_and_clean("--hard", ws.path)

        assert (ws.path / "cache" / "big.bin").exists(), "gitignore 的缓存应当保留"
        assert not (ws.path / "untracked.txt").exists(), "未跟踪的普通文件应当清掉"

    run(main())


# ---------- diff ----------

def test_diff_shows_task_changes(env):
    _, wm, repo = env

    async def main():
        ws = await wm.prepare(
            project_name="p", repo_path=str(repo), target_branch="main",
            task_id="t1", title="x",
        )
        _write(ws.path, "added.py", "print('new')\n")
        await wm.finalize(ws, message="m")

        d = await wm.diff(ws)
        assert "added.py" in d
        assert "+print('new')" in d

    run(main())


def test_diff_is_truncated_when_huge(env):
    _, wm, repo = env

    async def main():
        ws = await wm.prepare(
            project_name="p", repo_path=str(repo), target_branch="main",
            task_id="t1", title="x",
        )
        _write(ws.path, "big.txt", "x" * 200_000)
        await wm.finalize(ws, message="m")

        g = Git(ws.path)
        d = await g.diff(ws.base_commit, f"refs/heads/{ws.branch}", max_bytes=10_000)
        assert "已截断" in d

    run(main())
