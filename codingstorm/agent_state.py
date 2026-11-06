"""Phase 8 · 严格模式的状态机与 Agent 指令。

设计依据见 `docs/phase8-agent-layer.md`。要点：

- **状态在文件里，上下文在会话里。** `state.json` 是判断阶段的唯一依据，
  `claude --resume` 只负责接上下文。Claude Code 自带的自动重试和上下文压缩
  不可控，磁盘上的状态文件可控。
- **平台管权限，模型管推理。** 令牌、阶段合法性、产物存在性一律由服务端判定，
  不写进 prompt 求模型自觉。本模块只提供判定函数和指令文本。
- **阶段不等于模型调用次数。** 一次长会话内推进多个阶段，只在人工门处退出。

8a 的 Skill 先内置在本模块里（一个 `strict` Agent）；8b 再抽成
`contexts/<项目>/docs/agents/...` 下的可配置文件。
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
STATE_FILENAME = "state.json"
EVIDENCE_DIRNAME = "evidence"
BUILTIN_AGENT_ID = "bugfix"

# 人工门。只认精确字符串 —— 「继续」「可以」「按这个做」都不能解锁。
GATE_DEV_TOKEN = "【进入开发】"

# 阶段枚举。gate_dev 是挂起点，ship 是交付点。
STAGES = frozenset(
    {
        "init", "diagnose", "idea", "gate_dev", "explore", "implement",
        "review", "repair", "verify", "ship",
    }
)
GATE_STAGE = "gate_dev"
DELIVERY_STAGE = "ship"

# 挂起前必须有；交付前必须有。plan-delta.md 只在偏离已批准方案时才产生。
GATE_ARTIFACTS = ("diagnosis.md", "idea.md")
DELIVERY_ARTIFACTS = ("plan.md", "review-report.md", "ship-summary.md")
OPTIONAL_ARTIFACTS = ("findings.md", "plan-delta.md")
ALL_ARTIFACTS = GATE_ARTIFACTS + DELIVERY_ARTIFACTS + OPTIONAL_ARTIFACTS


class StateError(RuntimeError):
    """状态文件缺失、损坏或自相矛盾。调用方应把任务置为 failed，不要尝试修复。"""


def run_dir(root: Path, project_name: str, task_id: str) -> Path:
    return root / project_name / task_id


def ensure_run_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / EVIDENCE_DIRNAME).mkdir(exist_ok=True)
    return path


def state_path(run_path: Path) -> Path:
    return run_path / STATE_FILENAME


def new_state(task_id: str, agent_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "mode": "strict",
        "agent_id": agent_id,
        "stage": "init",
        "history": [],
        "artifacts": {},
        "review_rounds": 0,
        "waiting_token": None,
    }


def write_state(path: Path, state: dict[str, Any]) -> None:
    """原子写。Agent 可能同时在读，半截 JSON 会让它误判成状态错误。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_state(path: Path, *, task_id: str | None = None) -> dict[str, Any]:
    """读取并校验。缺文件 / 坏 JSON / 字段矛盾一律抛 StateError，不猜、不补。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StateError(f"状态文件读不到：{path}（{exc}）") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateError(f"状态文件不是合法 JSON：{path}（{exc}）") from exc
    if not isinstance(data, dict):
        raise StateError("状态文件顶层必须是对象")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise StateError(
            f"状态文件版本不匹配：期望 {SCHEMA_VERSION}，实际 {data.get('schema_version')!r}"
        )
    if task_id is not None and data.get("task_id") != task_id:
        raise StateError("状态文件属于另一条任务")
    if data.get("mode") != "strict":
        raise StateError("状态文件的 mode 不是 strict")
    if data.get("stage") not in STAGES:
        raise StateError(f"未知阶段：{data.get('stage')!r}")
    if not isinstance(data.get("history"), list):
        raise StateError("状态文件的 history 必须是数组")
    if not isinstance(data.get("artifacts"), dict):
        raise StateError("状态文件的 artifacts 必须是对象")
    rounds = data.get("review_rounds", 0)
    if not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 0:
        raise StateError("状态文件的 review_rounds 必须是非负整数")
    return data


def file_sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def collect_artifacts(run_path: Path, names: tuple[str, ...]) -> dict[str, str]:
    """算出产物哈希。缺一个就抛 —— 「聊天里写一句诊断完成」没有效力。"""
    out: dict[str, str] = {}
    for name in names:
        p = run_path / name
        if not p.is_file() or not p.read_text(encoding="utf-8", errors="replace").strip():
            raise StateError(f"阶段产物缺失或为空：{name}")
        out[name] = file_sha(p)
    return out


def validate_gate(run_path: Path, state: dict[str, Any], *, task_id: str) -> dict[str, str]:
    """执行 A 结束、准备挂起前的校验。"""
    if state["stage"] != GATE_STAGE:
        raise StateError(f"期望停在 {GATE_STAGE}，实际停在 {state['stage']}")
    if state.get("waiting_token") != GATE_DEV_TOKEN:
        raise StateError("状态文件没有声明等待【进入开发】")
    return collect_artifacts(run_path, GATE_ARTIFACTS)


def validate_delivery(run_path: Path, state: dict[str, Any], *, task_id: str) -> dict[str, str]:
    """执行 B 结束、准备 finalize 前的校验。"""
    if state["stage"] != DELIVERY_STAGE:
        raise StateError(f"期望停在 {DELIVERY_STAGE}，实际停在 {state['stage']}")
    return collect_artifacts(run_path, DELIVERY_ARTIFACTS)


def record_gate(state: dict[str, Any], *, token: str, approved_idea_sha: str) -> dict[str, Any]:
    """平台写授权记录。阶段迁移本身仍由 Agent 在续跑时按状态机完成。

    绑定 `idea.md` 的哈希，是为了事后能核对「当初批准的是哪个方案版本」。
    """
    state.setdefault("history", []).append(
        {
            "from": GATE_STAGE,
            "to": GATE_STAGE,
            "reason": "人工授权已由平台校验",
            "token": token,
            "approved_idea_sha": approved_idea_sha,
            "ts": datetime.now(UTC).isoformat(),
        }
    )
    state["authorized"] = True
    return state


def is_authorized(state: dict[str, Any]) -> bool:
    return any(
        isinstance(h, dict) and h.get("token") == GATE_DEV_TOKEN
        for h in state.get("history", [])
    )


def approved_idea_sha(state: dict[str, Any]) -> str | None:
    """取最后一次人工授权所批准的方案版本，供续跑指令引用。"""
    for h in reversed(state.get("history", [])):
        if isinstance(h, dict) and h.get("token") == GATE_DEV_TOKEN:
            return h.get("approved_idea_sha")
    return None


def describe(run_path: Path, state: dict[str, Any]) -> dict[str, Any]:
    """给 `/api/tasks/{id}/stage` 用的只读视图。"""
    artifacts = []
    for name in ALL_ARTIFACTS:
        p = run_path / name
        if p.is_file():
            artifacts.append({"name": name, "size": p.stat().st_size, "sha256": file_sha(p)})
    evidence = run_path / EVIDENCE_DIRNAME
    files = sorted(p.name for p in evidence.iterdir() if p.is_file()) if evidence.is_dir() else []
    return {
        "schema_version": state.get("schema_version"),
        "agent_id": state.get("agent_id"),
        "stage": state.get("stage"),
        "waiting_token": state.get("waiting_token") if state.get("stage") == GATE_STAGE else None,
        "authorized": is_authorized(state),
        "review_rounds": state.get("review_rounds", 0),
        "artifacts": artifacts,
        "evidence": files,
        "history": state.get("history", []),
        "state_path": str(state_path(run_path)),
    }


# ---------------------------------------------------------------------------
# 内置 bugfix Agent 指令
# ---------------------------------------------------------------------------

_ROLE_AND_AUTHORITY = """\
## 角色与边界

