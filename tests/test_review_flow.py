"""审阅流程测试：工作区 → 提交 → diff → 批准 / 丢弃。

走服务层而不是 HTTP —— 审批依赖「任务已经跑完并留下工作区」这个前置状态，
用 HTTP 造不出来。
"""

import asyncio
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from codingstorm.config import Config
from codingstorm.db import Database
from codingstorm.models import ProjectCreate, TaskCreate, TaskStatus
from codingstorm.store import Store
from codingstorm.workspace import RebaseConflict, WorkspaceManager


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(tmp_path: Path, make_repo):
    cfg = Config(root=tmp_path / "root")
    cfg.ensure_dirs()
    repo = make_repo()

    db = Database(tmp_path / "test.db")
    db.start()
    store = Store(db)
    wm = WorkspaceManager(cfg)
    try:
        yield store, wm, repo
    finally:
        db.close()


def git(args: list[str], cwd: Path) -> str:
    p = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8"
    )
    assert p.returncode == 0, f"git {' '.join(args)}: {p.stderr}"
    return (p.stdout or "").strip()


async def _make_task(store: Store, wm: WorkspaceManager, repo: Path, title: str = "加个文件"):
    project = await store.create_project(
        ProjectCreate(name="demo", repo_path=str(repo), target_branch="main")
    )
    task = await store.create_task(project.id, TaskCreate(title=title))
    ws = await wm.prepare(
        project_name=project.name,
        repo_path=project.repo_path,
        target_branch=project.target_branch,
        task_id=task.id,
        title=task.title,
    )
    await store.update_fields(
        task.id,
        branch=ws.branch,
        worktree_path=str(ws.path),
        base_commit=ws.base_commit,
    )
    return project, task, ws


def _advance_main(repo: Path, name: str, content: str) -> None:
    """从别处往 main 推进一个提交，用来模拟「任务跑的时候 target 前进了」。

    走 clone + push，因为目标仓库的当前分支不能直接 push。
    测试仓库已设 receive.denyCurrentBranch=ignore。
    """
    other = repo.parent / f"other-{name}"
    subprocess.run(["git", "clone", "-q", str(repo), str(other)], check=True)
    git(["config", "user.email", "o@localhost"], other)
    git(["config", "user.name", "o"], other)
    git(["checkout", "-q", "main"], other)
    (other / name).write_text(content, encoding="utf-8")
    git(["add", "-A"], other)
    git(["commit", "-q", "-m", f"别处改 {name}"], other)
    git(["push", "-q", "origin", "main"], other)


def test_full_review_flow(env):
    """准备 → 干活 → 提交 → 看 diff → 批准 → target 上出现改动。"""
    store, wm, repo = env

    async def main():
        project, task, ws = await _make_task(store, wm, repo)

        # AI 干活
        (ws.path / "feature.py").write_text("print('hello')\n", encoding="utf-8")

        result = await wm.finalize(ws, message="加 feature")

        # 审阅：diff 里能看到改动
        diff = await wm.diff(ws)
        assert "feature.py" in diff
        assert "+print('hello')" in diff

        # 批准
        sha = await wm.approve(ws, result.commit_sha)
        await store.set_status(
            task.id, TaskStatus.AWAITING_REVIEW, commit_sha=result.commit_sha
        )
        await store.set_status(task.id, TaskStatus.MERGED, merge_commit_sha=sha)
        await wm.cleanup(ws, delete_branch=True)

        # target 上确实有了
        assert git(["show", "main:feature.py"], repo) == "print('hello')"
        assert not ws.path.exists()
        merged = await store.get_task(task.id)
        assert merged.status == TaskStatus.MERGED

    run(main())


def test_discard_leaves_target_untouched(env):
    store, wm, repo = env

    async def main():
        project, task, ws = await _make_task(store, wm, repo, title="会被丢弃的任务")
        before = git(["rev-parse", "main"], repo)

        (ws.path / "junk.txt").write_text("不要的\n", encoding="utf-8")
        await wm.finalize(ws, message="加 junk")

        await wm.cleanup(ws, delete_branch=True)
        await store.set_status(task.id, TaskStatus.DISCARDED)

        assert git(["rev-parse", "main"], repo) == before, "target 不该被动过"
        assert not ws.path.exists()
        # 分支也删掉了
        branches = git(["branch", "--list", "cs/*"], repo)
        assert task.id not in branches

    run(main())


