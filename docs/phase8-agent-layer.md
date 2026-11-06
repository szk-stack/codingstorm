# Phase 8 · Agent 指令层设计稿

> 状态：**8a 已实现（2026-11-05）；8b / 8c 仍是设计稿**。
> 实现落在 `codingstorm/agent_state.py`，其余改动见本文 §8 的逐文件清单。
> 前置阅读：[`architecture.md`](architecture.md)（尤其 §4.2 数据模型、§5 实现约束、§9 实施阶段）、
> [`context-design.md`](context-design.md)、[`phase0-report.md`](phase0-report.md)。
> 依据：HiFox 团队两篇 AI-native 实践（2026-10-29 全流程协作、2026-11-04「狄仁杰」修 bug 状态机）。

### 8a 实现与本文的三处偏差

1. **Skill 先内置在 `agent_state.py` 的指令模板里**，没有生成
   `contexts/<项目>/docs/agents/...` 文件。8a 只有一个 `bugfix` Agent，抽文件没有收益；
   8b 做配置化时再抽。
2. **多个 `--add-dir` 尚未在执行机上实测**（Phase 0 只验证过单个）。代码已按首选方案实现；
   上线前要在服务器跑一次 spike，不行就走 §4.2 的降级方案。
3. **history 连续性 8a 不逐条校验**。平台只校验阶段落点（`gate_dev` / `ship`）、
   必需产物、授权记录与 `idea.md` 哈希；阶段内部迁移的 history 由 Agent 自己写。
   8b 补逐条核对。

其余按本文实现：`awaiting_decision` 挂起、`state.json` 为唯一权威、只新增一道令牌门
（另一道复用 `approve`）、门 1 之前用 `Git.is_clean()` 事后检测、默认全关、
定时任务一律 `plain`。

---

## 1. 定位：补的是框架里的一层，不是另一个框架

两篇推文说的是同一个 HiFox：**第一篇是协作平台底座 + 全流程视角，第二篇是跑在这个底座上的单个 Agent 的指令设计**。狄仁杰第 1 步要确认「自己位于 HiFox 提供的任务工作区」、按任务优先级定基线分支，第 10 步要把 HiFox 任务改成待测试 —— 它整条流程都长在平台底座上。

codingstorm 的 Phase 0–7 已经把底座做得很完整：队列、worktree / Git 生命周期、上下文三层、IM 入口、定时任务、执行窗口、成本记账、安全边界。对照下来：

| HiFox | codingstorm 现状 | 结论 |
|---|---|---|
| 任务 / 分支 / 工作区 / commit 关联 | Phase 1–2 已实现，含 rebase 与幂等批准 | 已对齐 |
| 定时自动化、IM 集成、共享 Skill | Phase 6–7 + `contexts/<项目>/docs/` | 部分对齐 |
| **Agent 配置、指令、阶段状态机、人工门、证据链** | 没有「Agent」这个概念，一次 `claude -p` 跑到底 | **本阶段要补** |

所以 Phase 8 不是「再加一个框架」，而是把 **Agent 指令**变成底座上的一等公民。最重要的一个判断：

> **「单 Agent 还是多 Agent」在本项目里不是框架选型，是配置选型。**
> 先把「指令 + 阶段 Skill + 人工门 + 状态触发规则」做成可配置的，哪怕只有一个 Agent；
> 以后加测试 Agent 只是加一条配置，不需要引入多 Agent 编排。

---

## 2. 设计原则

1. **不破坏一条任务 = 一个分支 + 一个工作区 + 一份待审 diff。** 这是 §5.5 的地基，任何「自动化触发就新建一条任务」的做法都会让新任务切在旧主干上、看不到未合并改动（README「已知限制」里已经写明）。
2. **状态在文件里，上下文在会话里。** `state.json` 是判断阶段的唯一依据，不依赖聊天记忆；`claude --resume` 负责接上下文。这也正好对冲 Phase 0 实测到的 Claude Code 自动重试 / 上下文压缩 —— Runtime 自带行为不可控，但磁盘上的状态文件可控。
3. **平台管权限，模型管推理。** 令牌校验、阶段合法性、产物存在性一律由服务端判定，不写进 prompt 求模型自觉。
4. **默认全关。** 跟 `[scheduler.window].enabled = false` 一个道理：升个版本不能让现有任务突然开始等人点头。`mode=plain` 时所有新逻辑短路。
5. **不换 Runtime、不引多 Agent、不做自动批准。** 推文里换 oh-my-pi 是因为他们的规模；本项目换 Runtime 等于推翻 Phase 0–5 的全部实测结论。

