"""助手自检：工具白名单是否完整。

tools/ 是「按文件名自动加载」的，某个文件忘了写末尾的 register(...) 时，
模块照样 import 成功、日志照样打印「已加载」，但模型根本看不到那个工具 ——
这个坑很安静，所以单独留一个检查。改动 tools/ 后跑一次：

    python selftest.py
"""

from __future__ import annotations

import sys

import toolbox

EXPECTED = {
    # codingstorm 平台工具：把需求整理成任务、立即入队
    "create_task",
    "get_project_context",
    "list_projects",
    "list_tasks",
    # 本地工作区工具：读写文件 / 执行命令 / 看图截图，全部受工作区边界约束
    "list_dir",
    "read_file",
    "write_file",
    "edit_file",
    "exec_command",
    "view_image",
    "screenshot",
}
# 这些原 agent 的能力**仍未还原**，一个都不该出现。
# 注意别把上面已还原的名字误列进来，否则自检会一直报红。
FORBIDDEN = {
    "spawn_agent", "search_kb", "bili_login", "recall",
    "video_search", "video_fetch", "video_batch", "video_transcribe",
}


def main() -> int:
    toolbox.load_tools()
    names = {d["function"]["name"] for d in toolbox.all_descriptions()}

    missing = EXPECTED - names
    leaked = FORBIDDEN & names
    extra = names - EXPECTED - FORBIDDEN

    print("发给模型的工具：", ", ".join(sorted(names)))
    if missing:
        print("✗ 缺少工具：", ", ".join(sorted(missing)), "（检查文件末尾的 register）")
    if leaked:
        print("✗ 出现了不该有的工具：", ", ".join(sorted(leaked)))
    if extra:
        print("! 额外工具：", ", ".join(sorted(extra)), "—— 确认是有意加的")
    if missing or leaked:
        return 1
    print("✓ 工具白名单正确（%d 个：4 个平台工具 + 7 个本地工具）" % len(names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