def test_two_tasks_second_rebases_after_first_approved(env):
    """串行执行的关键场景：任务 2 看不到任务 1 未合并的改动，批准任务 1 后任务 2 能正确 rebase。"""
    store, wm, repo = env

    async def main():
        project = await store.create_project(
            ProjectCreate(name="demo", repo_path=str(repo), target_branch="main")
        )

        # 任务 1：新建 a.txt
        t1 = await store.create_task(project.id, TaskCreate(title="任务一"))
        ws1 = await wm.prepare(
            project_name=project.name, repo_path=project.repo_path,
            target_branch=project.target_branch, task_id=t1.id, title=t1.title,
        )
        (ws1.path / "a.txt").write_text("来自任务一\n", encoding="utf-8")
        r1 = await wm.finalize(ws1, message="任务一")

        # 任务 2 在任务 1 还没合并时开工 —— 它看不到 a.txt
        t2 = await store.create_task(project.id, TaskCreate(title="任务二"))
        ws2 = await wm.prepare(
            project_name=project.name, repo_path=project.repo_path,
            target_branch=project.target_branch, task_id=t2.id, title=t2.title,
        )
        assert not (ws2.path / "a.txt").exists(), "任务 2 不该看到任务 1 未合并的改动"
        (ws2.path / "b.txt").write_text("来自任务二\n", encoding="utf-8")
        r2 = await wm.finalize(ws2, message="任务二")

        # 批准任务 1
        await wm.approve(ws1, r1.commit_sha)
        await wm.cleanup(ws1, delete_branch=True)

        # 任务 2 此时落后于 target，批准时要先 rebase
        merged2 = await wm.approve(ws2, r2.commit_sha)

        # git() 会 strip 输出，所以文件内容的结尾换行比不到 —— 这里只比正文
        assert git(["show", f"{merged2}:a.txt"], repo) == "来自任务一"
        assert git(["show", f"{merged2}:b.txt"], repo) == "来自任务二"

    run(main())


def test_approve_conflict_raises_and_leaves_no_half_state(env):
    store, wm, repo = env

    async def main():
        _project, _task, ws = await _make_task(store, wm, repo, title="会冲突的任务")
        (ws.path / "README.md").write_text("任务改的\n", encoding="utf-8")

        # 任务跑的时候别处也改了同一个文件 → 收尾 rebase 必然冲突
        _advance_main(repo, "README.md", "别处改的\n")

        r = await wm.finalize(ws, message="改 README")
        assert r.rebase_conflict, "前置条件：应当已经冲突"

        with pytest.raises(RebaseConflict):
            await wm.approve(ws, r.commit_sha)

        # 冲突后不能留半途状态，否则后续 git 操作全废
        raw = git(["rev-parse", "--git-dir"], ws.path)
        git_dir = Path(raw)
        if not git_dir.is_absolute():
            git_dir = ws.path / git_dir
        assert not (git_dir / "rebase-merge").exists()
        # 分支内容保留，供人工处理
        assert git(["log", "-1", "--format=%s"], ws.path) == "改 README"

    run(main())


def test_diff_still_readable_after_merge(env):
    """批准会删掉分支，但用户仍要能回看这次改了什么。

    回归：之前 diff 直接按 `refs/heads/<分支>` 取，分支没了就报
    `ambiguous argument`，**合完再也看不到改动**。
    """
    store, wm, repo = env

    async def main():
        _project, _task, ws = await _make_task(store, wm, repo)
        (ws.path / "kept.py").write_text("x = 1\n", encoding="utf-8")
        result = await wm.finalize(ws, message="加 kept.py")
        await wm.approve(ws, result.commit_sha)
        await wm.cleanup(ws, delete_branch=True)   # 分支没了

        merged = replace(ws, commit_sha=result.commit_sha)
        diff = await wm.diff(merged)
        assert "kept.py" in diff, "合并后仍应能看到改动"

    run(main())


def test_diff_falls_back_when_branch_missing_without_commit(env):
    """连 commit_sha 都没有时不该崩，只是取不到内容。"""
    store, wm, repo = env

    async def main():
        _project, _task, ws = await _make_task(store, wm, repo)
        gone = replace(ws, branch="cs/does-not-exist", commit_sha=None)
        with pytest.raises(Exception):
            await wm.diff(gone)

    run(main())


def test_failed_task_keeps_worktree_for_inspection(env):
    """失败时保留工作区 —— 现场要留着排查。"""
    store, wm, repo = env

    async def main():
        _project, task, ws = await _make_task(store, wm, repo)
        (ws.path / "half_done.py").write_text("没写完", encoding="utf-8")
        # 不 finalize、不 cleanup，直接标失败（模拟 runner 失败路径）
        await store.set_status(task.id, TaskStatus.FAILED, error_text="模拟失败")

        assert ws.path.exists(), "失败时工作区应当保留"
        assert (ws.path / "half_done.py").exists()
        failed = await store.get_task(task.id)
        assert failed.status == TaskStatus.FAILED

    run(main())
