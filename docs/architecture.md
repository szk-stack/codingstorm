# codingstorm 架构设计

> 定稿日期：2026-09-17
> 本文是 codingstorm 的完整架构设计，含已核实的技术契约、压测出的实现约束、以及分期实施路径。
> 配套阅读：`context-design.md`（上下文子系统的设计依据）、`prior-art-vibe-kanban.md`（前车之鉴）。

## 1. 要解决的问题

目前用 AI 编码时，必须等它跑完才能下新指令，人被迫同步等待。

**做法**：在服务器上跑一个常驻平台，按项目隔离地接收需求 / 指令 / bug，入队后由服务器上的 Claude Code 顺序执行；人随时可看执行过程和结果，事后审 diff 决定合并或丢弃。把「同步等待」变成「异步投递 + 事后审批」。

## 2. 已确认的设计决策

1. **并发**：按项目串行、跨项目并行，受全局并发上限约束
2. **权限**：全自动放行 + 边界拦截（`git push`、网络出口）
3. **交付**：每任务从「最近一次已合并的主干」切出，批准即合并
4. **会话**：每任务独立会话，不 resume
5. **上下文**：三层文档机制（指针图 / 文档索引 / 自动沉淀），不做 RAG
6. **仓库归属**：服务器上的克隆由 codingstorm 独占，用户本地开发、通过 git remote 同步

---

## 3. 技术契约（已核实）

来源：本地 `claude --help`（2.1.274）+ 服务器实测（2.1.220）+ 官方文档调研。

### 3.1 无头模式调用

```
claude -p <prompt>
  --output-format stream-json --verbose --include-partial-messages
  --session-id <uuid>
  --max-turns <N>                    ← 必须有，否则死循环 agent 能烧一整晚
  --permission-mode bypassPermissions
  --add-dir <context_dir>
```

cwd = 任务的工作目录（worktree）。`-p --output-format stream-json` 需要配 `--verbose`。

### 3.2 输出是 NDJSON，每行一个事件

| type | 内容 |
|---|---|
| `system` / `subtype:"init"` | `session_id`、`cwd`、`model`、`tools[]`、`permissionMode`、`uuid` |
| `assistant` | `message` 是完整的 Anthropic Message（含 `content[]`、`usage`）。`content[]` 元素为 `{type:"text"}` 或 `{type:"tool_use", id, name, input}` |
| `user` | 工具结果回流，`content[]` 含 `{type:"tool_result", tool_use_id, content, is_error}` |
| `stream_event` | 开 `--include-partial-messages` 后才有。原始 SSE，`content_block_delta` → `delta.text` 用于逐字渲染 |
| `result` | `subtype`(`success`/`error_max_turns`/`error_during_execution`)、`is_error`、`result`(最终文本)、`duration_ms`、`num_turns`、`total_cost_usd`、`usage`、`permission_denials` |

**退出码粒度粗**（0 成功 / 1 失败 / 130 SIGINT / 143 SIGTERM），**权威判据是 `result` 事件的 `subtype` + `is_error`**。

### 3.3 边界拦截

`--permission-mode bypassPermissions` 会绕过所有权限检查，所以 `permissions.deny` 规则不可靠。用 **PreToolUse hook**（独立机制）：

```json
{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"<拦截脚本>"}]}]}}
```

hook 从 stdin 收 JSON（`tool_name`、`tool_input`、`cwd`、`session_id`），**退出码 2 = 阻断**并把 stderr 回喂给模型。

> **Phase 0 必须实测**：`bypassPermissions` 下 hook 会不会触发。若无效应改用 `--permission-prompt-tool`（把权限询问转成 MCP 工具调用，由我们的代码回 allow/deny）。

---

## 4. 架构

单进程 asyncio。FastAPI 提供 HTTP + WebSocket，调度器是 asyncio 后台任务。

