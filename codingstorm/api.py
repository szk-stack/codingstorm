"""HTTP 接口。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from codingstorm.diff_render import render_diff
from codingstorm.models import (
    AttemptOut,
    ProjectCreate,
    ProjectOut,
    TaskCreate,
    TaskOut,
    TaskStatus,
)
from codingstorm.store import Store
from codingstorm.workspace import RebaseConflict, Workspace

router = APIRouter(prefix="/api")
pages = APIRouter()

STATIC_DIR = Path(__file__).parent / "static"

# 实时推送的合批间隔。逐条推会打爆浏览器，10fps 肉眼已经完全流畅。
WS_FLUSH_INTERVAL_S = 0.1
# 状态轮询间隔。事件可能因为队列满被丢，状态得有个兜底通道保证最终一致。
WS_STATUS_POLL_S = 2.0


def _store(request: Request) -> Store:
    return request.app.state.store


@router.get("/health")
async def health() -> dict:
    return {"ok": True}


# ---------- 项目 ----------


@router.get("/projects", response_model=list[ProjectOut])
async def list_projects(request: Request) -> list[ProjectOut]:
    return await _store(request).list_projects()


@router.post("/projects", response_model=ProjectOut, status_code=201)
async def create_project(request: Request, spec: ProjectCreate) -> ProjectOut:
    store = _store(request)
    if await store.get_project_by_name(spec.name):
        raise HTTPException(409, f"项目名已存在: {spec.name}")
    return await store.create_project(spec)


@router.get("/projects/{project_id}", response_model=ProjectOut)
async def get_project(request: Request, project_id: str) -> ProjectOut:
    project = await _store(request).get_project(project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    return project


@router.post("/projects/{project_id}/tasks", response_model=TaskOut, status_code=201)
async def create_task(request: Request, project_id: str, spec: TaskCreate) -> TaskOut:
    store = _store(request)
    if await store.get_project(project_id) is None:
        raise HTTPException(404, "项目不存在")
    return await store.create_task(project_id, spec)


# ---------- 任务 ----------


@router.get("/tasks", response_model=list[TaskOut])
async def list_tasks(
    request: Request, project_id: str | None = None, status: str | None = None
) -> list[TaskOut]:
    return await _store(request).list_tasks(project_id=project_id, status=status)


@router.get("/tasks/{task_id}", response_model=TaskOut)
async def get_task(request: Request, task_id: str) -> TaskOut:
    task = await _store(request).get_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return task


@router.get("/tasks/{task_id}/attempts", response_model=list[AttemptOut])
async def list_attempts(request: Request, task_id: str) -> list[AttemptOut]:
    store = _store(request)
    if await store.get_task(task_id) is None:
        raise HTTPException(404, "任务不存在")
    return await store.list_attempts(task_id)


@router.get("/tasks/{task_id}/events")
async def list_events(request: Request, task_id: str, after_seq: int = -1, limit: int = 500) -> list[dict]:
    store = _store(request)
    if await store.get_task(task_id) is None:
        raise HTTPException(404, "任务不存在")
    rows = await store.list_events(task_id, after_seq=after_seq, limit=limit)
    return [
        {
            "seq": r["seq"],
            "ts": r["ts"],
            "type": r["type"],
            "payload": json.loads(r["payload"]) if r["payload"] else None,
        }
        for r in rows
    ]


@router.post("/tasks/{task_id}/requeue", response_model=TaskOut)
async def requeue_task(request: Request, task_id: str) -> TaskOut:
    """重新入队。**会重建工作区** —— 旧工作区带着上次未提交的改动且基于旧基点。"""
    store = _store(request)
    task = await store.get_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task.status not in (TaskStatus.FAILED, TaskStatus.INTERRUPTED, TaskStatus.CANCELLED):
        raise HTTPException(409, f"任务状态为 {task.status}，不能重新入队")
    await store.requeue(task_id)
    refreshed = await store.get_task(task_id)
    assert refreshed is not None
    return refreshed


@router.post("/tasks/{task_id}/cancel", response_model=TaskOut)
async def cancel_task(request: Request, task_id: str) -> TaskOut:
    store = _store(request)
    task = await store.get_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task.status != TaskStatus.QUEUED:
        raise HTTPException(409, "只能取消还在排队的任务")

    runner = request.app.state.runner
    await store.set_status(task_id, TaskStatus.CANCELLED)
    await runner.kill(task_id)
    refreshed = await store.get_task(task_id)
    assert refreshed is not None
    return refreshed


# ---------- 审阅 ----------


async def _workspace_for(request: Request, task_id: str):
    """从任务记录还原出 Workspace（审批发生在任务跑完之后，内存里已经没有它了）。"""
    store = _store(request)
    raw = await store.get_task_raw(task_id)
    if raw is None:
        raise HTTPException(404, "任务不存在")
    if not raw["worktree_path"] or not raw["branch"]:
        raise HTTPException(409, "该任务还没有工作区（可能还没执行过）")

    project = await store.get_project(raw["project_id"])
    if project is None:
        raise HTTPException(404, "项目不存在")

    workspace = Workspace(
        path=Path(raw["worktree_path"]),
        repo_path=Path(project.repo_path),
        branch=raw["branch"],
        base_commit=raw["base_commit"] or "",
        target_branch=project.target_branch,
    )
    return store, raw, workspace


@router.get("/tasks/{task_id}/diff")
async def get_diff(request: Request, task_id: str) -> dict:
    """审的这份 diff 就是将来合入的内容（分支在收尾时已经 rebase 到最新 target）。"""
    _, raw, workspace = await _workspace_for(request, task_id)
    wm = request.app.state.workspaces
    try:
        text = await wm.diff(workspace)
        return {
            "diff": text,
            # 服务端渲染好，前端直接塞进 DOM —— 不依赖任何 CDN（执行机在国内）
            "html": render_diff(text),
            "stat": await wm.diff_stat(workspace),
            "base": workspace.target_branch,
            "branch": workspace.branch,
            "commit_sha": raw["commit_sha"],
        }
    except Exception as exc:
        raise HTTPException(500, f"生成 diff 失败: {exc}") from exc


@router.post("/tasks/{task_id}/approve", response_model=TaskOut)
async def approve_task(request: Request, task_id: str) -> TaskOut:
    """批准即合并。走 update-ref 快进，幂等。"""
    store, _raw, workspace = await _workspace_for(request, task_id)
    task = await store.get_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task.status != TaskStatus.AWAITING_REVIEW:
        raise HTTPException(409, f"任务状态为 {task.status}，只有待审的任务能批准")

    wm = request.app.state.workspaces
    raw = await store.get_task_raw(task_id)
    try:
        merged_sha = await wm.approve(workspace, raw["commit_sha"])
    except RebaseConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f"合并失败: {exc}") from exc

    await store.set_status(
        task_id, TaskStatus.MERGED, merge_commit_sha=merged_sha, error_text=None
    )
    # 提交已经在 target 可达了，worktree 和分支都可以收掉
    await wm.cleanup(workspace, delete_branch=True)

    refreshed = await store.get_task(task_id)
    assert refreshed is not None
    return refreshed


@router.post("/tasks/{task_id}/discard", response_model=TaskOut)
async def discard_task(request: Request, task_id: str) -> TaskOut:
    """丢弃：删掉 worktree 和分支，target 不受影响。"""
    store, _raw, workspace = await _workspace_for(request, task_id)
    task = await store.get_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task.status not in (TaskStatus.AWAITING_REVIEW, TaskStatus.FAILED, TaskStatus.INTERRUPTED):
        raise HTTPException(409, f"任务状态为 {task.status}，不能丢弃")

    wm = request.app.state.workspaces
    await wm.cleanup(workspace, delete_branch=True)
    await store.set_status(task_id, TaskStatus.DISCARDED)

    refreshed = await store.get_task(task_id)
    assert refreshed is not None
    return refreshed


# ---------- 页面 ----------


@pages.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# ---------- 实时推送 ----------


async def _drain_client(websocket: WebSocket) -> None:
    """前端不发消息，但必须有人在收 —— 否则客户端断开时我们察觉不到。"""
    try:
        while True:
            await websocket.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        return


@router.websocket("/ws/tasks/{task_id}")
async def ws_task(websocket: WebSocket, task_id: str, after_seq: int = -1) -> None:
    await websocket.accept()
    app = websocket.app
    store: Store = app.state.store
    bus = app.state.bus
    loop = asyncio.get_running_loop()

    task = await store.get_task(task_id)
    if task is None:
        await websocket.send_json({"kind": "error", "message": "任务不存在"})
        await websocket.close()
        return

    # 先订阅再读历史 —— 反过来的话，两者之间产生的事件会永久丢失。
    # 重复的由客户端按 seq 去重。
    queue = bus.subscribe(task_id)

    try:
        await websocket.send_json({"kind": "snapshot", "task": task.model_dump()})

        # 补历史事件，这样刷新页面不会丢上下文
        for row in await store.list_events(task_id, after_seq=after_seq, limit=5000):
            await websocket.send_json({
                "kind": "event",
                "seq": row["seq"],
                "ts": row["ts"],
                "type": row["type"],
                "payload": json.loads(row["payload"]) if row["payload"] else None,
            })
    except (WebSocketDisconnect, RuntimeError):
        bus.unsubscribe(task_id, queue)
        return

    receiver = asyncio.create_task(_drain_client(websocket))
    last_status = task.status
    pending: list[dict] = []
    last_flush = loop.time()
    last_poll = loop.time()

    try:
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=WS_FLUSH_INTERVAL_S)
                pending.append(msg)
            except TimeoutError:
                pass

            now = loop.time()
            if pending and now - last_flush >= WS_FLUSH_INTERVAL_S:
                await websocket.send_json({"kind": "events", "events": pending})
                pending.clear()
                last_flush = now

            # 事件可能因队列满被丢，状态得有条兜底通道保证最终一致
            if now - last_poll >= WS_STATUS_POLL_S:
                last_poll = now
                fresh = await store.get_task(task_id)
                if fresh is not None and fresh.status != last_status:
                    last_status = fresh.status
                    await websocket.send_json({
                        "kind": "status",
                        "status": fresh.status,
                        "task": fresh.model_dump(),
                    })
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        receiver.cancel()
        bus.unsubscribe(task_id, queue)