---

## 3. 核心抽象：决定「下一次执行什么」

现在 `scheduler._run()` 里 prompt 的来源散在两处（`build_prompt()` / `messages[-1].text`），本质是同一个问题：**这一轮给模型发什么指令**。把它抽成一个显式对象：

```python
@dataclass(frozen=True)
class RunRequest:
    prompt: str            # 实际发给 claude -p 的文本
    session_id: str
    resume: bool
    agent_id: str | None  # None = plain 模式
    stage: str | None     # strict 模式当前阶段
    state_path: Path | None
    context_dirs: list[Path]
```

触发源共四种，统一收敛到同一个构造器：

| 触发源 | 现在的实现 | Phase 8 |
|---|---|---|
| 用户提交任务 | `contexts.build_prompt()` | 不变，或按 `mode` 追加 Agent 主指令 |
| 用户追加消息 | `messages[-1].text` + `--resume` | 不变（plain）；strict 下包装成「阶段续跑指令」 |
| **人工门令牌** | 不存在 | 新：**同一条任务**的下一次 attempt |
| **自动化规则** | 不存在 | 新：状态变化 → 往**同一条任务**注入指令 |

**关键推论**：HiFox 的「自动化」是「任务状态变化触发某个 Agent 执行某条指令」，任务还是那条任务。落到 codingstorm 就必须翻译成「同一条任务的下一次 resume」，**不能新建任务**——否则违反原则 1。这样「Agent 配置」和「狄仁杰状态机」在架构上是同一件事：都在决定 `RunRequest`。

---

## 4. 数据模型与运行目录

### 4.1 DB 变更

沿用 `db._migrate()` 的 `PRAGMA table_info` + `ALTER TABLE` 方式（`CREATE TABLE IF NOT EXISTS` 对老库无效）。

```sql
-- 8a：tasks 追加四列
ALTER TABLE tasks ADD COLUMN mode       TEXT;  -- 'plain'(默认) | 'strict'
ALTER TABLE tasks ADD COLUMN agent_id   TEXT;  -- 8a 固定为内置 'bugfix'
ALTER TABLE tasks ADD COLUMN stage      TEXT;  -- strict 模式下的阶段，见 §6
ALTER TABLE tasks ADD COLUMN state_path TEXT;  -- 运行目录绝对路径
```

`TaskOut` / `TASK_FIELDS` 同步加这四个字段；`TaskKind` **仍然只是标签**（`models.py:23` 的注释保留），是否走严格模式由 `mode` 决定，不由 `kind` 决定。

```sql
-- 8b：Agent 配置与自动化规则（一等公民）
CREATE TABLE agent_profiles (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    instruction  TEXT,          -- 主指令文件路径（相对 agents 目录）
    skills       TEXT,          -- JSON：{stage: skill文件路径}
    gates        TEXT,          -- JSON：门定义
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL
);

CREATE TABLE automations (
    id             TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    trigger_status TEXT NOT NULL,   -- 'awaiting_review' / 'failed' / ...
    kind_filter    TEXT,            -- JSON 数组，NULL = 不限
    agent_id       TEXT,
    instruction    TEXT NOT NULL,   -- 触发时注入的指令模板
    enabled        INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL
);
```

Agent 的指令正文与阶段 Skill **存文件不存 DB**：`{root}/contexts/<项目>/docs/agents/<agent-id>/`，直接复用现有文档索引与 Web 编辑器 —— 这就是推文里「共享 Skill」的落点，不用另建一套管理界面。

### 4.2 阶段产物放哪：必须在 worktree 之外

`workspace.finalize()` 里是 `commit_all()` → **`git add -A`**（`workspace.py:314`）。Phase 2 已经踩过一次：没有 `.gitignore` 时 AI 跑测试产生的 `__pycache__` 被一起提交进 diff（`architecture.md` §9 Phase 2）。把 `state.json` / `diagnosis.md` 等放进 worktree，会原样复现这个坑，而且这次污染物是「审查时会看到的假文件」。