```
   浏览器 ──→ FastAPI ──→ SQLite (WAL)
                │
                └─ Scheduler (asyncio)
                     │  每项目串行 · 全局 Semaphore(N)
                     ↓
                   Runner ──→ claude -p ──stdout──→ 每任务一个 NDJSON 日志文件
                     │                                      │
                     │                           tail 协程 ──┴──→ 语义事件落库 + WS
                     ├─ Git 操作（worktree / rebase / update-ref）
                     └─ Context（组装 prompt / 任务后沉淀）
```

### 4.1 目录结构

```
codingstorm/
  pyproject.toml
  codingstorm/
    config.py      配置加载（TOML）
    db.py          SQLite schema、连接管理、单写线程
    models.py      pydantic 模型
    git_ops.py     worktree / rebase / update-ref / diff / 对账
    runner.py      子进程管理 + NDJSON 解析 + 日志 tail
    scheduler.py   每项目串行队列 + 全局并发上限 + 看门狗
    context.py     三层上下文组装与沉淀
    api.py         FastAPI 路由 + WebSocket
    app.py         入口 + lifespan（收尾与对账）
    static/
  scripts/
    spike.py       Phase 0 验证脚本
    hooks/         边界拦截脚本
  tests/
  docs/
```

### 4.2 数据模型

```sql
projects(id, name, repo_path, target_branch, worktree_root, context_dir,
         max_concurrency, max_turns, wall_clock_timeout_s, created_at, enabled)

tasks(id, project_id, title, body, kind,        -- kind: requirement|instruction|bug
      status, priority,
      branch, worktree_path, base_commit_git,   -- 切出时的 target sha
      commit_sha,                                -- 任务产出（判"已合并"的锚点）
      merge_commit_sha,
      prompt_snapshot,                           -- 或 hash + 上下文文件版本
      permission_denials,                        -- 边界拦截审计
      last_event_at,                             -- 心跳，看门狗用
      created_at, started_at, finished_at)

attempts(id, task_id, attempt_no, session_id, pid, pid_starttime, model,
         exit_code, result_subtype, is_error, error_text, duration_ms, num_turns,
         input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
         cost_usd, price_version, raw_log_path,
         origin,                                    -- task | sediment
         started_at, finished_at)

task_events(task_id, seq, ts, type, payload)       -- 语义事件，WITHOUT ROWID
```

**为什么要有 `attempts` 表**：重试会覆盖 `session_id`；token 必须分次记（失败那次往往最贵）；回放和将来的 resume 全靠它。**沉淀那步自己也要调模型，用 `origin` 记**，否则每个任务的固定开销全漏。

**`kind` 只是标签**，不改变执行流程。

### 4.3 任务状态机

```
queued ──→ running ──→ awaiting_review ──→ merged
             │                │
             │                └──→ discarded
             ├──→ failed
             ├──→ interrupted     (平台优雅退出时)
             └──→ cancelled
```

---

## 5. 关键实现约束（来自架构压测）

这一节每条都是「不这么做就会出问题」，不是风格偏好。

### 5.1 子进程与日志

- **不要把 stdout 用 PIPE 逐行 `readline()`**：asyncio 的 StreamReader 默认 limit 是 64 KiB，超长行直接抛 `ValueError` 且不消费缓冲区，之后每行都炸。而 `Write` 整个文件、`Read`/`Bash` 的输出轻松超过 64 KiB —— 解析器会在长任务里随机崩溃。
- **改为把 stdout 重定向到每任务一个 NDJSON 日志文件，另起协程 tail 它**。一次解决行长、背压、崩溃丢日志三件事，而且这份原始日志正是回放和恢复所需。
- `stdin=DEVNULL`；`start_new_session=True` 建独立进程组（顺带覆盖 Claude 拉起的 bash/ripgrep）。

### 5.2 孤儿进程

平台被 SIGKILL/OOM 后，子进程不死，reparent 到 PID 1，**继续改 worktree、继续烧钱**。

启动时必须核对 pid + `/proc/<pid>/stat` 的 starttime（防 PID 复用）后**杀掉**，不能只标 failed。

