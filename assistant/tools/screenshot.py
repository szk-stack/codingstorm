"""
工具：screenshot —— 用 Edge 无头模式给网页截图

Windows 自带 Edge（Chromium 内核），headless 模式可以在命令行截图——
不弹浏览器窗口，也不用另外装浏览器驱动。

截图存到工作区的 .screenshots/ 目录（隐藏目录，list_dir 不会列出它，
不会干扰模型浏览工作区）。截完想"看"效果，接着调 view_image。
"""

import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import config
import toolbox
from toolbox import register
from tools._common import safe_path

# Edge 的常见安装位置
EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

# 截图超时（秒）：页面卡死时不至于拖死对话
TIMEOUT_SECONDS = 90

# ---- ① 工具说明书 ----
DESCRIPTION = {
    "name": "screenshot",
    "description": (
        "用 Edge 无头模式给一个本地 HTML 文件或网址截图（PNG），存到工作区的 .screenshots/ 目录。"
        "适合检查自己生成的页面效果：截完再用 view_image 查看。"
        "可选参数：width/height 视口尺寸（默认 1200x800）、wait_ms 页面渲染等待的虚拟毫秒（默认 1500）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "工作区内的 .html 文件路径，或 http(s) 网址",
            },
            "width": {"type": "integer", "description": "视口宽度（默认 1200）"},
            "height": {"type": "integer", "description": "视口高度（默认 800）"},
            "wait_ms": {
                "type": "integer",
                "description": "页面渲染等待的虚拟毫秒（默认 1500；游戏/动画可调大）",
            },
        },
        "required": ["target"],
    },
}


def _find_edge():
    """找 Edge 可执行文件；找不到返回 None。"""
    for candidate in EDGE_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return shutil.which("msedge")


# ---- ② 执行函数 ----
def run(arguments):
    target = (arguments.get("target") or "").strip()
    if not target:
        return "错误：target 不能为空"

    # 目标 URL：http(s) 直接用；否则当作工作区内的 html 文件
    if target.lower().startswith(("http://", "https://")):
        url = target
    else:
        try:
            page = safe_path(target)
        except ValueError as e:
            return f"错误：{e}"
        if not page.exists() or not page.is_file():
            return f"错误：页面不存在：{target}"
        url = page.as_uri()      # 转成 file:// URL（中文路径也会正确编码）

    edge = _find_edge()
    if not edge:
        return "错误：没找到 Microsoft Edge（截图需要它）。"

    width = int(arguments.get("width") or 1200)
    height = int(arguments.get("height") or 800)
    wait_ms = int(arguments.get("wait_ms") or 1500)

    # 截到工作区的 .screenshots/ 目录（隐藏目录，不干扰列表）
    workspace = Path(toolbox.current_workspace() or str(toolbox.PROJECT_ROOT))
    out_dir = workspace / ".screenshots"
    out_dir.mkdir(exist_ok=True)
    filename = f"shot-{time.strftime('%Y%m%d-%H%M%S')}.png"
    out_path = out_dir / filename

    # 临时 profile：避免和用户正在用的 Edge 冲突，也不污染它
    profile_dir = tempfile.mkdtemp(prefix="jian-shot-")
    try:
        completed = subprocess.run(
            [
                edge,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                f"--user-data-dir={profile_dir}",
                f"--screenshot={out_path}",
                f"--window-size={width},{height}",
                f"--virtual-time-budget={wait_ms}",
                url,
            ],
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # 不弹黑窗口
        )
    except subprocess.TimeoutExpired:
        return f"错误：截图超时（{TIMEOUT_SECONDS} 秒）——页面可能卡住了"
    except Exception as e:
        return f"错误：截图失败：{e}"
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)

    if not out_path.exists() or out_path.stat().st_size == 0:
        detail = (completed.stderr or b"").decode("utf-8", errors="replace")[:300]
        return f"错误：截图没有生成（Edge 输出：{detail}）"

    return (
        f"截图已保存：.screenshots/{filename}"
        f"（{width}x{height}，{out_path.stat().st_size // 1024} KB）。"
        "想看效果请调用 view_image 查看这张图。"
    )


# ---- ③ 登记进注册表 ----
# 截图主要服务"看图自检"，跟视觉功能一起开关
if config.ENABLE_VISION:
    register(DESCRIPTION, run)
