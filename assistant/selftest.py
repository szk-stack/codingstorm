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
    "create_task",
    "get_project_context",
    "list_projects",
    "list_tasks",
}
# 这些是原 agent 的能力，助手里必须一个都没有
FORBIDDEN = {
    "exec", "run_command", "write_file", "read_file", "list_dir",
    "spawn_agent", "video", "bili", "screenshot", "recall",
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
    print("✓ 工具白名单正确（%d 个，只读 + 建任务）" % len(names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