```text
{root}/runs/<项目>/<任务id>/
  state.json
  diagnosis.md  idea.md  findings.md  plan.md  plan-delta.md
  review-report.md  ship-summary.md
  evidence/                     截图、测试输出等
```

理由：① 不进产品仓库、不进 review diff；② 不碰被调度仓库的 `.gitignore`（沿用 §7「上下文目录放 worktree 之外」的同一原则）；③ 平台自己可以直接读写，不必绕 git。

需要给 Agent 授权读这个目录：

- **首选**：`Runner.build_command()` 的 `context_dir: Path | None` 改成 `context_dirs: Sequence[Path]`，每个目录各发一次 `--add-dir`。Phase 0 只实测过单个 `--add-dir`（§4），**多个要在执行机上先跑一次 spike 确认**。
- **降级**（若实测不支持多个）：把运行目录挪到 `{root}/contexts/<项目>/runs/<任务id>/`，共用现有 `--add-dir`。它不在 `docs/` 下，不会被 `ContextStore.list_docs()` 索引，唯一代价是语义上混进了上下文目录。

### 4.3 `state.json` 契约

```json
{
  "schema_version": 1,
  "task_id": "…",
  "mode": "strict",
  "stage": "gate_dev",
  "history": [
    {"from": "init", "to": "diagnose", "reason": "…", "files": {"diagnosis.md": "sha256:…"}},
    {"from": "gate_dev", "to": "explore", "token": "【进入开发】",
     "approved_idea_sha": "sha256:…", "ts": "…"}
  ],
  "artifacts": {"diagnosis.md": "sha256:…", "idea.md": "sha256:…"},
  "review_rounds": 0,
  "waiting_token": "【进入开发】"
}
```

规则照抄推文里那套「持久化状态机」，但判定权归平台：

- `stage` 是唯一依据；每次行动前读取并校验，**同一时间只能有一个活动阶段**。
- **权威顺序**：Agent 主指令 + 人工决策 > 仓库内 `AgentS.md` / 文档 / 分支政策 > 任务描述 / 批准方案 / 代码与运行证据。文档与代码冲突时要暴露出来，不许挑最方便的解释继续走。
- 文件缺失、JSON 损坏、字段自相矛盾 → **停止并置 `failed`，报「状态错误」**。不许从聊天内容补造一条丢失的批准记录，也不许手工把阶段改到后面。
- 授权门（令牌）的 history 由**平台**写；阶段内部迁移由 Agent 写；平台在挂起/交付时校验 history 连续性。

---

## 5. 任务状态机扩展

新增一个状态 `awaiting_decision`（待决策）：

```text
queued ──► running ──┬──► awaiting_review ──► merged / discarded   ← plain（现状）
                     │           ▲
                     │           │ 执行 B：实现/评审/验证 → finalize
                     └──► awaiting_decision                        ← strict 执行 A 结束
                                  │
                                  └── 令牌通过 ──► queued（resume 同一会话）──► 执行 B
```

逐项核对现有代码，改动比想象中小：

| 位置 | 要改什么 | 为什么 |
|---|---|---|
| `models.TaskStatus` | 加 `AWAITING_DECISION`；加进 `ACTIONABLE_STATUSES` | 前端/CLI 据此判断可交互 |
| `store.set_status()` (`store.py:176`) | **不要**把新状态加进写 `finished_at` 的集合 | 挂起不是完成；写了会污染时长统计 |
| `store.claim_next()` (`store.py:143`) | **不用改** | 只领 `queued`，`NOT EXISTS` 只排 `running`；任务一挂起，`_run()` 返回 → `_running` 弹出 → **并发位天然释放** |
| `scheduler.recover()` | **不用改** | 只处理 `running` 孤儿；挂起态没有活进程，重启后靠磁盘 `state.json` 原样续跑 |
| `store.requeue()` (`store.py:189`) | **不用改** | 已经会清 `started_at` / `finished_at` / `error_text` |
| `api.approve_task()` | **不用改** | 仍只放行 `awaiting_review` |
| `api.discard_task()` (`api.py:336`) | 放行集合加 `awaiting_decision` | 挂起的任务也要能丢，否则永远卡着 |
| `api.add_task_message()` (`api.py:215`) | 见 §6 | 挂起态只接受令牌 |

---

## 6. 人工门：本项目只需要新增一道

