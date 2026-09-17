"""边界拦截钩子测试。

直接以子进程方式跑 hook 脚本，喂 JSON 到 stdin，看退出码 ——
跟 Claude Code 调用它的方式一致。
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from codingstorm.config import ClaudeConfig, Config
from codingstorm.guard import HOOK_SOURCE, GuardInstaller


@pytest.fixture
def installed(tmp_path: Path) -> Path:
    cfg = Config(root=tmp_path / "root")
    cfg.ensure_dirs()
    cfg.claude = ClaudeConfig(guard_enabled=True)
    settings = GuardInstaller(cfg).install()
    assert settings is not None
    return cfg.root / "guard"


def run_hook(guard_dir: Path, tool: str, tool_input: dict) -> tuple[int, str]:
    payload = json.dumps({"tool_name": tool, "tool_input": tool_input, "session_id": "s"})
    proc = subprocess.run(
        [sys.executable, str(guard_dir / "guard.py")],
        input=payload, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    return proc.returncode, proc.stderr


# ---------- 装配 ----------

def test_install_writes_all_files(installed: Path):
    assert (installed / "guard.py").exists()
    assert (installed / "guard.json").exists()
    assert (installed / "settings.json").exists()

    settings = json.loads((installed / "settings.json").read_text(encoding="utf-8"))
    hooks = settings["hooks"]["PreToolUse"]
    assert any(h["matcher"] == "Bash" for h in hooks)
    # 用解释器显式调用，不依赖可执行位和 shebang
    assert sys.executable in hooks[0]["hooks"][0]["command"]


def test_install_disabled_returns_none(tmp_path: Path):
    cfg = Config(root=tmp_path / "root")
    cfg.ensure_dirs()
    cfg.claude = ClaudeConfig(guard_enabled=False)
    assert GuardInstaller(cfg).install() is None


def test_shipped_hook_exists():
    assert HOOK_SOURCE.exists(), "发行包里必须带上 hook 脚本"


# ---------- 拦截 ----------

def test_blocks_git_push(installed: Path):
    code, err = run_hook(installed, "Bash", {"command": "git push origin main"})
    assert code == 2, "应当阻断"
    assert "BLOCKED" in err


def test_blocks_push_with_flags_and_path_prefix(installed: Path):
    for cmd in (
        "cd /repo && git push",
        "git -C /repo push --force",
        "/usr/bin/git push",
        "echo hi; git push",
    ):
        code, _ = run_hook(installed, "Bash", {"command": cmd})
        assert code == 2, f"应当阻断: {cmd}"


def test_blocks_remote_changes(installed: Path):
    for cmd in ("git remote add origin x", "git remote set-url origin y"):
        code, _ = run_hook(installed, "Bash", {"command": cmd})
        assert code == 2, f"应当阻断: {cmd}"


def test_allows_normal_commands(installed: Path):
    for cmd in (
        "ls -la",
        "git status",
        "git commit -m x",
        "git log --oneline",
        "pytest -q",
        "git pushd",  # 不能因为前缀像就误伤
    ):
        code, err = run_hook(installed, "Bash", {"command": cmd})
        assert code == 0, f"不该阻断: {cmd}（stderr={err}）"


def test_network_allowed_by_default(installed: Path):
    """网络默认放行 —— 挡掉会误伤正常的依赖安装。"""
    code, _ = run_hook(installed, "Bash", {"command": "pip install requests"})
    assert code == 0


def test_network_blocking_when_enabled(tmp_path: Path):
    cfg = Config(root=tmp_path / "root")
    cfg.ensure_dirs()
    cfg.claude = ClaudeConfig(guard_enabled=True, guard_block_network=True)
    GuardInstaller(cfg).install()
    guard_dir = cfg.root / "guard"

    for cmd in ("curl https://example.com", "pip install requests", "npm install"):
        code, _ = run_hook(guard_dir, "Bash", {"command": cmd})
        assert code == 2, f"应当阻断: {cmd}"

    # WebFetch 也要挡
    code, _ = run_hook(guard_dir, "WebFetch", {"url": "https://x"})
    assert code == 2


def test_allow_list_overrides_deny(tmp_path: Path):
    cfg = Config(root=tmp_path / "root")
    cfg.ensure_dirs()
    cfg.claude = ClaudeConfig(
        guard_enabled=True, guard_allow=[r"git push origin docs-only"]
    )
    GuardInstaller(cfg).install()
    guard_dir = cfg.root / "guard"

    code, _ = run_hook(guard_dir, "Bash", {"command": "git push origin docs-only"})
    assert code == 0, "白名单应当放行"
    code, _ = run_hook(guard_dir, "Bash", {"command": "git push origin main"})
    assert code == 2, "白名单之外的仍要挡"


def test_extra_deny_patterns(tmp_path: Path):
    cfg = Config(root=tmp_path / "root")
    cfg.ensure_dirs()
    cfg.claude = ClaudeConfig(guard_enabled=True, guard_extra_deny=[r"rm -rf /"])
    GuardInstaller(cfg).install()
    guard_dir = cfg.root / "guard"

    code, _ = run_hook(guard_dir, "Bash", {"command": "rm -rf /"})
    assert code == 2


# ---------- 健壮性与审计 ----------

def test_empty_stdin_does_not_block(installed: Path):
    """读不懂就放行 —— 不能因为解析失败把正常流程卡死。"""
    proc = subprocess.run(
        [sys.executable, str(installed / "guard.py")],
        input="", capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 0


def test_malformed_stdin_does_not_block(installed: Path):
    proc = subprocess.run(
        [sys.executable, str(installed / "guard.py")],
        input="{不是 json", capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 0


def test_writes_audit_log(installed: Path):
    run_hook(installed, "Bash", {"command": "ls"})
    run_hook(installed, "Bash", {"command": "git push"})

    log = (installed / "audit.log").read_text(encoding="utf-8").strip().splitlines()
    assert len(log) == 2
    assert "git push" in log[1]
    assert json.loads(log[0])["tool"] == "Bash"


def test_missing_rules_file_uses_safe_defaults(tmp_path: Path):
    """规则文件缺失时用保守默认值，而不是全放行。"""
    d = tmp_path / "bare"
    d.mkdir()
    (d / "guard.py").write_text(HOOK_SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
    code, _ = run_hook(d, "Bash", {"command": "git push"})
    assert code == 2
