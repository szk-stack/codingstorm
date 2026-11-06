# 接入 Pi Coding Agent

> 日期：2026-11-06
> 目标：让 codingstorm 能把任务交给 Pi 执行，而不只是 Claude Code。
> 官方 wire reference：<https://pi.dev/docs/latest/json>、<https://pi.dev/docs/latest/cli>

## 1. 设计：把「执行器」抽出来

调度器只关心四件事：任务什么时候跑、在哪个 worktree 跑、跑完怎么收尾、花了多少 token。
它不应该知道 CLI 输出的是 Anthropic 的 stream-json 还是 Pi 的 JSONL。

所以：

```
Scheduler ──► Runner ──► AgentRuntime（claude | pi）
                 │            ├─ build_command()      命令行
                 │            └─ new_run() ──► AgentRun
                 │                              ├─ handle(event)   一行 JSON → 语义事件
                 │                              ├─ finish()        进程退出后补判成败
                 │                              └─ recover()       从日志尾部捞结果
                 └─ 子进程 / tail / 看门狗 / 落库（与执行器无关）
```

语义事件仍然是四种：`init` / `assistant` / `tool_result` / `result`。
Web UI、成本记账、多轮会话、diff / approve 全部不用改。

## 2. 两个 CLI 的差异（已核实）

| | Claude Code | Pi |
|---|---|---|
| 输出 | `--output-format stream-json --verbose` | `--mode json` |
| 会话创建 | `--session-id <uuid>` | `--session-id <id>` |
| 会话续跑 | `--resume <uuid>` | 同一个 `--session-id`（存在就打开，不存在就新建） |
| 轮数上限 | `--max-turns N` | **没有**；平台数 `turn_end`，在下一轮 `turn_start` 前拦下 |
| 权限 | `bypassPermissions` + PreToolUse hook | **没有权限系统**；边界靠 `tool_call` 扩展 |
| 工作目录外的文件 | 需要 `--add-dir` | 按启动用户权限访问，不限制在 cwd |
| 成败 | `result.is_error` | `agent_settled.aborted` + 最后一条 assistant 的 `stopReason` |
| 用量 | `result.usage` 一次给全 | 每条 assistant 的 `usage` 累加 |
| 认证重试 | `api_retry` 最多 10 次（约 3 分钟） | `auto_retry_start` 最多 3 次 |

**最重要的一条：Pi 的 JSON 模式在助手回复失败时仍然退出 0。**
退出码不能用来判成败，必须看事件流。

## 3. 事件映射

| Pi 事件 | 平台事件 | 说明 |
|---|---|---|
| `session` | `init` | 第一行；`id` 就是会话 id，`cwd` 是工作区 |
| `message_end`（assistant） | `assistant` | `content[]` 里的 `text` / `toolCall`；`toolCall.arguments` 映射成 UI 认的 `input` |
| `tool_execution_end` | `tool_result` | `result.content[].text` 拼成文本；`isError` 原样带出 |
| `agent_settled` | `result` | 成败、最终文本、轮数、聚合后的 usage |

**不落库的**：`message_start` / `message_update`（delta）、`turn_start` /
`turn_end`（只用来数轮数）、`compaction_*`、`queue_update`、`entry_appended`。
理由和 Claude 的 `thinking_tokens` 一样：它们占事件量的绝大多数，逐条落库会把
`result` 这类关键事件挤丢。

### 用量字段

Pi 的 `Usage` 与 `attempts` 表的对应：

```
input       → input_tokens
output      → output_tokens         （reasoning 已含在 output 里，不重复加）
cacheRead   → cache_read_tokens
cacheWrite  → cache_creation_tokens
cost.total  → 不用。和 Claude 的 total_cost_usd 一样，按 token × 自有价目表算
```

## 4. 失败判定

```
agent_settled.aborted == true                    → aborted
最后一条 assistant.stopReason ∈
  {error, aborted, length, deferred}             → 失败
没有任何 assistant 消息                           → 失败（通常是认证/模型配置问题）
进程结束但既没有 agent_settled 也没有
  agent_end(willRetry=false)                     → 失败，原因带上 stderr
```

`agent_end(willRetry=false)` 已经声明「不会再有自动续跑」，所以进程正常退出时
即使没等到 `agent_settled`（流被截断），也按这一轮结束处理。

认证类错误（`401/403/unauthorized/invalid api key`）在 `auto_retry_start` 里
直接判死，不白等 3 次退避。

## 5. 边界拦截

Pi 没有 Claude Code 的 `PreToolUse` hook，也没有权限系统。能阻断工具调用的只有
扩展的 `tool_call` 事件：

```ts
pi.on("tool_call", async (event) => {
  // bash 命令交给平台生成的 guard.py 判定，退出码 2 = 阻断
  return { block: true, reason: "..." };
});
```

平台的实现（`codingstorm/hooks/pi_guard.ts`，启动时生成到
`{root}/guard/pi-guard.ts`）不重写规则，而是把 bash 命令按 Claude hook 的协议
喂给同一个 `guard.py`。**规则只有一份**（`guard/guard.json`），不会两边漂移。

注意：

- 被拦下的调用在事件流里表现为一条 `is_error=true` 的 `tool_execution_end`，
  模型能看到 `reason`，这一点和 Claude 的 hook 一致。