推文里狄仁杰有两道门：`【进入开发】`（解锁写产品代码）和 `【提测】`（解锁推送 / 改任务状态）。**照搬到本项目会多出一道多余的门**，因为终点那道门已经存在而且更强：`cs approve` 是不可逆的 `update-ref` 快进合并。

所以严格模式的人工介入点仍是 **2 个**：

| # | 门 | 控制什么 | 实现 |
|---|---|---|---|
| 1 | `【进入开发】`（新增） | 允许修改产品代码 | `awaiting_decision` 下由服务端校验令牌 |
| 2 | `approve`（复用，强化） | 允许合入主干 | 现有流程 + 校验 `ship-summary.md` 等产物齐全 |

这正好对上推文「人工门从 7 道减到 2 道」的经验，也没让「提交完就能走」退化成「中途回来点三次」。

**令牌校验**：

- 位置在 `POST /api/tasks/{id}/messages` 与 `store`，**不在 prompt**。
- `awaiting_decision` 状态下只接受精确字符串 `【进入开发】`；`继续` / `可以` / `按这个做` / 带空格的变体一律 **409**。推文明确说过自然语言会随上下文漂移，把客套话当批准是最典型的翻车方式。
- 校验通过才 `add_message()` + `requeue()`；授权记录（令牌、`idea.md` 的 sha256、时间）写进 `state.json` 的 history。
- 非令牌消息在挂起态一律拒绝，v1 不提供「挂起中讨论」通道 —— 想讨论就先丢弃再开新任务，保持简单。

**续跑指令由平台包装**：`task_messages` 里存用户原文（界面显示人说了什么），`scheduler._run()` 在 strict 模式下不直接把 `messages[-1].text` 发给模型（`scheduler.py:368`），而是拼成完整指令：

```text
【人工授权已由平台校验】令牌：进入开发
当前阶段：gate_dev → explore
状态文件：<runs_dir>/state.json（行动前必须重新读取并校验）
本阶段必读 Skill：<contexts>/<项目>/docs/agents/bugfix/skills/explore.md（完整读取）
已批准方案：idea.md，sha256=…（不得偏离；偏离写进 plan-delta.md）
运行目录：<runs_dir>（产物写这里，不要写进 worktree）
从现在起才允许修改 worktree 内的产品文件。
```

`--resume` 接的是执行 A 的会话；`store.last_session_id()` 已经按 `origin='task'` 过滤掉了沉淀那次调用（`store.py:380` 注释里记着这个坑），strict 模式下更要保持。**不要让沉淀混进阶段历史**。

### 「不许提前改代码」怎么强制

推文靠指令约束，但 `bypassPermissions` 下最可靠的是 Phase 0 已验证的 **PreToolUse hook**（`architecture.md` §3.3）。分两步走：

- **8a（检测优先）**：执行 A 结束准备挂起时，平台检查 worktree 是否有产品代码改动；有 → 直接 `failed` 并报告「门 1 之前不得修改产品代码」。禁止不一定拦得住，**检测一定拦得住**。
- **8b（硬拦截）**：扩展 `guard.py`，hook 读 `state.json`，在 `stage < gate_dev` 时阻断 `Write` / `Edit` 以及写向 worktree 的 Bash，同时放行写运行目录。这是真正的强制，但规则较复杂，放到 8b。注意 `guard.py:85` 目前只注册了 `Bash` / `WebFetch` 两个 matcher，8b 要补上 `Write` / `Edit`。

---

## 7. 阶段、产物与 Skill

阶段名对齐推文的 10 步，但**阶段不等于模型调用次数** —— 这点很要紧。狄仁杰实际案例是「14 分钟诊断+方案，停下来等人；37 分钟实现+验证，停下来等人」，阶段是在**一次长会话内**推进的。本项目同理：

| 执行 | 推进的阶段 | 结束在哪 | 产物 |
|---|---|---|---|
| **A** | init → diagnose → idea | `gate_dev`（正常退出，`result.success`） | `diagnosis.md`、`idea.md` |
| **B** | explore → implement → review / repair（≤6 轮）→ verify → ship | 正常 `finalize()` → `awaiting_review` | `findings.md`、`plan.md`、`plan-delta.md`、`review-report.md`、`ship-summary.md` |

