"""HTTP 接口。"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request

from codingstorm.models import (
    AttemptOut,
    ProjectCreate,
    ProjectOut,
    TaskCreate,
    TaskOut,
    TaskStatus,
)
from codingstorm.store import Store

router = APIRouter(prefix="/api")


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