你只处理当前分配的这一条任务。不找别的任务、不创建替代任务、不调用其他 Agent。
**禁止跨阶段工作**：当前阶段该做什么就做什么，不许提前修改产品代码。

## 权威顺序（信息冲突时按这个顺序）

1. 本指令与人工决策
2. 仓库内的 AgentS.md / 文档 / 分支政策
3. 任务描述、已批准的方案、代码与运行证据

模型记得某个框架「通常」怎么配置，不能证明这个仓库也该这么配。
文档、代码、运行行为互相冲突时，把冲突暴露出来，不许挑一条最方便的解释继续走。
"""

_STATE_CONTRACT = """\
## 状态文件（唯一权威）

每次行动前重新读取 `<RUN_DIR>/state.json` 并校验；同一时间只能有一个活动阶段。
`stage` 只能取：init / diagnose / idea / gate_dev / explore / implement / review / repair / verify / ship。

每次合法的阶段变化都要往 `history` 追加一条：
`{"from": "...", "to": "...", "reason": "...", "files": {"<产物名>": "sha256:..."}}`。

状态文件缺失、JSON 损坏或字段互相矛盾时，**停止并如实报告状态错误**；
不许手工把阶段改到后面，也不许根据聊天内容补造一条已经丢失的批准记录。

`review_rounds` 记录净室评审轮数，最多 {max_review_rounds} 轮；超过仍有阻断问题就停下等人工。

