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
from datetime import UTC, datetime, timedelta
from typing import Any

from codingstorm.config import Config
from codingstorm.context import ContextStore
from codingstorm.models import TaskCreate, TaskKind, TaskStatus, WindowOut
from codingstorm.pricing import PriceTable, Usage
from codingstorm.runner import (
    RunOutcome,
    Runner,
    kill_process_group,
    pid_is_alive,
)
from codingstorm.sediment import sediment_task
from codingstorm.store import Store
from codingstorm.timing import (
    TimingError,
    Window,
    is_open,
    is_open_for,
    iso_utc,
    load_zone,
    next_close,
    next_occurrence,
    next_open,
    parse_rule,
    parse_utc,
    parse_windows,
)
from codingstorm.workspace import WorkspaceManager

log = logging.getLogger("codingstorm.scheduler")


def _human_delta(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    if total < 60:
        return f"{total} 秒"
    if total < 3600:
        return f"{total // 60} 分钟"
    hours, minutes = divmod(total // 60, 60)
    if hours < 24:
        return f"{hours} 小时 {minutes} 分"
    days, hours = divmod(hours, 24)
    return f"{days} 天 {hours} 小时"


class Scheduler:
    def __init__(
        self,
        config: Config,
        store: Store,
        runner: Runner,
        workspaces: WorkspaceManager,
        contexts: ContextStore | None = None,
        prices: PriceTable | None = None,
    ):
        self.config = config
        self.store = store
        self.runner = runner
        self.workspaces = workspaces
        self.contexts = contexts or ContextStore(config)
        self.prices = prices or PriceTable.empty()
        self._loop_task: asyncio.Task | None = None
        self._running: dict[str, asyncio.Task] = {}
        self._stopping = asyncio.Event()

        # 窗口配置在启动时解析一次。时区写错就让平台起不来 —— 悄悄退回 UTC 的话，
        # 「凌晨 00:30 到 08:30」会变成北京时间早上 8:30 到下午 4:30，正好跑在最贵的时候。
        self._tz = load_zone(config.scheduler.window.timezone)
        self._windows: list[Window] = (
            parse_windows(config.scheduler.window.windows)
            if config.scheduler.window.enabled
            else []
        )
        # 临时关闭窗口。**内存态**：重启即恢复，忘记关掉不会一直烧钱。
        self._window_disabled = False

    # ---------- 执行窗口 ----------

    @property
    def timezone(self):
        """定时规则和窗口共用的时区。用户说的「9 点」指的是这个时区的 9 点。"""
        return self._tz

    def window_state(self, now: datetime | None = None) -> WindowOut:
        moment = now or datetime.now(self._tz)
        open_now = self._window_disabled or is_open(moment, self._windows)
        opening = None if open_now else next_open(moment, self._windows)
        closing = next_close(moment, self._windows) if open_now else None
        return WindowOut(
            enabled=self.config.scheduler.window.enabled,
            disabled=self._window_disabled,
            open=open_now,
            timezone=self.config.scheduler.window.timezone,
            windows=[[w.start.strftime("%H:%M"), w.end.strftime("%H:%M")] for w in self._windows],
            now=iso_utc(moment),
            next_open_at=iso_utc(opening) if opening else None,
            next_close_at=iso_utc(closing) if closing else None,
        )

    def set_window_disabled(self, disabled: bool) -> WindowOut:
        """临时关掉/恢复窗口限制。急事用 —— 平时靠它省谷时的钱。"""
        self._window_disabled = disabled
        log.warning("执行窗口被临时%s", "关闭（所有任务立即放行）" if disabled else "恢复")
        return self.window_state()

    async def _allowed_projects(self, now: datetime) -> list[str] | None:
        """此刻允许启动任务的项目；None 表示不限制。

        窗口开着时直接返回 None —— 省掉每秒钟一次的项目查询。
        """
        if self._window_disabled or is_open(now, self._windows):
            return None
        projects = await self.store.list_projects()
        return [
            p.id
            for p in projects
            if is_open_for(now, self._windows, p.window_override)
        ]

    # ---------- 定时任务 ----------

    async def _fire_due_schedules(self, now: datetime) -> None:
        """把到点的定时任务变成真正的任务。

        宽限期内的迟到照跑（轮询是一秒一次，只有平台重启才会迟到）；
        超过宽限期就算错过 —— 一次性任务停下并标记，周期任务直接跳到下一次。
        **一律不补跑**，否则停机三天开机时会一口气冒出三条任务。
        """
        now_iso = iso_utc(now)
        grace = timedelta(seconds=self.config.scheduler.misfire_grace_s)
        for schedule in await self.store.due_schedules(now_iso):
            try:
                await self._fire_one(schedule, now, grace)
            except Exception:
                # 一条定时任务出错不该拖垮整个调度循环
                log.exception("定时任务 %s 触发失败", schedule.id)

    async def _fire_one(self, schedule, now: datetime, grace: timedelta) -> None:
        assert schedule.next_run_at is not None
        try:
            rule = parse_rule(schedule.rule)
        except TimingError as exc:
            log.error("定时任务 %s 的规则坏了，已停用：%s", schedule.id, exc)
            await self.store.update_schedule(schedule.id, enabled=0, next_run_at=None)
            return

        lateness = now - parse_utc(schedule.next_run_at)
        upcoming = next_occurrence(rule, now, self._tz)

        if lateness > grace and rule.type == "once":
            marked = await self.store.expire_schedule(
                schedule, expected_next_run_at=schedule.next_run_at, now_iso=iso_utc(now)
            )
            if marked:
                log.warning(
                    "定时任务 %s 错过 %s 共 %s，未执行（已在界面上标出）",
                    schedule.id,
                    schedule.next_run_at,
                    _human_delta(lateness),
                )
            return

        if lateness > grace:
            advanced = await self.store.advance_schedule(
                schedule.id,
                expected_next_run_at=schedule.next_run_at,
                next_run_at=iso_utc(upcoming) if upcoming else iso_utc(now),
            )
            if advanced:
                log.warning(
                    "定时任务 %s 错过 %s 共 %s，跳过这一轮，下一次 %s",
                    schedule.id,
                    schedule.next_run_at,
                    _human_delta(lateness),
                    upcoming,
                )
            return

        created = await self.store.fire_schedule(
            schedule,
            TaskCreate(
                title=schedule.title,
                body=schedule.body,
                kind=TaskKind(schedule.kind),
                priority=schedule.priority,
            ),
            expected_next_run_at=schedule.next_run_at,
            next_run_at=iso_utc(upcoming) if upcoming else None,
            now_iso=iso_utc(now),
        )
        if created is not None:
            log.info(
                "定时任务 %s 入队任务 %s（下一次 %s）",
                schedule.id,
                created.id,
                upcoming.astimezone(self._tz).strftime("%m-%d %H:%M") if upcoming else "不再触发",
            )

    def _cost_fields(
        self,
        model: str | None,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_creation_tokens: int,
    ) -> dict[str, Any]:
        """按 token 算成本。价目表里没有这个模型就留空 —— 不瞎猜。

        不能用 `total_cost_usd`：那是按另一端的价目算的，跟实际付费对不上。
        连 `price_version` 一起落库，是为了**改价目表不能改写历史成本**。
        """
        usage = Usage(input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens)
        cost = self.prices.cost(model, usage)
        if cost is None:
            return {}
        return {"cost_usd": cost, "price_version": self.prices.version}

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
        now = datetime.now(self._tz)
        # 先触发定时任务，这样刚入队的任务在同一次 tick 里就能被领走，少一秒延迟
        await self._fire_due_schedules(now)

        if len(self._running) >= self.config.scheduler.max_concurrent:
            return
        # 窗口关着就一条都不领 —— 任务留在队列里等窗口开，界面上标「等待窗口」
        allowed = await self._allowed_projects(now)
        if allowed is not None and not allowed:
            return
        row = await self.store.claim_next(allowed_projects=allowed)
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
            messages = await self.store.list_messages(task_id)
            last_session = await self.store.last_session_id(task_id) if messages else None

            # 追加过消息、且上一轮留下了会话和工作区 → 接着聊。
            # 少任何一样就是从头跑：比如第一轮在切工作区时就失败了（仓库还是空的），
            # branch 为空，这次带上追加说明重开。
            resume = bool(messages and last_session and task.branch)
            if resume:
                workspace = await self.workspaces.prepare_resume(
                    project_name=project.name,
                    repo_path=project.repo_path,
                    target_branch=project.target_branch,
                    task_id=task_id,
                    branch=task.branch,
                    base_commit=task.base_commit or "",
                )
                # 只发新这一句 —— 上下文都在会话里，重复注入一遍是浪费
                prompt = messages[-1].text
                session_id = last_session
            else:
                workspace = await self.workspaces.prepare(
                    project_name=project.name,
                    repo_path=project.repo_path,
                    target_branch=project.target_branch,
                    task_id=task_id,
                    title=task.title,
                )
                prompt = self.contexts.build_prompt(
                    project.name,
                    task,
                    max_journal_chars=self.config.context.journal_prompt_chars,
                )
                if messages:
                    prompt += f"\n\n---\n\n## 追加说明\n\n{messages[-1].text}"
                session_id = str(uuid.uuid4())

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
            ctx = self.contexts.ensure(project.name)
            outcome = await self.runner.run(
                task_id,
                project.name,
                prompt=prompt,
                session_id=session_id,
                cwd=workspace.path,
                attempt_no=attempt_no,
                # 上下文目录在 worktree 之外，靠 --add-dir 授权访问 ——
                # 这样完全不用碰被调度仓库的 .gitignore，也不污染它的历史
                context_dir=ctx.root,
                resume=resume,
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
            **self._cost_fields(
                outcome.model,
                input_tokens=outcome.input_tokens,
                output_tokens=outcome.output_tokens,
                cache_read_tokens=outcome.cache_read_tokens,
                cache_creation_tokens=outcome.cache_creation_tokens,
            ),
        )

        if not outcome.ok:
            reason = outcome.error_text or f"执行失败（subtype={outcome.result_subtype}）"
            await self.store.set_status(task_id, TaskStatus.FAILED, error_text=reason)
            log.warning("任务 %s 失败: %s", task_id, reason)
            return

        await self._finalize_success(task_id, task, project.name, workspace)

    async def _finalize_success(self, task_id: str, task, project_name: str, workspace) -> None:
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
        if result.had_changes:
            await self._sediment(task, project_name, workspace)

    async def _sediment(self, task, project_name: str, workspace) -> None:
        """任务结束后自动写变更记录。

        **必须自动** —— 需要人记得去维护的文档一定会腐烂（Cline 的 Memory Bank 就是这么死的）。
        这是每个任务的一笔固定开销，单独记一条 attempt（origin=sediment），
        否则这部分成本会凭空消失。
        """
        if not self.config.context.sediment:
            return
        try:
            diff = await self.workspaces.diff(workspace)
            stat = await self.workspaces.diff_stat(workspace)
        except Exception:
            log.exception("取 diff 失败，跳过沉淀")
            return

        attempt_no = await self.store.next_attempt_no(task.id)
        await self.store.start_attempt(task.id, attempt_no, origin="sediment")
        session_id = str(uuid.uuid4())
        try:
            result = await sediment_task(
                self.config,
                self.contexts,
                project_name,
                task,
                workspace.path,
                diff=diff,
                stat=stat,
                session_id=session_id,
            )
        except Exception as exc:
            log.exception("沉淀异常")
            await self.store.finish_attempt(
                task.id, attempt_no, is_error=True, error_text=str(exc)
            )
            return

        await self.store.finish_attempt(
            task.id,
            attempt_no,
            session_id=session_id,
            model=result.model,
            result_subtype="skipped" if result.skipped else ("error" if result.error else "success"),
            is_error=bool(result.error),
            error_text=result.error,
            duration_ms=result.duration_ms,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_read_tokens=result.cache_read_tokens,
            cache_creation_tokens=result.cache_creation_tokens,
            **self._cost_fields(
                result.model,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cache_read_tokens=result.cache_read_tokens,
                cache_creation_tokens=result.cache_creation_tokens,
            ),
        )
