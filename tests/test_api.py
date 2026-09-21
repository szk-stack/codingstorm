"""HTTP 接口测试。不启动调度器，只验证路由与校验。"""

import shutil
import sqlite3
import subprocess
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


def _seed(repo_path: Path, branch: str = "main") -> None:
    """确保仓库里有一条分支。

    空仓库现在收不了任务（提交时就会挡下），所以凡是要提交任务的测试都得先有提交。
    已经有该分支就什么都不做。
    """
    def git(*args: str, check: bool = False):
        return subprocess.run(["git", *args], capture_output=True, text=True, check=check)

    if git("-C", str(repo_path), "rev-parse", "--verify", "--quiet",
           f"refs/heads/{branch}").returncode == 0:
        return
    work = repo_path.parent / f"{repo_path.name}.seed"
    shutil.rmtree(work, ignore_errors=True)
    # 裸仓库没有工作区，只能 clone 出来提交再 push 回去
    git("clone", "-q", str(repo_path), str(work), check=True)
    git("-C", str(work), "checkout", "-q", "-b", branch, check=True)
    (work / "README.md").write_text("# seed\n", encoding="utf-8")
    git("-C", str(work), "add", "-A", check=True)
    git("-C", str(work), "-c", "user.email=t@l", "-c", "user.name=t",
        "commit", "-q", "-m", "init", check=True)
    git("-C", str(work), "push", "-q", "origin", branch, check=True)


def _mk_project(
    client: TestClient, name: str = "demo", repo: str | None = None, *, seed: bool = True
) -> dict:
    body: dict = {"name": name}
    if repo is not None:
        body["repo_path"] = repo
    r = client.post("/api/projects", json=body)
    assert r.status_code == 201, r.text
    p = r.json()
    if seed:
        _seed(Path(p["repo_path"]), p["target_branch"])
    return p


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


def test_repo_path_defaults_to_root_repos(client: TestClient):
    """不填仓库路径时，在 {root}/repos/<name>.git 建一个空裸仓库。"""
    p = _mk_project(client, "myapp")
    expected = client.app.state.config.repos_dir / "myapp.git"  # type: ignore[attr-defined]
    assert p["repo_path"] == str(expected)
    assert expected.is_dir()
    assert (expected / "HEAD").exists()


def test_new_repo_head_points_at_target_branch(client: TestClient):
    """新建的裸仓库 HEAD 指向 target_branch，用户直接 clone 就落在主干上。"""
    _mk_project(client, "myapp", repo="")
    head = (client.app.state.config.repos_dir / "myapp.git" / "HEAD").read_text()  # type: ignore[attr-defined]
    assert head.strip() == "ref: refs/heads/main"


def test_explicit_repo_path_must_exist(client: TestClient):
    r = client.post("/api/projects", json={"name": "x", "repo_path": "/nope/nowhere.git"})
    assert r.status_code == 400
    assert "不存在" in r.json()["detail"]