**所有阶段产物只写进 `<RUN_DIR>`，绝对不要写进 worktree** —— 那是产品仓库，
收尾会被 `git add -A` 提交进去。测试输出等证据放 `<RUN_DIR>/evidence/`。
"""

_PHASE_A = """\
## 执行 A：诊断与方案（本次只做这一段）

1. `init`：确认任务编号、标题、描述，确认基线分支。
2. `diagnose`：**只复现问题、收集证据、找根因**。即使修法已经很明显也不许改代码。
   结果写进 `<RUN_DIR>/diagnosis.md`，写清复现步骤、根因、证据来源。
3. `idea`：起草解决方案，说明准备改什么、哪些行为保持不变、还有哪些问题需要人选择。
   写进 `<RUN_DIR>/idea.md`；涉及用户可见文案时给出多个候选说法。
4. 把 `state.json` 写成：`stage = "gate_dev"`、`waiting_token = "【进入开发】"`、
   `artifacts` 里登记 diagnosis.md 与 idea.md 的 sha256。
5. **正常结束本次执行。** 不要修改 worktree 内的任何产品文件，不要运行会写产品文件的命令，
   不要等待用户输入，不要尝试自己继续后面阶段。

回复里写明：实际完成的工作、当前阶段（gate_dev）、下一步需要的人工动作（发送【进入开发】）。
"""

_PHASE_B = """\
## 执行 B：实现、评审与交付（人工授权后）

1. `explore`：读相关文件、找调用关系、确认现有测试和仓库惯例，
   产出 `<RUN_DIR>/findings.md` 与 `<RUN_DIR>/plan.md`。
2. `implement`：按**已批准**的 idea.md 修改代码。计划外的变化记进 `<RUN_DIR>/plan-delta.md`。
   此时才允许修改 worktree 内的产品文件。
3. `review`：净室评审 —— 暂时放下实现者思路，用评审者标准重新检查：
   根因是否真的解决、调用方是否全部迁移、测试是否覆盖风险、有没有残留旧路径。
   结果写进 `<RUN_DIR>/review-report.md`。发现阻断问题则进入 `repair`，
   修完重新同步基线再评审，`review_rounds` 加一；超过 {max_review_rounds} 轮就停止自动流程。
4. `verify`：运行项目约定（pointer.md）里声明的测试命令，把输出存进 `<RUN_DIR>/evidence/`。
   **测试命令退出成功只证明通过了一部分**，还要检查与改动性质相符的真实行为。
   没有浏览器/预览环境就不要声称提供了截图或 Compare 链接。
5. `ship`：把交付信息写进 `<RUN_DIR>/ship-summary.md`（改了什么、怎么验的、证据清单、
   已知限制），把 `state.json` 的 `stage` 置为 `ship`、`waiting_token` 置为 null。

回复里写明：实际完成的工作、当前阶段（ship）、下一步需要的人工动作（审阅 diff 后批准）。
"""


def _render(template: str, run_path: Path, max_review_rounds: int) -> str:
    """替换占位符。

    **不用 `str.format`** —— 指令正文里有 JSON 示例，那些花括号会被当成格式字段。
    """
    return template.replace("<RUN_DIR>", str(run_path)).replace(
        "{max_review_rounds}", str(max_review_rounds)
    )


def build_initial_instruction(run_path: Path, *, max_review_rounds: int) -> str:
    """首次执行追加到项目 prompt 后面的 Agent 主指令。"""
    body = _ROLE_AND_AUTHORITY + "\n" + _STATE_CONTRACT + "\n" + _PHASE_A
    return "# 严格模式 · 缺陷修复 Agent\n\n" + _render(body, run_path, max_review_rounds)


def build_resume_instruction(
    run_path: Path,
    state: dict[str, Any],
    *,
    token: str,
    approved_idea_sha: str,
    max_review_rounds: int,
) -> str:
    """令牌通过后续跑时，平台包装出来发给模型的指令。

    `task_messages` 里存的仍是用户原文（界面显示人说了什么），这里给模型的是
    带完整上下文的续跑指令 —— `--resume` 只负责接会话，不能指望它记得规则。
    """
    header = (
        "# 人工授权已由平台校验\n\n"
        f"令牌：`{token}`；授权记录已写入 state.json 的 history。\n"
        f"当前阶段：`{state.get('stage')}` → `explore`。\n"
        f"已批准方案：`idea.md`（sha256=`{approved_idea_sha}`），不得偏离；"
        "偏离必须写进 plan-delta.md 并说明原因。\n"
        f"状态文件：`{state_path(run_path)}`（行动前必须重新读取并校验）。\n\n"
    )
    body = _ROLE_AND_AUTHORITY + "\n" + _STATE_CONTRACT + "\n" + _PHASE_B
    return header + _render(body, run_path, max_review_rounds)