### 5.3 优雅退出

- 不要用 `loop.add_signal_handler`（会和 uvicorn 抢），走 FastAPI lifespan shutdown
- 顺序：先把库里的状态写成 `interrupted`（否则重启后与崩溃无法区分）→ SIGTERM 进程组 → 超时 SIGKILL
- systemd 单元要设 **`KillMode=mixed`**：默认的 control-group 会同时给子进程发 SIGTERM，抢在收尾逻辑前面

### 5.4 超时与看门狗

- 每个任务有 `--max-turns` 和墙钟超时
- 靠 `last_event_at` 心跳；超墙钟或长时间无输出就杀
- UI 上要有强制失败按钮

### 5.5 分支与合并

**rebase 和 `--no-ff` 是矛盾的** —— rebase 完再 `merge --no-ff` 照样生成合并提交，历史依然分叉，白搭一个改写历史的失败模式。

采用**线性模型**：

- 任务从 target 当前 HEAD 切出，记 `base_commit_git`
- 任务完成时若 target 已前进 → 在 worktree 里 `git rebase <target>`；冲突则 `git rebase --abort` 复原再上报
- 审的 diff 用 `git diff <target>...<branch>` —— **就是将来真正合入的内容**
- **批准 = `git update-ref refs/heads/<target> <commit_sha>`**（线性保证下就是快进）。不需要 checkout、不碰任何工作树、不产生 MERGE_HEAD、天然原子

> 这就是为什么 merge 不落在主工作树里：`git checkout <target> && git merge` 若工作树是脏的会带上脏改动或直接拒绝；冲突则主仓库卡在 MERGE_HEAD，之后所有合并全废。

- 批准前若 target 又前进了（另一个任务被批），重新 rebase 并重算 diff；diff 变了就标记出来要求重新确认

### 5.6 崩溃恢复

**总原则：worktree / 分支 / 合并的真实状态以 git 为准，启动时把 DB 对回去。**

- `git worktree prune` 并与 DB 对账（崩溃遗留的 `.git/worktrees/*` 会让同路径 `worktree add` 报 already exists）
- **批准的幂等性**：动 git 前先写一条 intent（含 merge 前 target sha），启动时用 `git merge-base --is-ancestor` 对账。否则「rebase 完没 merge」或「merge 完没写库」重启后会让用户重复批准
- **`result` 事件是唯一权威判据**，崩在它落库和状态更新之间就全丢。靠原始日志文件在启动时重解析尾部，把 result / session_id / commit 捞回来
- runner 顶层 try/finally 保证任何异常都离开 `running` 并释放信号量

### 5.7 重试

失败后重试必须**重建 worktree 和分支**。旧 worktree 带着上次未提交的改动、且基于旧 base，直接复用会污染。

### 5.8 并发保护

按 `repo_path` 加一把 `asyncio.Lock` 包住所有 git 写操作（批准的 merge 与 runner 的自动提交会并发），代价近零。

### 5.9 SQLite

- `journal_mode=WAL`、`synchronous=NORMAL`、显式设 `busy_timeout`
- 同步 sqlite3 直接在事件循环里调用会阻塞整个 loop（每条 insert 含 fsync）→ 走 `to_thread`/`aiosqlite`
- **只有一条写连接、一个写线程**，批量提交（每 100 条或 200ms 一刷）；不要 `check_same_thread=False` 多线程共用连接
- 事务绝不跨 `await`；领取任务用单条原子 `UPDATE ... WHERE id=? AND status='queued'` + rowcount 判断

### 5.10 事件存储

**`task_events` 一定会爆**：`--include-partial-messages` 是 token 级事件，单任务 10^5–10^6 行，payload 里 tool_result 动辄上百 KB。