- 扩展在 `print` / `json` 模式下 `ctx.hasUI === false`，所以不能用弹窗确认，
  只能按规则无条件拒绝（fail closed）。
- 扩展自身崩了（找不到 python、超时）按放行处理，和 `guard.py`「读不懂就放行」
  保持一致。**这不是沙箱**：真正的边界是执行机隔离，别把重要东西放在上面。
- 和 Claude 一样，这是模式匹配，不是内核级隔离。`bash run.sh` 里面再 `git push`
  拦不住 —— 现有 hook 也有同样的限制。

## 6. 上下文与严格模式

- 上下文目录（`contexts/<项目>/`）、严格模式的运行目录（`runs/<项目>/<任务>/`）
  都在 worktree 之外。Pi 不限制文件访问，prompt 里给的是绝对路径，可以直接读写。
- `AGENTS.md` / `CLAUDE.md` 会被 Pi 自动加载（和项目信任无关）。因此
  `pointer.md` 的注入方式和 Claude 一致，不需要额外处理。
- 严格模式的 `state.json` / 阶段产物靠 prompt 指令读写，不依赖特定 CLI 的能力，
  Pi 可以跑。但严格模式目前只在 Claude Code 上做过端到端验证。

## 7. 配置

全局默认：

```toml
[runtime]
default = "pi"

[pi]
binary = "pi"
model = "deepseek/deepseek-chat"   # 可选
provider = "deepseek"              # 必须和 model 一起
thinking = "high"                  # 可选
project_trust = "ignore"           # 默认：不加载仓库里的 .pi 扩展
```

项目级覆盖（注册时）：

```bash
curl -X POST http://127.0.0.1:8788/api/projects \
  -H 'content-type: application/json' \
  -d '{"name":"demo","runtime":"pi"}'
```

已有项目切换：

```bash
cs runtime demo pi          # inherit = 跟随全局
```

或者走接口：

```bash
curl -X PATCH http://127.0.0.1:8788/api/projects/<项目id> \
  -H 'content-type: application/json' \
  -d '{"runtime":"pi"}'
```

界面注册时也可以直接选「执行器：Pi」。`null` = 跟随全局。

## 8. 验证情况

### 已在 Pi 1.5.0 上实测通过

开发机临时装了 `@earendil-works/pi-coding-agent@1.5.0`（没有全局安装），用本地
OpenAI 兼容的假模型跑通了完整链路：

- 平台构造的命令行全部被接受：`--mode json`、`--session-id`、`--session-dir`、
  `--no-approve`、`--no-tools`、`--tools`、`-e`、`--`（prompt 放在最后）。
- JSON 事件序列与文档一致；`usage` 字段是 `input/output/cacheRead/cacheWrite`，
  `stopReason: "stop"`，`message_end` 是权威消息。
- 事件翻译：`session → init`、assistant `message_end → assistant`、
  `tool_execution_end → tool_result`、`agent_settled → result`。
- 工具调用：模型发起 `bash`，平台解析出 `tool_uses`；两轮用量正确累加（20 in / 4 out），
  `num_turns=2`。
- 边界拦截：在真实 Pi 进程里加载 `-e pi-guard.ts`，`git push origin main` 被
  `guard.py` 拦下，工具结果为 `is_error=true`，模型收到 `BLOCKED` 原因。
- 多轮会话：同一个 `--session-id` 连跑两次，第二次的模型请求里带上了第一轮的
  user/assistant 消息；会话文件落在 `--session-dir` 下。

### 上真机前还要确认

这次没有接真实模型（假模型只验证协议），以下几项建议第一次部署时各跑一遍：

1. 真实 provider 的鉴权能通；`pi --mode json "Reply with exactly: OK"` 能等到
   `agent_settled`。
2. 长任务里的 `auto_retry_start` / `compaction_*` 不会让看门狗误判（默认
   `idle_timeout_s=300`，与 Claude 一致）。
3. 严格模式在 Pi 上的两段式流程（`state.json` + `【进入开发】`）—— **没有实测过**，
   目前只在 Claude Code 上验证。
4. 超轮数：把 `[task].max_turns` 调成 1，确认任务被平台终止且原因写明 `max_turns`。
5. 认证失败：故意写错 key，确认任务失败原因是 stderr 里的真实错误，而不是空。


## 9. 已知限制

- **切换执行器会丢对话上下文。** 未完成任务的下一轮会复用工作区（改动不丢），
  但会开一个新会话并重新注入完整 prompt —— Claude 的会话 Pi 打不开，反之亦然。
  `scheduler` 用 `attempts.runtime` 检测到这一点，不会把一句话硬塞给新 CLI。
- **并发内存**按 Claude Code 估的（200–400MB/进程）；Pi 是 Node 进程，实际占用
  可能不同，`scheduler.max_concurrent` 仍要保守。
- **Pi 的会话文件**默认留在 `~/.pi/agent/sessions`，不随 worktree 清理。任务多了
  会积累；需要的话设 `[pi].session_dir` 指到 `{root}` 下自行清理。
- **模型名**来自 Pi 的 `message_end.model`，价目表按它匹配；用 `provider/id` 时
  确认价目表里写的是 Pi 实际报出来的那个名字。
- **严格模式**没有在 Pi 上做端到端验证（见第 6 节）。
- **guard 是护栏，不是沙箱**（见第 5 节）。