def test_explicit_repo_path_must_be_a_repo(client: TestClient, tmp_path: Path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    r = client.post("/api/projects", json={"name": "x", "repo_path": str(plain)})
    assert r.status_code == 400
    assert "不是 git 仓库" in r.json()["detail"]


def test_missing_target_branch_rejected(client: TestClient, make_repo):
    """分支填错要当场报错，并把实际存在的分支列出来。"""
    repo = make_repo("r")
    r = client.post(
        "/api/projects",
        json={"name": "x", "repo_path": str(repo), "target_branch": "trunk"},
    )
    assert r.status_code == 400
    assert "main" in r.json()["detail"]


def test_empty_repo_allowed(client: TestClient, tmp_path: Path):
    """空仓库放行 —— 显然是先注册、再从本地 push。"""
    empty = tmp_path / "empty.git"
    subprocess.run(["git", "init", "-q", "--bare", str(empty)], check=True)
    r = client.post("/api/projects", json={"name": "x", "repo_path": str(empty)})
    assert r.status_code == 201, r.text


def test_duplicate_project_name_rejected(client: TestClient):
    _mk_project(client, "demo")
    r = client.post("/api/projects", json={"name": "demo"})
    assert r.status_code == 409


def test_project_name_pattern_enforced(client: TestClient):
    r = client.post("/api/projects", json={"name": "有 空格"})
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


def test_create_task_on_empty_repo_allowed(client: TestClient):
    """空仓库放行 —— 全新项目就是这样，平台会给它造一个初始提交当起点。"""
    p = _mk_project(client, "fresh", seed=False)
    r = client.post(f"/api/projects/{p['id']}/tasks", json={"title": "写一个快速排序"})
    assert r.status_code == 201, r.text


def test_create_task_on_missing_branch_rejected(client: TestClient):
    """仓库里有分支，但推的不是 target_branch 那条 —— 同样要当场挡下。"""
    p = _mk_project(client, "wrong-branch", seed=False)
    _seed(Path(p["repo_path"]), "dev")  # 推的是 dev，不是 main
    r = client.post(f"/api/projects/{p['id']}/tasks", json={"title": "x"})
    assert r.status_code == 409
    assert "没有 main 分支" in r.json()["detail"]


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


def test_task_out_exposes_git_fields(client: TestClient):
    """分支、基点、提交这些字段要暴露出来，否则前端没法展示审阅上下文。"""
    p = _mk_project(client)
    task = client.post(f"/api/projects/{p['id']}/tasks", json={"title": "x"}).json()
    for field in ("branch", "worktree_path", "base_commit", "commit_sha",
                  "merge_commit_sha", "last_event_at"):
        assert field in task, f"TaskOut 缺少字段 {field}"


def test_context_endpoints(client: TestClient):
    p = _mk_project(client)

    got = client.get(f"/api/projects/{p['id']}/context").json()
    assert "pointer" in got and got["pointer_soft_limit"] > 0
    assert got["docs"], "应当已经生成了 INDEX.md 模板"

    # 改指针图
    updated = client.put(
        f"/api/projects/{p['id']}/context/pointer",
        json={"text": "# 约定\n\n跑 `pytest -q`\n"},
    ).json()
    assert "pytest -q" in updated["pointer"]
    assert updated["pointer_bytes"] > 0

    # 文档增删改查
    client.put(f"/api/projects/{p['id']}/context/docs/api.md", json={"text": "# 接口\n"})
    doc = client.get(f"/api/projects/{p['id']}/context/docs/api.md").json()
    assert doc["text"] == "# 接口\n"

    paths = {d["path"] for d in client.get(f"/api/projects/{p['id']}/context").json()["docs"]}
    assert "api.md" in paths

    assert client.delete(f"/api/projects/{p['id']}/context/docs/api.md").status_code == 204
    assert client.get(f"/api/projects/{p['id']}/context/docs/api.md").status_code == 404


def test_context_doc_path_traversal_rejected(client: TestClient):
    """文档路径来自 HTTP，必须挡住越界。"""
    p = _mk_project(client)
    r = client.put(
        f"/api/projects/{p['id']}/context/docs/..%2F..%2Fevil.md",
        json={"text": "x"},
    )
    assert r.status_code in (400, 404)
    # 确认没有真的写出去
    root = client.app.state.config.contexts_dir
    assert not (root.parent / "evil.md").exists()


def test_context_unknown_project_404(client: TestClient):
    assert client.get("/api/projects/nope/context").status_code == 404


def test_scheduler_not_started_in_tests(client: TestClient):
    """start_scheduler=False 时不应有调度循环在跑。"""
    scheduler = client.app.state.scheduler  # type: ignore[attr-defined]
    assert scheduler.running == {}


# ---------- 文件浏览 ----------


def _commit(repo: Path, message: str = "add files") -> None:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@l", "-c", "user.name=t",
         "commit", "-q", "-m", message],
        check=True,
    )


@pytest.fixture
def repo_with_files(make_repo) -> Path:
    repo = make_repo("withfiles")
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("print(1)\n", encoding="utf-8")
    (repo / "src" / "bin.dat").write_bytes(b"\x00\x01\x02")
    (repo / "src" / "中文.md").write_text("你好\n", encoding="utf-8")
    _commit(repo)
    return repo


def test_tree_lists_dirs_before_files(client: TestClient, repo_with_files: Path):
    p = _mk_project(client, "demo", repo=str(repo_with_files))
    tree = client.get(f"/api/projects/{p['id']}/tree").json()
    assert tree["ref"] == "main"
    assert tree["path"] == ""
    assert [(e["name"], e["type"]) for e in tree["entries"]] == [
        ("src", "dir"),
        ("README.md", "file"),
    ]


def test_tree_descends_and_keeps_non_ascii_names(client: TestClient, repo_with_files: Path):
    """中文文件名不能被 git 转义成八进制 —— quotePath 那个坑。"""
    p = _mk_project(client, "demo", repo=str(repo_with_files))
    tree = client.get(f"/api/projects/{p['id']}/tree", params={"path": "src"}).json()
    assert [e["name"] for e in tree["entries"]] == ["a.py", "bin.dat", "中文.md"]
    assert tree["entries"][0]["path"] == "src/a.py"