所以严格模式只比现在多 **1 次主模型调用**（A 和 B，各带 `--resume`），不是 10 次；多出来的就是一次调用的固定开销（Phase 0 实测约 2.6 万 input token 量级），沉淀仍只在交付时跑一次。**10 次调用在这个成本结构下完全不可行**，这也是不照搬「每阶段一个 Agent」的硬理由。

`scheduler._run()` 在 `outcome.ok` 之后分支（`scheduler.py:447` 附近）：

- `mode == plain` → 走现有 `_finalize_success()`，一行不改。
- `mode == strict` → 读 `state.json`：
  - `stage == gate_dev` → 校验 `diagnosis.md` / `idea.md` 存在 + 哈希一致 + worktree 无产品改动 → `set_status(AWAITING_DECISION)`；**不 finalize、不 sediment**。
  - `stage == ship` → 校验 `review-report.md` / `ship-summary.md` 齐全 → 走现有 `_finalize_success()`（提交、rebase、待审、沉淀）。
  - 其他阶段 / 状态文件异常 → `failed`，`error_text` 写明状态错误。

**阶段产物是硬约束**：缺对应文件不许宣告阶段完成，聊天里写一句「诊断完成」无效。Skill 模板放 `contexts/<项目>/docs/agents/bugfix/skills/`，进入新阶段前完整读取对应 Skill 并把读取记录写进 history；仅记「已读」不够，产物格式要符合 Skill 规定。

**净室评审**：`review` 发现阻断问题 → `repair` → 重新同步基线 → 再评审，最多 6 轮；第 6 轮仍有阻断问题就停止自动流程、置 `failed` 等人工（防止自己写、自己审、自己修的死循环）。

**验证与证据**：测试命令一律取自 `pointer.md`（项目约定层），不许模型自己猜。指令里写明「测试命令退出成功只证明通过了一部分」。推文要求 UI 改动必须截图并贴进会话 —— **8a 做不到，也不假装做得到**：本项目没有浏览器、预览链接或共享测试机，那需要 8c 的独立前置建设。8a 只要求把测试输出 / 命令结果写进 `evidence/` 并在 `ship-summary.md` 里列出。

**关于「提测包」**：推文里是 PR + Compare 链接 + 截图。本项目 `guard_block_push = true` 刻意拦 `git push`，没有远端 PR。对应物改成「diff + `ship-summary.md` + 产物清单」，由现有 `/api/tasks/{id}/diff` 呈现。不要在指令里让模型生成它做不出来的 Compare 链接。

---

## 8. 配置、API、CLI、界面

```toml
[agents]
enabled        = false        # 总开关；默认关，升级不惊动现有任务
strict_kinds   = []           # 哪些 kind 自动走 strict；默认空 = 全靠显式 --mode
max_review_rounds = 6
```

`Config` 加 `AgentsConfig` 与 `runs_dir` property。`strict_kinds = ["bug"]` 是可选的便利项，`kind` 本身不改变执行流程的语义保持不变。

| 层 | 改动 |
|---|---|
| `config.py` | 加 `AgentsConfig`、`Config.runs_dir` |
| `models.py` | `TaskStatus.AWAITING_DECISION`、`ACTIONABLE_STATUSES`、`TaskOut` 四字段、`TaskCreate.mode` |
| `db.py` | `_ADDED_COLUMNS` 加 4 列；8b 加两张表 |
| `store.py` | `set_status` 的 finished_at 集合保持不含新状态；`claim_next` / `requeue` / `recover` 不动 |
| `runner.py` | `build_command` 改多 `--add-dir`；`RunOutcome` 不需要改 |
| `scheduler.py` | 抽 `RunRequest` 构造器；`_run()` 成功分支按 `mode` 分流；strict 续跑指令包装；新增 `_validate_state()` |
| `api.py` | `add_task_message` 挂起态令牌校验；`discard_task` 放行新状态；新增 `GET /api/tasks/{id}/stage` |
| `cli.py` | `cs submit --mode strict`；新增 `cs stage <id>`（阶段 / 等待令牌 / 产物清单）；`cs say <id> "【进入开发】"` 复用现有命令 |
| `static/app.js` | `STATUS_LABEL` 加「待决策」；`FOLLOWABLE` 加挂起态但输入框只收令牌；任务详情加「阶段」页签（state + 产物 + 一键发令牌按钮） |
| `guard.py` | 8a 不动；8b 加 stage gate hook |
| `workspace.py` | **不动**（产物在 worktree 之外，`finalize()` 保持现状） |
| `README.md` / `docs/hermes-skill.md` | 功能落地后再写；**设计稿阶段不改**，否则消息网关会去用一个还不存在的 `--mode` |

