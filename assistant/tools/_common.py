"""
tools/_common.py —— 文件工具们共用的小帮手

（说明：下划线开头的文件不会被当成"工具"加载，它只是内部模块，
  给 list_dir / read_file / write_file / edit_file 这些工具共用。）
"""

from pathlib import Path

import toolbox

# 默认工作区 = 项目根目录（tools/ 的上一级）。
# 如果某个对话没有设定自己的工作区，工具就在这个目录里干活。
DEFAULT_WORKSPACE = Path(__file__).resolve().parent.parent


def safe_path(user_path):
    """把模型给的路径转成真实路径，并检查它没有"越狱"。

    "被关在圈里"的"圈"是当前对话的**工作区**：
      - 没设定过 → 默认项目目录
      - 设定过   → 用户指定的目录（含新建的沙箱）
    工作区由 toolbox 的"线程本地小本本"记录，engine 每次对话开始时写入。

    两层检查：
      ① 工作区本身要合规（防手滑黑名单——比如盘符根目录）；
      ② 拼出来的真实路径必须还在工作区里面（防 ../ 越界）。
    报错信息会作为工具结果还给模型，它看到后可以自己换个路径重试。
    """
    # 1. 取当前工作区（没设置就用默认的项目目录）
    root_value = toolbox.current_workspace()
    root = Path(root_value).resolve() if root_value else DEFAULT_WORKSPACE

    # 2. 工作区合规检查（兜底防线：就算设置环节被绕过，这里也拦着）
    reason = toolbox.dangerous_reason(root)
    if reason:
        raise ValueError(f"当前工作区不合规（{reason}），所有文件操作已暂停")

    # 3. 不传路径就当作工作区根目录（比如 list_dir 不带参数时）
    candidate = root / (user_path or ".")

    # resolve() 会把 ".."、"." 和符号链接都算清楚，得到最终的真实路径
    real = candidate.resolve()

    # 4. ★ 安全检查：必须落在工作区里面，否则拒了
    if toolbox.full_access():
        # 用户在前端拨开了「完整权限」：不再限制在工作区内。
        # 但仍然拒绝盘符根目录 / 系统目录这类"一写就出大事"的位置——
        # 放开的是"范围"，不是"保险"。这两个不是一回事。
        reason = toolbox.dangerous_reason(real)
        if reason:
            raise ValueError(f"目标路径不合规（{reason}）：{user_path}")
        return real

    if not real.is_relative_to(root):
        raise ValueError(f"路径越界：「{user_path}」超出了工作区范围")

    return real
