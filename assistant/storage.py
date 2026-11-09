"""
storage.py —— 数据保管员（本项目第三块积木）

★ 本次大改：跟 Pi（github.com/earendil-works/pi）学的"事件日志"存储思路。

以前的方式：把"最终的对话数据"整个存成一个 JSON 文件，每次改动全量重写。
现在的方式：把"发生的每一件事"一行一行追加到一个日志文件（data.jsonl），
           需要数据时，从头到尾把日志"回放"一遍，把状态推导出来。

这就是 Pi 的核心思路，一共三招：
  ① 追加写   —— 新记录只往文件末尾加，从不重写、从不修改旧行
  ② 事件日志 —— 每一行都是一条"事件"，用 type 字段标明这是什么事件
  ③ 回放推导 —— "当前状态"不是直接存的，是回放全部事件算出来的

为什么要这样做？
  - 写入更快：加一行 vs 重写整个文件
  - 更安全：写到一半断电，顶多丢最后一行，不会毁掉所有数据
  - 可追溯：历史完整保留，想审计、想统计都有据可查
"""

import json
from datetime import datetime
from pathlib import Path

import envconfig

# 事件日志文件（JSONL 格式：每行一条 JSON 记录）
# ★ 走 data_dir()：容器里用 MYWHEEL_DATA_DIR 指到挂进来的宿主机目录，
#   容器重建后会话记录还在。本机默认仍是项目根，行为完全不变。
LOG_FILE = envconfig.data_dir() / "data.jsonl"

# 上一版用的老文件。如果它还在、而新日志还不存在，就自动"搬家"（迁移）
OLD_JSON_FILE = envconfig.data_dir() / "data.json"


# ============================================================
# 写：往日志末尾追加事件
# ============================================================

def append_events(events):
    """把一批事件追加到日志文件末尾（一行一条）。

    参数 events 是一个列表，比如：
        [{"type": "message", "conversation_id": "c1", "role": "user", "content": "你好"}]
    """
    # 如果还在用老格式，先自动搬一次家（保证事件按正确顺序记录）
    _migrate_if_needed()
    _append_events_raw(events)


def _append_events_raw(events):
    """真正的追加写入（内部函数，不做迁移检查）。"""
    # open(..., "a") 里的 "a" 表示 append（追加）模式：
    # 文件原有内容一动不动，新内容只会写在最末尾——这就是"只追加"。
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        for event in events:
            # 后端统一给每条事件盖上时间戳（前端就不用操心了）
            event.setdefault("timestamp", datetime.now().isoformat(timespec="seconds"))
            # 一行一条：json.dumps 之后必须补一个换行符
            f.write(json.dumps(event, ensure_ascii=False) + "\n")


# ============================================================
# 读：把日志"回放"成当前状态
# ============================================================