---

## 9. 分期

**8a · 最小闭环 ✅ 已实现（2026-11-05）**

内置一个 `bugfix` Agent、两道人工点（令牌 + approve）、`state.json`、6 份阶段产物、`awaiting_decision`。不加 `agent_profiles` / `automations` 两张表，不加多 Agent，不加截图证据。

验收：提交一条 `--mode strict` 的 bug → 诊断/方案后停在「待决策」，并发位释放 → `cs say <id> "【进入开发】"` → resume 同一会话完成实现与验证 → 待审 → `approve` 合入，主干历史保持线性；plain 任务的既有流程全量回归通过。

两条边界要一起验：**定时任务生成的任务一律 `plain`**（`ScheduleCreate` 不加 `mode`），否则周期任务会挂在门上等人，与 Phase 7「不做自动批准」同理；界面在挂起态不展示「改动」页签 —— 此时还没有 `commit_sha`，diff 端点拿不到东西。

**8b · 配置化**

抽 `agent_profiles` + `automations`，让「状态变化 → 往同一条任务注入指令」可配置；PreToolUse hook 做门 1 硬拦截。此时才谈得上「加一个测试 Agent 只是加一条配置」。

**8c · 证据链**

浏览器 / 预览环境、截图贴进会话、共享测试机、多个专职 Agent。依赖服务器资源（约 2.3G 可用内存、并发 2），**当前不做**。

---

## 10. 验证方式

- 状态机：合法迁移放行；跳阶段、`stage` 缺失 / JSON 损坏 / 字段矛盾一律 `failed` 且原因明确
- 令牌：`【进入开发】` 通过；`继续` / `可以` / 带空格变体 / 大小写变体一律 409
- 授权记录绑定 `idea.md` 的 sha256；方案被改动后放行应被拒
- 门 1 之前 worktree 有产品改动 → `failed`（8a 检测路径）
- 挂起时 `_running` 已弹出，同项目下一条任务能被领取；重启后挂起任务仍在、state 完好
- `last_session_id()` 仍返回执行 A 的会话，不串到沉淀那次
- 阶段产物**不出现在** `/api/tasks/{id}/diff` 里
- 净室评审第 7 轮自动停止
- `mode=plain` 的端到端回归（提交 → 审 diff → 批准 → 主干前进）零变化
- 执行机 spike：多个 `--add-dir` 是否可用（8a 前置）

---

## 11. 风险与回退

| 风险 | 处理 |
|---|---|
| 两道人工点伤害「提交完就能走」 | 严格模式全靠显式 opt-in；默认 `enabled=false`，`strict_kinds=[]` |
| 成本上升 | 严格模式只多 1 次主调用；若实测不可接受，优先合并执行 A 的产物要求，不要让阶段数变成调用数 |
| Claude Code 自动重试 / 上下文压缩干扰状态机 | `state.json` 是唯一权威，`resume` 只用于接上下文；保留 `--disallowed-tools AskUserQuestion`、`stdin=DEVNULL`、`result.subtype` 判据 |
| `--resume` 会话随阶段膨胀 | 每次 attempt 仍受 `task.max_turns=50` 与墙钟上限约束；`attempts` 表按次记账，可观测 |
| 产物污染 diff | 运行目录在 worktree 之外；测试要专门覆盖这一条 |
| 整体回退 | 一个开关：`[agents].enabled = false`。`mode=plain` 时所有新代码路径短路，删掉四列不影响旧行为 |

---

## 12. 刻意不做

多 Agent 小队、IM 结果双向回推、共享测试机、自动批准、更换 Runtime、分阶段计价。前四项依赖多人团队与服务器资源，第五项会推翻 Phase 0–5 的实测结论，第六项在 Phase 7 已经论证过收益极低。

推文里那句「核心不是某个单一工具，而是把开发流程重新设计成 AI 可以参与的形式」是对的 —— 落到本项目，就是把「下一次执行什么」从一个隐含判断，变成底座上一个可配置、可校验、可回放的对象。
