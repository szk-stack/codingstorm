"""HTTP 接口。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from codingstorm.context import POINTER_SOFT_LIMIT_BYTES
from codingstorm.diff_render import render_diff
from codingstorm.git_ops import Git, GitError
from codingstorm.models import (
    AttemptOut,
    ContextOut,
    DocOut,
    FileEntry,
    FileOut,
    ModelUsage,
    ProjectCreate,
    ProjectOut,
    TaskCreate,
    TaskMessageIn,
    TaskMessageOut,
    TaskOut,
    TaskStatus,
    TextPayload,
    TreeOut,
    UsageOut,
)
from codingstorm.store import Store
from codingstorm.workspace import RebaseConflict, RepoError, Workspace, prepare_repo

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
    try:
        spec.repo_path = str(
            await prepare_repo(
                request.app.state.config,
                name=spec.name,
                repo_path=spec.repo_path,
                target_branch=spec.target_branch,
            )
        )
    except RepoError as exc:
        raise HTTPException(400, str(exc)) from exc
    return await store.create_project(spec)


@router.get("/projects/{project_id}", response_model=ProjectOut)
async def get_project(request: Request, project_id: str) -> ProjectOut:
    project = await _store(request).get_project(project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    return project


async def _require_repo_ready(project: ProjectOut) -> None:
    """提交前看一眼仓库，挡住注定跑不起来的任务。

    **空仓库要放行** —— 全新项目就是这样的，平台会替它造一个初始提交当起点
    （见 `WorkspaceManager._prepare`）。只有「有分支但没有 target_branch」才拦：
    那说明推错了分支或者项目配错了，而任务会在切工作区时失败，用户要等到
    排到队才看得到一个「失败」。
    """
    repo = Git(Path(project.repo_path))
    try:
        branches = await repo.branches()
    except GitError as exc:
        raise HTTPException(409, f"读不了仓库 {project.repo_path}：{exc}") from exc
    if branches and project.target_branch not in branches:
        raise HTTPException(
            409,
            f"仓库里没有 {project.target_branch} 分支（现有：{'、'.join(branches)}）—— "
            f"先 push 它，或者把项目的 target_branch 改成已有分支",
        )


@router.post("/projects/{project_id}/tasks", response_model=TaskOut, status_code=201)
async def create_task(request: Request, project_id: str, spec: TaskCreate) -> TaskOut:
    store = _store(request)
    project = await store.get_project(project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    await _require_repo_ready(project)
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


@router.get("/tasks/{task_id}/messages", response_model=list[TaskMessageOut])
async def list_task_messages(request: Request, task_id: str) -> list[TaskMessageOut]:
    store = _store(request)
    if await store.get_task(task_id) is None:
        raise HTTPException(404, "任务不存在")
    return await store.list_messages(task_id)


@router.post("/tasks/{task_id}/messages", response_model=TaskOut)
async def add_task_message(request: Request, task_id: str, spec: TaskMessageIn) -> TaskOut:
    """追加一轮，任务回到队列 —— 调度器会带着同一个会话接着跑。

    允许的状态只有待审阅和失败：已合并/已丢弃的任务工作区和分支都清掉了，
    接着聊没有意义（那时候该开新任务）。
    """
    store = _store(request)
    task = await store.get_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task.status not in (TaskStatus.AWAITING_REVIEW, TaskStatus.FAILED):
        raise HTTPException(409, f"任务状态为 {task.status}，只有待审阅或失败的任务能继续")

    await store.add_message(task_id, spec.text)
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


async def _workspace_for(request: Request, task_id: str, *, required: bool = True):
    """从任务记录还原出 Workspace（审批发生在任务跑完之后，内存里已经没有它了）。

    `required=False` 时，没有工作区就返回 None 而不是报错 —— 丢弃要用到：
    早期版本留下过没有工作区记录的任务，那种任务批不了，再不让丢就只能永远挂着。
    """
    store = _store(request)
    raw = await store.get_task_raw(task_id)
    if raw is None:
        raise HTTPException(404, "任务不存在")
    if not raw["worktree_path"] or not raw["branch"]:
        if required:
            raise HTTPException(409, "该任务还没有工作区（可能还没执行过）")
        return store, raw, None

    project = await store.get_project(raw["project_id"])
    if project is None:
        raise HTTPException(404, "项目不存在")

    workspace = Workspace(
        path=Path(raw["worktree_path"]),
        repo_path=Path(project.repo_path),
        branch=raw["branch"],
        base_commit=raw["base_commit"] or "",
        target_branch=project.target_branch,
        # 分支被清掉后（批准/丢弃时），回看 diff 要靠它
        commit_sha=raw["commit_sha"],
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
    """丢弃：删掉 worktree 和分支，target 不受影响。

    **没有工作区的任务也能丢** —— 那种任务批不了，再不让丢就只能一直挂在待审阅里。
    """
    store, _raw, workspace = await _workspace_for(request, task_id, required=False)
    task = await store.get_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task.status not in (TaskStatus.AWAITING_REVIEW, TaskStatus.FAILED, TaskStatus.INTERRUPTED):
        raise HTTPException(409, f"任务状态为 {task.status}，不能丢弃")

    if workspace is not None:
        await request.app.state.workspaces.cleanup(workspace, delete_branch=True)
    await store.set_status(task_id, TaskStatus.DISCARDED)

    refreshed = await store.get_task(task_id)
    assert refreshed is not None
    return refreshed


@router.get("/usage", response_model=UsageOut)
async def get_usage(request: Request, project_id: str | None = None) -> UsageOut:
    """花钱可追溯。

    ⚠️ 不给 `total_cost_usd` 留位置：那是 Claude Code 按另一端价目算的，
    跟实际付费对不上。这里只按 token 和**用户自己配的**价目表算。
    """
    store = _store(request)
    prices = request.app.state.prices
    rows = await store.usage_summary(project_id=project_id)

    by_model = [
        ModelUsage(
            model=r["model"],
            attempts=r["attempts"],
            input_tokens=r["input_tokens"],
            output_tokens=r["output_tokens"],
            cache_read_tokens=r["cache_read_tokens"],
            cache_creation_tokens=r["cache_creation_tokens"],
            cost_usd=r["cost_usd"],
        )
        for r in rows
    ]
    note = ""
    if not prices.configured:
        note = (
            f"未配置价目表。在 {request.app.state.config.root / 'prices.toml'} 里填入"
            "各模型的单价（每百万 token）后，成本会自动出现。"
        )
    return UsageOut(
        total_attempts=await store.count_attempts(),
        task_attempts=await store.count_attempts(origin="task"),
        sediment_attempts=await store.count_attempts(origin="sediment"),
        by_model=by_model,
        price_version=prices.version,
        prices_configured=prices.configured,
        note=note,
    )


# ---------- 文件浏览 ----------

# 单次返回的文件大小上限。超过就截断 —— 前端也渲染不动。
MAX_FILE_BYTES = 512 * 1024


def _clean_path(raw: str) -> str:
    """把 HTTP 传来的路径归一化，挡住越界。

    git 自己也会拒绝 `..`（实测 `cat-file blob main:../../etc/passwd` 直接 fatal），
    这里再拦一道是省得让它跑到 git 那层。
    """
    parts = [p for p in raw.strip().strip("/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise HTTPException(400, "路径不能包含 ..")
    return "/".join(parts)


@router.get("/projects/{project_id}/tree", response_model=TreeOut)
async def get_project_tree(
    request: Request, project_id: str, path: str = "", ref: str | None = None
) -> TreeOut:
    """列出某个版本下某个目录的内容。ref 缺省是主干。

    用 ls-tree 而不是读工作区 —— 项目仓库是裸的，根本没有工作区可读。
    """
    project = await _require_project(request, project_id)
    ref = ref or project.target_branch
    rel = _clean_path(path)
    try:
        entries = await Git(Path(project.repo_path)).ls_tree(ref, rel)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except GitError as exc:
        raise HTTPException(404, f"读不到 {ref}:{rel or '.'}") from exc

    # 目录在前、文件在后，各自按名字排 —— 跟普通文件管理器一致
    ordered = sorted(entries, key=lambda e: (e.type != "tree", e.name.lower()))
    return TreeOut(
        ref=ref,
        path=rel,
        entries=[
            FileEntry(
                name=e.name,
                path=f"{rel}/{e.name}" if rel else e.name,
                type="dir" if e.type == "tree" else "file",
                size=e.size,
            )
            for e in ordered
        ],
    )


@router.get("/projects/{project_id}/file", response_model=FileOut)
async def get_project_file(
    request: Request, project_id: str, path: str, ref: str | None = None
) -> FileOut:
    project = await _require_project(request, project_id)
    ref = ref or project.target_branch
    rel = _clean_path(path)
    if not rel:
        raise HTTPException(400, "要指定文件路径")
    try:
        data, binary = await Git(Path(project.repo_path)).blob(ref, rel)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except GitError as exc:
        raise HTTPException(404, f"读不到 {ref}:{rel}") from exc

    return FileOut(
        ref=ref,
        path=rel,
        size=len(data),
        binary=binary,
        truncated=len(data) > MAX_FILE_BYTES,
        # 二进制不往回传内容，传了也没法看
        text="" if binary else data[:MAX_FILE_BYTES].decode("utf-8", "replace"),
    )


# ---------- 上下文 ----------


def _contexts(request: Request):
    return request.app.state.contexts


async def _require_project(request: Request, project_id: str) -> ProjectOut:
    project = await _store(request).get_project(project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    return project


@router.get("/projects/{project_id}/context", response_model=ContextOut)
async def get_context(request: Request, project_id: str) -> ContextOut:
    project = await _require_project(request, project_id)
    ctx_store = _contexts(request)
    pointer = ctx_store.read_pointer(project.name)
    return ContextOut(
        pointer=pointer,
        pointer_bytes=len(pointer.encode("utf-8")),
        pointer_soft_limit=POINTER_SOFT_LIMIT_BYTES,
        journal=ctx_store.read_journal(project.name),
        index=ctx_store.read_index(project.name),
        docs=[DocOut(path=d.path, size=d.size) for d in ctx_store.list_docs(project.name)],
    )


@router.put("/projects/{project_id}/context/pointer", response_model=ContextOut)
async def put_pointer(request: Request, project_id: str, payload: TextPayload) -> ContextOut:
    project = await _require_project(request, project_id)
    _contexts(request).write_pointer(project.name, payload.text)
    return await get_context(request, project_id)


@router.put("/projects/{project_id}/context/index", response_model=ContextOut)
async def put_index(request: Request, project_id: str, payload: TextPayload) -> ContextOut:
    project = await _require_project(request, project_id)
    _contexts(request).write_index(project.name, payload.text)
    return await get_context(request, project_id)


@router.get("/projects/{project_id}/context/docs/{path:path}")
async def get_doc(request: Request, project_id: str, path: str) -> dict:
    project = await _require_project(request, project_id)
    try:
        return {"path": path, "text": _contexts(request).read_doc(project.name, path)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError:
        raise HTTPException(404, "文档不存在") from None


@router.put("/projects/{project_id}/context/docs/{path:path}")
async def put_doc(request: Request, project_id: str, path: str, payload: TextPayload) -> dict:
    project = await _require_project(request, project_id)
    try:
        _contexts(request).write_doc(project.name, path, payload.text)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"path": path, "text": payload.text}


@router.delete("/projects/{project_id}/context/docs/{path:path}", status_code=204)
async def delete_doc(request: Request, project_id: str, path: str) -> None:
    project = await _require_project(request, project_id)
    try:
        _contexts(request).delete_doc(project.name, path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


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