def load_state():
    """回放整个日志，推导出当前状态。

    返回的结构和以前完全一样（所以前端毫无察觉）：
        {"conversations": [...], "current_id": ...}
    """
    # 第一次运行时日志还不存在，也可能需要从老的 data.json 搬家
    _migrate_if_needed()

    conversations = []   # 所有会话（最新的在最前面，和界面显示顺序一致）
    current_id = None    # 最近打开的是哪个会话

    for event in _read_all_events():
        etype = event.get("type")

        if etype == "session":
            # 新建会话：插到列表最前面
            conversations.insert(0, {
                "id": event["id"],
                "title": event.get("title", "新对话"),
                "messages": [],
                "workspace": None,    # 工作区（None = 默认项目目录）
                "compaction": None,   # 上下文压缩状态（摘要 + 保留边界）
            })

        elif etype == "message":
            # 新消息：找到它属于哪个会话，追加到那个会话的消息列表里
            conv = _find(conversations, event.get("conversation_id"))
            if conv is not None:
                conv["messages"].append({
                    "role": event["role"],
                    "content": event["content"],
                })

        elif etype == "tool":
            # 工具调用记录：作为一条 role=tool 的条目放进消息时间线。
            # 前端把它渲染成灰色小卡片（可回溯"agent 当时干了什么"）；
            # 发给模型前会转成"协议对"（见 engine._convert_tool_history）——
            # 模型因此"记得"自己读过什么，不用重复读文件。
            conv = _find(conversations, event.get("conversation_id"))
            if conv is not None:
                conv["messages"].append({
                    "role": "tool",
                    "name": event.get("name"),
                    "arguments": event.get("arguments"),
                    "result": event.get("result"),
                    "ok": event.get("ok", True),
                    # 附加数据（比如子代理的完整过程）；老记录没有这个字段，就是 None
                    "extra": event.get("extra"),
                })

        elif etype == "workspace":
            # 设定会话的工作区（agent 能在哪个目录里干活）
            # path 为 null 表示恢复默认（项目目录）
            conv = _find(conversations, event.get("conversation_id"))
            if conv is not None:
                conv["workspace"] = event.get("path")

        elif etype == "compact":
            # 上下文压缩记录：更新会话的压缩状态（最新一条覆盖旧的）
            # 被压缩的原始消息仍然完好地留在日志里，只是"发送时不再带上"
            conv = _find(conversations, event.get("conversation_id"))
            if conv is not None:
                conv["compaction"] = {
                    "summary": event.get("summary"),
                    "first_kept_index": event.get("first_kept_index", 0),
                }

        elif etype == "rename":
            # 会话改名
            conv = _find(conversations, event.get("id"))
            if conv is not None:
                conv["title"] = event["title"]

        elif etype == "delete":
            # 删除会话：从列表里过滤掉
            # （注意：日志里原来的消息行还在！只是"回放"时不再算数）
            conversations = [c for c in conversations if c["id"] != event.get("id")]

        elif etype == "switch":
            # 切换当前打开的会话
            current_id = event.get("id")

        # 不认识的类型？跳过就好
        # （这是事件日志的优点：以后加新事件类型，旧代码不会崩，天然向前兼容）

    return {"conversations": conversations, "current_id": current_id}


# ============================================================
# 下面都是内部小工具
# ============================================================

def _find(conversations, conv_id):
    """在会话列表里按 id 找会话，找不到返回 None。"""
    for c in conversations:
        if c["id"] == conv_id:
            return c
    return None


def _read_all_events():
    """把日志文件里的每一行都读出来，变成一串事件。"""
    if not LOG_FILE.exists():
        return []

    events = []
    with open(LOG_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue   # 跳过空行
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                # 某一行坏了就跳过这一行，不让整个文件读不出来
                # （真实系统还会做校验和、备份恢复，这里从简）
                continue
    return events


def _migrate_if_needed():
    """自动搬家：把老版 data.json 里的数据转成事件，写进新日志。

    就像 Pi 加载旧版本会话文件时会自动升级格式一样。
    只会在"新日志不存在、而老文件存在"时执行一次。
    """
    if LOG_FILE.exists():
        return                      # 已经有新日志了，不用搬家
    if not OLD_JSON_FILE.exists():
        return                      # 全新开始，没有老数据

    old = json.loads(OLD_JSON_FILE.read_text(encoding="utf-8"))

    events = []
    # 老文件里的会话顺序是"新的在前"，写事件时要从"老的先写"。
    # 因为回放时 session 事件会插到最前面，这样顺序才对得上。
    for conv in reversed(old.get("conversations", [])):
        events.append({
            "type": "session",
            "id": conv["id"],
            "title": conv.get("title", "新对话"),
        })
        for msg in conv.get("messages", []):
            events.append({
                "type": "message",
                "conversation_id": conv["id"],
                "role": msg["role"],
                "content": msg["content"],
            })

    if old.get("current_id"):
        events.append({"type": "switch", "id": old["current_id"]})

    if events:
        _append_events_raw(events)

    # 老文件改名成 data.json.bak 留作备份（不删，万一想回去看）
    try:
        OLD_JSON_FILE.rename(OLD_JSON_FILE.with_suffix(".json.bak"))
    except OSError:
        pass   # 改名失败也没关系：下次不会再迁移（日志已存在），老文件留着就留着
