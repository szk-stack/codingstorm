"""定时任务与执行窗口的 HTTP 接口。"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from codingstorm.app import create_app
from codingstorm.config import Config
from codingstorm.models import TaskStatus
from codingstorm.timing import parse_utc

TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def client(tmp_path: Path):
    cfg = Config(root=tmp_path / "root")
    cfg.ensure_dirs()
    cfg.scheduler.window.enabled = True
    cfg.scheduler.window.timezone = "Asia/Shanghai"
    cfg.scheduler.window.windows = [["00:30", "08:30"]]
    app = create_app(cfg, start_scheduler=False)
    with TestClient(app) as c:
        c.app = app  # type: ignore[attr-defined]
        yield c


def _seed(repo_path: Path, branch: str = "main") -> None:
    def git(*args: str, check: bool = False):
        return subprocess.run(["git", *args], capture_output=True, text=True, check=check)

    if git("-C", str(repo_path), "rev-parse", "--verify", "--quiet",
           f"refs/heads/{branch}").returncode == 0:
        return
    work = repo_path.parent / f"{repo_path.name}.seed"
    shutil.rmtree(work, ignore_errors=True)
    git("clone", "-q", str(repo_path), str(work), check=True)
    git("-C", str(work), "checkout", "-q", "-b", branch, check=True)
    (work / "README.md").write_text("# seed\n", encoding="utf-8")
    git("-C", str(work), "add", "-A", check=True)
    git("-C", str(work), "-c", "user.email=t@l", "-c", "user.name=t",
        "commit", "-q", "-m", "init", check=True)
    git("-C", str(work), "push", "-q", "origin", branch, check=True)


@pytest.fixture
def project(client: TestClient) -> dict:
    r = client.post("/api/projects", json={"name": "demo"})
    assert r.status_code == 201, r.text
    p = r.json()
    _seed(Path(p["repo_path"]), p["target_branch"])
    return p


def _add(client: TestClient, project: dict, rule: dict, **extra) -> dict:
    body = {"title": "定时任务", "rule": rule, **extra}
    r = client.post(f"/api/projects/{project['id']}/schedules", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ---------- 建与查 ----------


def test_建每周定时任务(client, project):
    s = _add(client, project, {"type": "weekly", "days": ["mon", "wed"], "time": "09:00"})
    assert "周一" in s["rule_text"] and "09:00" in s["rule_text"]
    assert s["enabled"] is True
    assert s["run_count"] == 0
    assert s["missed_at"] is None

    local = parse_utc(s["next_run_at"]).astimezone(TZ)
    assert (local.hour, local.minute) == (9, 0)  # 落在配置时区的 09:00 上
    assert local.weekday() in (0, 2)


def test_每天规则的下一次在今天或明天(client, project):
    s = _add(client, project, {"type": "daily", "time": "09:00"})
    local = parse_utc(s["next_run_at"]).astimezone(TZ)
    assert (local.hour, local.minute) == (9, 0)
    assert local > datetime.now(TZ)


def test_一次性任务要给出未来时刻(client, project):
    future = datetime.now(TZ).replace(second=0, microsecond=0) + timedelta(days=1)
    s = _add(client, project, {"type": "once", "at": future.strftime("%Y-%m-%dT%H:%M")})
    assert "一次性" in s["rule_text"]


def test_一次性任务的时间已过要当场报错(client, project):
    """不能静默变成「永不触发」—— 刚打完命令，值得立刻知道写错了。"""

    r = client.post(
        f"/api/projects/{project['id']}/schedules",
        json={"title": "晚了", "rule": {"type": "once", "at": "2020-01-01T09:00"}},
    )
    assert r.status_code == 400
    assert "已经过去" in r.json()["detail"]


def test_规则写错要报_400(client, project):
    # 语义错误（形状对、内容不对）走我们自己的校验
    for bad in (
        {"type": "daily", "time": "九点"},
        {"type": "weekly", "time": "09:00"},  # 缺 days
        {"type": "weekly", "days": ["星期八"], "time": "09:00"},
        {"type": "daily"},  # 缺 time
    ):
        r = client.post(
            f"/api/projects/{project['id']}/schedules", json={"title": "x", "rule": bad}
        )
        assert r.status_code == 400, f"{bad} 应该被拒绝，实际 {r.status_code}"

    # 认不出的类型是形状问题，由 pydantic 在进处理函数之前就挡掉
    r = client.post(
        f"/api/projects/{project['id']}/schedules",
        json={"title": "x", "rule": {"type": "hourly"}},
    )
    assert r.status_code == 422


def test_项目不存在就建不了(client):
    r = client.post(
        "/api/projects/nope/schedules",
        json={"title": "x", "rule": {"type": "daily", "time": "09:00"}},
    )
    assert r.status_code == 404


def test_列表能按项目过滤(client, project):
    _add(client, project, {"type": "daily", "time": "09:00"})
    other = client.post("/api/projects", json={"name": "other"}).json()
    _seed(Path(other["repo_path"]), other["target_branch"])
    _add(client, other, {"type": "daily", "time": "09:00"})

    assert len(client.get("/api/schedules").json()) == 2
    assert len(client.get(f"/api/schedules?project_id={project['id']}").json()) == 1


# ---------- 改与删 ----------


def test_停用后不再有下次触发时刻(client, project):
    s = _add(client, project, {"type": "daily", "time": "09:00"})
    r = client.patch(f"/api/schedules/{s['id']}", json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    assert r.json()["next_run_at"] is None


def test_重新启用会重算下一次而不是补跑(client, project):
    s = _add(client, project, {"type": "daily", "time": "09:00"})
    client.patch(f"/api/schedules/{s['id']}", json={"enabled": False})
    r = client.patch(f"/api/schedules/{s['id']}", json={"enabled": True})
    assert r.json()["next_run_at"] is not None
    assert parse_utc(r.json()["next_run_at"]) > datetime.now(TZ)
    # 停用期间没有攒出任何任务
    assert client.get("/api/tasks").json() == []


def test_改规则会重算下一次(client, project):
    s = _add(client, project, {"type": "daily", "time": "09:00"})
    r = client.patch(f"/api/schedules/{s['id']}", json={"rule": {"type": "daily", "time": "03:00"}})
    local = parse_utc(r.json()["next_run_at"]).astimezone(TZ)
    assert (local.hour, local.minute) == (3, 0)


def test_改规则也要校验(client, project):
    s = _add(client, project, {"type": "daily", "time": "09:00"})
    r = client.patch(f"/api/schedules/{s['id']}", json={"rule": {"type": "daily", "time": "三点"}})
    assert r.status_code == 400


def test_改标题不影响排期(client, project):
    s = _add(client, project, {"type": "daily", "time": "09:00"})
    r = client.patch(f"/api/schedules/{s['id']}", json={"title": "换个名字"})
    assert r.json()["title"] == "换个名字"
    assert r.json()["next_run_at"] == s["next_run_at"]


def test_删除定时任务不动它已经生成的任务(client, project):
    s = _add(client, project, {"type": "daily", "time": "09:00"})
    task = client.post(f"/api/schedules/{s['id']}/run").json()

    assert client.delete(f"/api/schedules/{s['id']}").status_code == 204
    assert client.get(f"/api/schedules/{s['id']}").status_code == 404

    # 任务还在 —— 那可能是待审的产物，跟着一起消失才是真的丢东西
    assert client.get(f"/api/tasks/{task['id']}").status_code == 200


# ---------- 立刻跑一次 ----------


def test_立即跑一次不改动原定排期(client, project):
    s = _add(client, project, {"type": "daily", "time": "09:00"})
    task = client.post(f"/api/schedules/{s['id']}/run").json()

    assert task["schedule_id"] == s["id"]
    assert task["status"] == "queued"

    fresh = client.get(f"/api/schedules/{s['id']}").json()
    assert fresh["next_run_at"] == s["next_run_at"]  # 排期没动
    assert fresh["run_count"] == 0  # 手动那次不计进「自动触发次数」
    assert fresh["last_task_id"] == task["id"]


def test_定时任务生成的会出现在任务列表里(client, project):
    s = _add(client, project, {"type": "daily", "time": "09:00"})
    task = client.post(f"/api/schedules/{s['id']}/run").json()
    listed = client.get(f"/api/tasks?project_id={project['id']}").json()
    assert [t["id"] for t in listed] == [task["id"]]
    assert listed[0]["schedule_id"] == s["id"]


def test_上一轮没审会体现在列表里(client, project):
    s = _add(client, project, {"type": "daily", "time": "09:00"})
    task = client.post(f"/api/schedules/{s['id']}/run").json()
    listed = client.get("/api/schedules").json()[0]
    assert listed["last_task_id"] == task["id"]
    assert listed["last_task_status"] == "queued"

    # 转成待审后，界面据此提示「本轮会切在旧主干上」
    asyncio.run(
        client.app.state.store.set_status(task["id"], TaskStatus.AWAITING_REVIEW)  # type: ignore[attr-defined]
    )
    assert client.get("/api/schedules").json()[0]["last_task_status"] == "awaiting_review"


# ---------- 执行窗口 ----------


def test_窗口状态(client):
    w = client.get("/api/window").json()
    assert w["enabled"] is True
    assert w["disabled"] is False
    assert w["timezone"] == "Asia/Shanghai"
    assert w["windows"] == [["00:30", "08:30"]]
    assert w["now"]
    # 此刻要么开着（有下次关闭时刻）要么关着（有下次开启时刻）
    assert w["open"] is (w["next_close_at"] is not None)
    assert (not w["open"]) is (w["next_open_at"] is not None)


def test_临时关掉窗口(client):
    w = client.post("/api/window", json={"disabled": True}).json()
    assert w["disabled"] is True
    assert w["open"] is True  # 一关就全放行
    assert client.get("/api/window").json()["disabled"] is True

    w = client.post("/api/window", json={"disabled": False}).json()
    assert w["disabled"] is False


def test_项目可以单独不受窗口限制(client, project):
    r = client.put(f"/api/projects/{project['id']}/window", json={"mode": "always"})
    assert r.status_code == 200
    assert r.json()["window_override"] == '{"mode": "always"}'


def test_项目可以配自己的时段(client, project):
    r = client.put(
        f"/api/projects/{project['id']}/window",
        json={"mode": "custom", "windows": [["09:00", "18:00"]]},
    )
    assert r.status_code == 200
    assert "09:00" in r.json()["window_override"]


def test_custom_模式必须给时段(client, project):
    r = client.put(f"/api/projects/{project['id']}/window", json={"mode": "custom"})
    assert r.status_code == 400


def test_坏时段要报_400(client, project):
    r = client.put(
        f"/api/projects/{project['id']}/window",
        json={"mode": "custom", "windows": [["九点", "18:00"]]},
    )
    assert r.status_code == 400


def test_恢复继承全局(client, project):
    client.put(f"/api/projects/{project['id']}/window", json={"mode": "always"})
    r = client.put(f"/api/projects/{project['id']}/window", json={"mode": "inherit"})
    assert r.json()["window_override"] is None
