"""调度器。

核心规则：**按项目串行、跨项目并行**。

同一项目目录下并发跑两个 Claude Code 会互相踩文件，这是硬约束；而项目之间互不相干，
可以同时跑 —— 这正是它相对「全局串行队列」的价值所在。

全局并发上限由执行机内存决定（每个 Claude Code 进程约 200-400MB）。
"""

from __future__ import annotations

import asyncio
import logging
import signal
import uuid
from typing import Any

from codingstorm.config import Config
from codingstorm.context import build_prompt
from codingstorm.models import TaskStatus
from codingstorm.runner import (
    RunOutcome,
    Runner,
    kill_process_group,
    pid_is_alive,
)
from codingstorm.store import Store
from codingstorm.workspace import WorkspaceManager

log = logging.getLogger("codingstorm.scheduler")


class Scheduler:
    def __init__(
        self,
        config: Config,
        store: Store,
        runner: Runner,
        workspaces: WorkspaceManager,
    ):
        self.config = config
        self.store = store
        self.runner = runner
        self.workspaces = workspaces
        self._loop_task: asyncio.Task | None = None
        self._running: dict[str, asyncio.Task] = {}
        self._stopping = asyncio.Event()

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        await self.recover()
        self._stopping.clear()
        self._loop_task = asyncio.create_task(self._loop(), name="cs-scheduler")

    async def stop(self, *, grace_s: float = 15.0) -> None:
        """优雅退出。

        顺序很重要：**先写库再发信号**。否则重启后无法区分「优雅退出」和「崩溃」。
        """
        self._stopping.set()
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):
                pass

        for task_id in list(self._running):
            await self.store.set_status(
                task_id, TaskStatus.INTERRUPTED, error_text="平台正在退出，任务被中断"
            )
            await self.runner.kill(task_id)

        if self._running:
            await asyncio.wait(list(self._running.values()), timeout=grace_s)

    @property
    def running(self) -> dict[str, asyncio.Task]:
        return dict(self._running)

    # ---------- 崩溃恢复 ----------

    async def recover(self) -> list[str]:
        """启动时的对账。

        **不能简单地把 running 改成 failed。** 平台被 SIGKILL / OOM 后子进程还活着，
        会继续改文件、继续烧钱 —— 必须先杀掉，再改状态。
        """
        recovered: list[str] = []
        for row in await self.store.reconcile_orphans():
            task_id = row["id"]
            pid = row["pid"]
            starttime = row["pid_starttime"]
            if pid and pid_is_alive(pid, starttime):
                log.warning("回收孤儿进程 pid=%s（任务 %s）", pid, task_id)
                kill_process_group(pid, signal.SIGTERM)
                for _ in range(20):  # 最多等 2 秒
                    await asyncio.sleep(0.1)
                    if not pid_is_alive(pid, starttime):
                        break
                else:
                    log.warning("pid=%s 未响应 SIGTERM，升级为 SIGKILL", pid)
                    kill_process_group(pid, signal.SIGKILL)
            await self.store.set_status(
                task_id, TaskStatus.INTERRUPTED, error_text="平台重启，任务被中断"
            )
            recovered.append(task_id)
        if recovered:
            log.info("对账了 %d 条中断任务: %s", len(recovered), ", ".join(recovered))
        return recovered

    # ---------- 主循环 ----------

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("调度循环出错")
            await asyncio.sleep(self.config.scheduler.poll_interval_s)

    async def _tick(self) -> None:
        if len(self._running) >= self.config.scheduler.max_concurrent:
            return
        row = await self.store.claim_next()
        if row is None:
            return
        task_id = row["id"]
        task = asyncio.create_task(self._run(dict(row)), name=f"cs-task-{task_id}")
        self._running[task_id] = task
        task.add_done_callback(lambda _t, tid=task_id: self._running.pop(tid, None))

    # ---------- 执行单条任务 ----------

    async def _run(self, row: dict[str, Any]) -> None:
        task_id = row["id"]
        project = await self.store.get_project(row["project_id"])
        if project is None:
            await self.store.set_status(task_id, TaskStatus.FAILED, error_text="项目不存在")
            return

        task = await self.store.get_task(task_id)
        if task is None:
            return

        attempt_no = await self.store.next_attempt_no(task_id)
        await self.store.start_attempt(task_id, attempt_no)
        outcome = RunOutcome()
        workspace = None

        try:
            workspace = await self.workspaces.prepare(
                project_name=project.name,
                repo_path=project.repo_path,
                target_branch=project.target_branch,
                task_id=task_id,
                title=task.title,
            )
            # 只更新字段，不重写 status —— claim_next 已经置为 running 了，
            # 在这里再写一次会把并发的 interrupted 覆盖回 running。
            await self.store.update_fields(
                task_id,
                branch=workspace.branch,
                worktree_path=str(workspace.path),
                base_commit=workspace.base_commit,
            )
            fresh = await self.store.get_task(task_id)
            if fresh is None or fresh.status != TaskStatus.RUNNING:
                log.info("任务 %s 在准备阶段被中断，放弃执行", task_id)
                return
            outcome = await self.runner.run(
                task_id,
                project.name,
                prompt=build_prompt(task),
                session_id=str(uuid.uuid4()),
                cwd=workspace.path,
                attempt_no=attempt_no,
            )
        except asyncio.CancelledError:
            await self.store.set_status(task_id, TaskStatus.INTERRUPTED, error_text="任务被取消")
            raise
        except Exception as exc:
            log.exception("任务 %s 执行异常", task_id)
            await self.store.finish_attempt(
                task_id, attempt_no, is_error=True, error_text=str(exc)
            )
            await self.store.set_status(task_id, TaskStatus.FAILED, error_text=str(exc))
            return

        await self.store.finish_attempt(
            task_id,
            attempt_no,
            exit_code=outcome.exit_code,
            result_subtype=outcome.result_subtype,
            is_error=outcome.is_error,
            error_text=outcome.error_text,
            duration_ms=outcome.duration_ms,
            num_turns=outcome.num_turns,
            input_tokens=outcome.input_tokens,
            output_tokens=outcome.output_tokens,
            cache_creation_tokens=outcome.cache_creation_tokens,
            cache_read_tokens=outcome.cache_read_tokens,
            session_id=outcome.session_id,
            model=outcome.model,
        )

        if not outcome.ok:
            reason = outcome.error_text or f"执行失败（subtype={outcome.result_subtype}）"
            await self.store.set_status(task_id, TaskStatus.FAILED, error_text=reason)
            log.warning("任务 %s 失败: %s", task_id, reason)
            return

        await self._finalize_success(task_id, task, workspace)

    async def _finalize_success(self, task_id: str, task, workspace) -> None:
        """提交改动、rebase 到最新 target、置为待审。

        失败**不删工作区** —— 现场要留着排查。
        """
        try:
            result = await self.workspaces.finalize(
                workspace, message=f"{task.title}\n\ncodingstorm-task: {task_id}"
            )
        except Exception as exc:
            log.exception("任务 %s 收尾失败", task_id)
            await self.store.set_status(task_id, TaskStatus.FAILED, error_text=f"收尾失败: {exc}")
            return

        if result.rebase_conflict:
            log.warning("任务 %s 的分支与 %s 有冲突", task_id, workspace.target_branch)
            await self.store.set_status(
                task_id,
                TaskStatus.AWAITING_REVIEW,
                commit_sha=result.commit_sha,
                error_text=f"分支与 {workspace.target_branch} 有 rebase 冲突，合入前需人工处理",
            )
            return

        await self.store.set_status(
            task_id, TaskStatus.AWAITING_REVIEW, commit_sha=result.commit_sha
        )
        log.info(
            "任务 %s 完成，等待审查（%s，%s）",
            task_id,
            workspace.branch,
            "有改动" if result.had_changes else "无改动",
        )