- **`stream_event` 一条都不落库**，只用于实时渲染（可从 assistant 事件重建）
- 原始 NDJSON 进文件，DB 只存语义事件 + `raw_log_path`
- 大 payload 截断，只留大小和预览
- 老事件按保留期清理（任务行和日志保留）
- 表用 `WITHOUT ROWID`、主键 `(task_id, seq)`，正好匹配「按任务顺序全取」的访问模式
- **WS 不逐条推**，合并到约 10fps；重连按日志文件的字节 offset 续读，不要回放 DB

### 5.11 记账

`result.usage` 是全程聚合：`input_tokens`、`output_tokens`、`cache_creation_input_tokens`、`cache_read_input_tokens`。

> ⚠️ **服务器走第三方中转，`total_cost_usd` 可能按 Claude 价目计算，与实际付费不符。成本必须自己按 token 算**，所以要存 `model`（init 事件里有，中转可能偷偷换模型）+ 版本化价目（`cost_usd` + `price_version`，否则改价目表等于篡改历史）。

---

## 6. 关键流程

### 6.1 调度

```python
while True:
    if running_count < MAX_CONCURRENT:
        task = next_queued()   # 每项目最多取一个；FIFO，priority 高者优先
        if task:
            start(task); continue
    await asyncio.sleep(1)
```

全局并发上限由内存决定（服务器可用内存有限，每个 Claude Code 进程 200–400MB）→ **默认 2，可配**。

### 6.2 执行一个任务

1. `git worktree add <worktree_root>/<task_id> -b cs/<id>-<slug> <target_branch>`
2. 组装 prompt = **指针图** + **文档目录索引** + 任务正文；存 `prompt_snapshot`
3. 拉起 `claude -p`，cwd = worktree，stdout 重定向到日志文件
4. tail 协程解析 NDJSON → 语义事件落库 + WS 推送；更新 `last_event_at`
5. 退出后：`result.subtype == "success"` → 有未提交改动则 `git add -A && git commit` → 记 `commit_sha` → rebase 到最新 target → `awaiting_review`
6. 否则 → `failed`，**保留 worktree 供排查**（但重试时会重建）
7. **沉淀步骤**：一次轻量调用，取 diff + 任务描述，生成带 `source`/`applicability`/`expiry` 元数据的变更记录追加到 journal（要幂等，重试不能重复追加）

### 6.3 批准 / 丢弃

- **批准**：确认分支包含当前 target（否则重新 rebase + 重算 diff）→ `git update-ref refs/heads/<target> <commit_sha>` → 清理 worktree 与分支 → `merged`，记 `merge_commit_sha`
- **丢弃**：`git worktree remove` + `git branch -D` → `discarded`
- 冲突 → 标为需要人工处理并列出冲突文件（**v1 不做 AI 自动解冲突**）

---

## 7. 上下文三层

沿用 `context-design.md` 的结论：

| 层 | 存放 | 注入方式 |
|---|---|---|
| 1 指针图 | `context_dir/pointer.md`，约 100 行 | 拼进 prompt 前缀，无条件 |
| 2 文档索引 | `context_dir/docs/` + `INDEX.md` | 索引进 prompt，正文由 AI 自己 Read |
| 3 沉淀 | `context_dir/journal.md` | 任务结束后自动追加 |

**关键决策：上下文目录放在 worktree 之外**，通过 `--add-dir` 授权访问 —— 完全不用碰被调度仓库的 `.gitignore`，也不污染它的 git 历史。

指针图只写四类内容：带参数的 build/test/lint 命令、非显然约束、项目专有工具用法、每条任务都适用的硬规则。**不写"这个项目是做什么的"**。

沉淀记录的三字段元数据写在 **HTML 注释**里（进模型前会被剥离，不花 token，但人和工具能读）。

---

## 8. 仓库布局

```
/srv/codingstorm/
  repos/<project>/                     被调度的仓库（codingstorm 独占）
  worktrees/<project>/<task_id>/       每任务一个
  contexts/<project>/                  三层上下文
  logs/<project>/<task_id>.ndjson      原始事件流
  codingstorm.db
```

**服务器上这份克隆由 codingstorm 独占**：只有 AI 在它上面跑任务，用户本地开发、通过 git remote 同步。

