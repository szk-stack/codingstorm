"""
工具：exec_command —— 在工作区目录里执行一条系统命令（Windows · cmd）

这是"手脚"里最强的一个：文件工具只能读写单个文件，它可以直接跑程序——
运行 Python 脚本、git 操作、查看环境、批量处理文件……

★ 安全边界（重要，必须说清楚）：
  文件工具被 safe_path() 锁在工作区里；但"命令"是一个任意程序，
  它天生能操作工作区之外的东西（cd ..、绝对路径都拦不住）——
  所以本工具的安全模型是：
    ① 工作目录默认锁定在当前工作区；
    ② 危险命令黑名单（防手滑，不是安全边界——刻意绕过总能绕过）；
    ③ 超时强制结束（防挂死）；
    ④ 输出截断（防塞爆模型上下文）。
  要更严格的管控，可以在 before_tool 空槽里挂"命令确认/白名单"插件。
"""

import re
import subprocess

import config
import toolbox
from toolbox import register

# 命令超时（秒）：超过就强制结束——防止命令挂住整个对话
TIMEOUT_SECONDS = 60

# 输出截断：太长的输出只返回开头这么多字符（防止把模型的上下文塞爆）
MAX_OUTPUT_CHARS = 8000

# "明显毁灭性"的命令模式：拦截它们，防手滑
# （不是安全边界——只是降低误操作的代价）
BLOCKED_PATTERNS = [
    r"(?i)\bformat\s+[a-z]:",                           # 格式化磁盘
    r"(?i)\bshutdown\b",                                 # 关机 / 重启
    r"(?i)\bdiskpart\b",                                 # 磁盘分区工具
    r"(?i)\b(rd|rmdir)\s+/s\b.*?\s+[a-z]:[\\/]?\s*$",   # 删整个盘（rd 版）
    r"(?i)\bdel\s+/s\b.*?\s+[a-z]:[\\/]?\s*$",          # 删整个盘（del 版）
    r"(?i)\brm\s+-[a-z]+\s+/(?=\s|$)",                   # rm -rf /（Unix 风格删根）
    r"\(\)\s*\{[^}]*\}\s*;",                             # fork bomb 的典型形状
]


def _decode(raw):
    """把子进程的输出字节转成字符串。

    ★ 为什么需要这个：Windows 上子进程的输出编码不统一——
      cmd 内建命令按系统代码页（中文系统是 GBK）输出，
      而很多现代程序（Python 等）输出 UTF-8。
    策略：先按 UTF-8 严格解码，失败则退回 GBK；
    再不行就 UTF-8 替换模式兜底（乱码好过崩溃）。
    """
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw.decode("gbk")
        except UnicodeDecodeError:
            return raw.decode("utf-8", errors="replace")

# ---- ① 工具说明书（会发给模型看）----
DESCRIPTION = {
    "name": "exec_command",
    "description": (
        "在工作区目录里执行一条 Windows 命令（cmd 语法），返回退出码和输出。"
        "适合：运行 Python 脚本、查看版本/环境、git 操作、批量处理文件。"
        "命令默认在工作区目录下执行；请勿操作工作区之外的文件。"
        f"有 {TIMEOUT_SECONDS} 秒超时，输出过长会被截断。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的完整命令（Windows cmd 语法，如 python xxx.py、git status、dir）",
            },
        },
        "required": ["command"],
    },
}


# ---- ② 执行函数（模型点名时真正运行）----
def run(arguments):
    command = (arguments.get("command") or "").strip()
    if not command:
        return "错误：command 不能为空"

    # 安全策略（防手滑）：明显毁灭性的命令直接拒绝
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, command):
            return (
                "错误：命令被安全策略拦截（疑似危险操作）。"
                "如果确实需要，请手动在终端里执行。"
            )

    # 工作目录 = 当前对话的工作区（和文件工具是同一个"干活范围"）；
    # 没设工作区的会话（老对话）→ 回退到项目目录，和 safe_path 的兜底一致
    workspace = toolbox.current_workspace()
    cwd = workspace or str(toolbox.PROJECT_ROOT)

    try:
        completed = subprocess.run(
            command,
            shell=True,                     # 用系统 shell（Windows 上是 cmd.exe）
            cwd=cwd,                        # 默认就在工作区里跑
            stdin=subprocess.DEVNULL,       # 不给输入：等待输入的命令会直接失败，而不是把对话挂死
            capture_output=True,            # 先拿原始字节，解码交给 _decode()（见上）
            timeout=TIMEOUT_SECONDS,        # 超时强制结束（subprocess.run 会杀掉子进程）
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # 不弹黑窗口
        )
    except subprocess.TimeoutExpired:
        return (
            f"错误：命令超时（超过 {TIMEOUT_SECONDS} 秒被强制结束）。"
            "长任务请分段执行，或写成脚本再跑。"
        )
    except Exception as e:
        return f"错误：命令执行失败：{e}"

    # 标准输出 + 标准错误一起返回（模型要看到报错才能自己修）
    stdout = _decode(completed.stdout)
    stderr = _decode(completed.stderr)
    parts = []
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append("[stderr]\n" + stderr)
    output = "\n".join(parts).strip()

    # 截断保护（先记原文长度，再截）
    note = ""
    if len(output) > MAX_OUTPUT_CHARS:
        note = f"\n…（输出太长已截断，原文共 {len(output)} 字符）"
        output = output[:MAX_OUTPUT_CHARS]

    header = f"命令：{command}\n退出码：{completed.returncode}"
    if not output:
        return header + "\n（没有输出）"
    return header + "\n\n" + output + note


# ---- ③ 登记进注册表 ----
# （总开关关掉时就不登记——模型连这个工具都看不到）
if config.ENABLE_EXEC:
    register(DESCRIPTION, run)
