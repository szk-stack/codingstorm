"""执行器公共工具：Windows 下 npm 垫片的解析。"""

from __future__ import annotations

from pathlib import Path

from codingstorm.agents.base import resolve_executable


def test_non_windows_returns_binary_unchanged():
    assert resolve_executable("/usr/local/bin/claude", windows=False) == "/usr/local/bin/claude"
    assert resolve_executable("claude", windows=False) == "claude"


def test_windows_resolves_shim_to_nested_exe(tmp_path: Path):
    """npm 的 claude.cmd 指向包内 bin\\claude.exe；直接启动 exe 才能避开 cmd.exe。"""
    bin_dir = tmp_path / "node_modules" / "pkg" / "bin"
    bin_dir.mkdir(parents=True)
    exe = bin_dir / "claude.exe"
    exe.write_bytes(b"")

    shim = tmp_path / "claude.cmd"
    shim.write_text(
        '@ECHO off\r\n"%dp0%\\node_modules\\pkg\\bin\\claude.exe" %*\r\n',
        encoding="utf-8",
    )
    # npm 同时生成的 POSIX sh 脚本，没有扩展名
    script = tmp_path / "claude"
    script.write_text("#!/bin/sh\n", encoding="utf-8")

    assert resolve_executable(str(script), windows=True) == str(exe)
    assert resolve_executable(str(shim), windows=True) == str(exe)


def test_windows_falls_back_to_cmd_without_exe(tmp_path: Path):
    shim = tmp_path / "tool.cmd"
    shim.write_text("@echo off\r\nnode cli.js %*\r\n", encoding="utf-8")
    assert resolve_executable(str(shim), windows=True) == str(shim)


def test_windows_exe_passthrough(tmp_path: Path):
    exe = tmp_path / "tool.exe"
    exe.write_bytes(b"")
    assert resolve_executable(str(exe), windows=True) == str(exe)