def test_file_returns_text(client: TestClient, repo_with_files: Path):
    p = _mk_project(client, "demo", repo=str(repo_with_files))
    f = client.get(f"/api/projects/{p['id']}/file", params={"path": "src/a.py"}).json()
    assert f["text"] == "print(1)\n"
    assert f["binary"] is False
    assert f["size"] == 9


def test_file_reports_binary_without_content(client: TestClient, repo_with_files: Path):
    p = _mk_project(client, "demo", repo=str(repo_with_files))
    f = client.get(f"/api/projects/{p['id']}/file", params={"path": "src/bin.dat"}).json()
    assert f["binary"] is True
    assert f["text"] == ""


def test_tree_can_read_a_branch(client: TestClient, repo_with_files: Path):
    """审阅时要能看任务分支的版本。"""
    subprocess.run(["git", "-C", str(repo_with_files), "branch", "cs/abc"], check=True)
    p = _mk_project(client, "demo", repo=str(repo_with_files))
    tree = client.get(f"/api/projects/{p['id']}/tree", params={"ref": "cs/abc"}).json()
    assert tree["ref"] == "cs/abc"
    assert [e["name"] for e in tree["entries"]] == ["src", "README.md"]


def test_tree_rejects_parent_traversal(client: TestClient, repo_with_files: Path):
    p = _mk_project(client, "demo", repo=str(repo_with_files))
    r = client.get(f"/api/projects/{p['id']}/tree", params={"path": "../../etc"})
    assert r.status_code == 400


def test_tree_rejects_option_like_ref(client: TestClient, repo_with_files: Path):
    """ref 以 - 开头会被 git 当成选项（实测 ls-tree --evil 报 unknown option）。"""
    p = _mk_project(client, "demo", repo=str(repo_with_files))
    r = client.get(f"/api/projects/{p['id']}/tree", params={"ref": "--evil"})
    assert r.status_code == 400


def test_tree_unknown_ref_is_404(client: TestClient, repo_with_files: Path):
    p = _mk_project(client, "demo", repo=str(repo_with_files))
    r = client.get(f"/api/projects/{p['id']}/tree", params={"ref": "nope"})
    assert r.status_code == 404


def test_file_missing_path_rejected(client: TestClient, repo_with_files: Path):
    p = _mk_project(client, "demo", repo=str(repo_with_files))
    assert client.get(f"/api/projects/{p['id']}/file", params={"path": ""}).status_code == 400


# ---------- 多轮对话 ----------


def test_followup_rejected_while_queued(client: TestClient):
    """还在排队/执行的任务不能追加 —— 先把这一轮跑完。"""
    p = _mk_project(client)
    task = client.post(f"/api/projects/{p['id']}/tasks", json={"title": "x"}).json()
    r = client.post(f"/api/tasks/{task['id']}/messages", json={"text": "再改改"})
    assert r.status_code == 409


def test_followup_rejects_empty_text(client: TestClient):
    p = _mk_project(client)
    task = client.post(f"/api/projects/{p['id']}/tasks", json={"title": "x"}).json()
    assert client.post(f"/api/tasks/{task['id']}/messages", json={"text": ""}).status_code == 422


def test_followup_missing_task_404(client: TestClient):
    assert client.post("/api/tasks/nope/messages", json={"text": "x"}).status_code == 404


def test_discard_works_without_workspace(client: TestClient):
    """「待审阅但没有工作区」的任务也要丢得掉。

    早期版本留下过这种任务（那会儿还没记 worktree_path）—— 它批不了，
    要是再不让丢，就永远挂在待审阅里清不掉。
    """
    p = _mk_project(client)
    task = client.post(f"/api/projects/{p['id']}/tasks", json={"title": "x"}).json()

    # 直接改库造出那个状态：走接口造不出来，任务得真跑过才会有工作区
    db = client.app.state.config.db_path  # type: ignore[attr-defined]
    con = sqlite3.connect(db)
    con.execute("UPDATE tasks SET status = 'awaiting_review' WHERE id = ?", (task["id"],))
    con.commit()
    con.close()

    r = client.post(f"/api/tasks/{task['id']}/discard")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "discarded"


def test_messages_start_empty(client: TestClient):
    p = _mk_project(client)
    task = client.post(f"/api/projects/{p['id']}/tasks", json={"title": "x"}).json()
    assert client.get(f"/api/tasks/{task['id']}/messages").json() == []
