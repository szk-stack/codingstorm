"""打开系统文件夹选择器。

浏览器出于安全限制拿不到本地绝对路径，所以「选择文件夹」只能由服务端进程
弹出系统对话框。它只在 **codingstorm 和用户在同一台机器上** 时有意义；远程
服务器上弹的是服务器桌面的对话框。

实现按平台分支：Windows 用 PowerShell 的 FolderBrowserDialog（失败退 tkinter），
macOS 用 osascript，Linux 用 zenity / kdialog。没有图形环境时抛 PickerUnavailable，
HTTP 层会返回 501，让前端提示手动输入。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


class PickerUnavailable(RuntimeError):
    """当前环境弹不出文件夹选择器（无图形界面、缺命令等）。"""


def pick_directory() -> str | None:
    """弹系统文件夹选择器。取消返回 None。"""
    if os.name == "nt":
        return _windows()
    if sys.platform == "darwin":
        return _macos()
    return _linux()


def _finish(path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path.strip()).expanduser()
    return str(p) if p.is_dir() else None


def _run(cmd: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise PickerUnavailable("文件夹选择器超时") from exc
    except OSError as exc:
        raise PickerUnavailable(f"无法启动文件夹选择器：{exc}") from exc


def _windows() -> str | None:
    # Windows 的 stdout 编码跟控制台代码页走，中文路径可能乱码；让 PowerShell
    # 把结果写进 UTF-8 临时文件，Python 再读。
    fd, name = tempfile.mkstemp(prefix="codingstorm-pick-", suffix=".txt")
    os.close(fd)
    out = Path(name)
    out.unlink(missing_ok=True)
    escaped = str(out).replace("'", "''")
    script = f"""
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = '选择项目文件夹'
$dialog.ShowNewFolderButton = $true
$result = $dialog.ShowDialog()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {{
  [System.IO.File]::WriteAllText('{escaped}', $dialog.SelectedPath, [System.Text.Encoding]::UTF8)
}}
"""
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        out.unlink(missing_ok=True)
        return _tk_directory()
    try:
        proc = _run([exe, "-NoProfile", "-STA", "-Command", script])
        if proc.returncode != 0:
            # PowerShell 本身失败（不是用户取消）—— 退到 tkinter 再试一次
            return _tk_directory()
        if not out.exists():
            return None
        return _finish(out.read_text(encoding="utf-8-sig"))
    finally:
        out.unlink(missing_ok=True)


def _tk_directory() -> str | None:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:  # pragma: no cover - 取决于运行环境
        raise PickerUnavailable("当前环境没有可用的图形文件夹选择器") from exc
    try:
        root = tk.Tk()
    except Exception as exc:  # pragma: no cover
        raise PickerUnavailable(f"无法打开图形界面：{exc}") from exc
    root.withdraw()
    try:
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(title="选择项目文件夹", mustexist=False)
    finally:
        root.destroy()
    return _finish(path)


def _macos() -> str | None:
    proc = _run(
        ["osascript", "-e", 'POSIX path of (choose folder with prompt "选择项目文件夹")']
    )
    if proc.returncode != 0:
        return None
    lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    return _finish(lines[-1] if lines else None)


def _linux() -> str | None:
    if shutil.which("zenity"):
        cmd = ["zenity", "--file-selection", "--directory", "--title=选择项目文件夹"]
    elif shutil.which("kdialog"):
        cmd = ["kdialog", "--getexistingdirectory", str(Path.home())]
    else:
        raise PickerUnavailable("没有 zenity/kdialog，无法打开文件夹选择器")
    proc = _run(cmd)
    if proc.returncode != 0:
        return None
    return _finish(proc.stdout)