这带来一个重要简化：**不需要脏工作树保护**。target 分支只由 codingstorm 通过 `update-ref` 推进，不会被别人的手工操作干扰，合并路径上不用做「工作树是否干净」的检查。

---

## 9. 实施阶段

### Phase 0 · 技术验证（先做，结果决定后续细节）

`scripts/spike.py`：在服务器上跑通一次 `claude -p`，把 NDJSON 落到文件，确认：

- [ ] 实际事件序列与字段（尤其 `result.usage` 是否如文档所述）
- [ ] `--verbose` 是否必需
- [ ] **`bypassPermissions` 下 PreToolUse hook 会不会触发** ← 决定边界方案
- [ ] `--add-dir` 授权后 AI 能否读到 worktree 外的上下文目录
- [ ] `--max-turns` 达到时的实际行为
- [ ] 超长行确实存在（验证 64 KiB 问题，确认改用文件的必要性）
- [ ] 服务器 2.1.220 与本地 2.1.274 的事件差异

### Phase 1 · 核心骨架

- SQLite schema、连接管理（WAL + 单写线程 + 批量提交）
- 项目注册、任务提交 API
- 调度器：每项目串行 + 全局并发 + 原子领取
- Runner：子进程 + 日志文件 + tail 解析 + 语义事件落库
- 孤儿回收、优雅退出、崩溃对账

### Phase 2 · Git 生命周期

- worktree 创建 / 清理 / prune 对账
- 自动提交、rebase、`update-ref` 快进批准
- diff 生成与展示
- 批准幂等（intent + `merge-base --is-ancestor`）
- 重试重建

### Phase 3 · Web UI

- 项目列表 / 任务队列
- 任务详情：实时输出（WebSocket，合并到约 10fps，工具调用渲染成卡片）
- diff 审阅 + 批准 / 丢弃
- 无构建步骤：Jinja2 + 原生 JS；diff 由服务端渲染成带 class 的 HTML，不依赖 CDN

### Phase 4 · 上下文三层

- 指针图编辑界面
- 文档目录管理
- 沉淀步骤接进执行流程（含幂等）

### Phase 5 · 安全边界与记账

- PreToolUse hook 拦截 `git push` / 网络出口
- token 记账、价目表版本化、成本展示

### Phase 6 · 消息入口接入（可选）

- 通过已有的消息网关（微信 / QQ）提交任务，执行完推结果

**Phase 0–3 是能解决痛点的最小闭环**，4–6 依次叠加。

---

## 10. 验证方式

- **Phase 0**：spike 产出的 NDJSON 文件，人工核对事件序列
- **Phase 1–2**：pytest 覆盖调度器状态流转、worktree 生命周期、rebase 判定、批准幂等；用临时 git 仓库跑端到端
- **崩溃恢复**：跑任务时 `kill -9` 平台，重启后确认孤儿被杀、DB 与 git 对账正确、没有重复批准
- **Phase 3**：浏览器走完整流程 —— 提交 → 看实时输出 → 审 diff → 批准 → 确认 target 分支上出现改动
- **边界**：构造一个试图 `git push` 的任务，确认被拦下且模型收到拒绝原因
- **端到端**：测试仓库上连续提交 3 个任务，确认串行执行、第 2 个任务看不到第 1 个未合并的改动、批准第 1 个后第 2 个能正确 rebase

---

## 11. 风险

- **服务器可用内存有限**（约 2.3G），并发上限务必保守；同机还跑着别的服务
- **全自动放行有真实风险**：被调度的项目会拿到 `bypassPermissions`。Phase 5 完成前不要接入重要项目
- **`total_cost_usd` 不可信**（第三方中转），成本只按 token 自算
- Phase 0 若发现 hook 在 `bypassPermissions` 下不触发，需改用 `--permission-prompt-tool`（要写一个 MCP server，工作量增加）
- 部署方式待定：倾向 systemd user service，`KillMode=mixed`，不用 Docker 以省内存
