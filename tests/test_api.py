"""HTTP 接口测试。不启动调度器，只验证路由与校验。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codingstorm.app import create_app
from codingstorm.config import Config


@pytest.fixture
def client(tmp_path: Path):
    cfg = Config(root=tmp_path / "root")
    cfg.ensure_dirs()
    app = create_app(cfg, start_scheduler=False)
    with TestClient(app) as c:
        c.app = app  # type: ignore[attr-defined]
        yield c


def _mk_project(client: TestClient, name: str = "demo", repo: str = "/tmp/demo") -> dict:
    r = client.post("/api/projects", json={"name": name, "repo_path": repo})
    assert r.status_code == 201, r.text
    return r.json()


def test_health(client: TestClient):
    assert client.get("/api/health").json() == {"ok": True}


def test_create_and_get_project(client: TestClient):
    p = _mk_project(client)
    assert p["name"] == "demo"
    assert p["target_branch"] == "main"  # 默认值
    assert p["enabled"] is True

    got = client.get(f"/api/projects/{p['id']}")
    assert got.status_code == 200
    assert got.json()["id"] == p["id"]

    listed = client.get("/api/projects").json()
    assert [x["name"] for x in listed] == ["demo"]


def test_duplicate_project_name_rejected(client: TestClient):
    _mk_project(client, "demo")
    r = client.post("/api/projects", json={"name": "demo", "repo_path": "/tmp/other"})
    assert r.status_code == 409


def test_project_name_pattern_enforced(client: TestClient):
    r = client.post("/api/projects", json={"name": "有 空格", "repo_path": "/tmp/x"})
    assert r.status_code == 422


def test_get_missing_project_returns_404(client: TestClient):
    assert client.get("/api/projects/nope").status_code == 404


def test_create_task_and_list(client: TestClient):
    p = _mk_project(client)
    r = client.post(
        f"/api/projects/{p['id']}/tasks",
        json={"title": "修一个 bug", "body": "详情", "kind": "bug", "priority": 5},
    )
    assert r.status_code == 201, r.text
    task = r.json()
    assert task["status"] == "queued"
    assert task["kind"] == "bug"
    assert task["priority"] == 5

    tasks = client.get("/api/tasks", params={"project_id": p["id"]}).json()
    assert len(tasks) == 1
    assert tasks[0]["title"] == "修一个 bug"


def test_create_task_unknown_project_404(client: TestClient):
    r = client.post("/api/projects/nope/tasks", json={"title": "x"})
    assert r.status_code == 404


def test_create_task_rejects_empty_title(client: TestClient):
    p = _mk_project(client)
    r = client.post(f"/api/projects/{p['id']}/tasks", json={"title": ""})
    assert r.status_code == 422


def test_attempts_and_events_start_empty(client: TestClient):
    p = _mk_project(client)
    task = client.post(f"/api/projects/{p['id']}/tasks", json={"title": "x"}).json()
    assert client.get(f"/api/tasks/{task['id']}/attempts").json() == []
    assert client.get(f"/api/tasks/{task['id']}/events").json() == []


def test_requeue_rejects_queued_task(client: TestClient):
    """只有失败/中断/取消的任务才能重新入队。"""
    p = _mk_project(client)
    task = client.post(f"/api/projects/{p['id']}/tasks", json={"title": "x"}).json()
    r = client.post(f"/api/tasks/{task['id']}/requeue")
    assert r.status_code == 409


def test_cancel_then_requeue(client: TestClient):
    p = _mk_project(client)
    task = client.post(f"/api/projects/{p['id']}/tasks", json={"title": "x"}).json()

    cancelled = client.post(f"/api/tasks/{task['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    requeued = client.post(f"/api/tasks/{task['id']}/requeue")
    assert requeued.status_code == 200
    assert requeued.json()["status"] == "queued"


def test_requeue_missing_task_404(client: TestClient):
    assert client.post("/api/tasks/nope/requeue").status_code == 404


def test_scheduler_not_started_in_tests(client: TestClient):
    """start_scheduler=False 时不应有调度循环在跑。"""
    scheduler = client.app.state.scheduler  # type: ignore[attr-defined]
    assert scheduler.running == {}
