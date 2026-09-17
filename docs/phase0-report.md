# Phase 0 · 技术验证报告

> 执行日期：2026-09-17
> 执行机：腾讯云服务器，Claude Code **2.1.220**，Ubuntu 24.04
> 脚本：`scripts/spike/`（可复现）
> 结论已并入 [`architecture.md`](architecture.md)，本文保留原始证据与复现方法。

## 结论速览

| # | 验证项 | 结果 | 影响 |
|---|---|---|---|
| 1 | 事件序列与字段 | ⚠️ 多出两个未记载类型 | 见 §1 |
| 2 | `--verbose` 是否必需 | ✅ **必需** | 命令行固定 |
| 3 | hook 在 `bypassPermissions` 下是否触发 | ✅ **触发且成功阻断** | **不需要 MCP server** |
| 4 | `--add-dir` 能否读 worktree 外文件 | ✅ 可用 | 上下文目录可放外面 |
| 5 | `--max-turns` 用尽的行为 | ✅ `subtype=error_max_turns` | 成败判据确定 |
| 6 | 64 KiB 超长行风险 | ❌ **未复现** | 修正了一条 overstated 的设计理由 |
| 7 | `stdin` 继承 | ⚠️ 会继承 | `stdin=DEVNULL` 是必需的 |

---

## 1. 两个未记载的事件类型

### `system` / `subtype:"thinking_tokens"`

```json
{"type":"system","subtype":"thinking_tokens","estimated_tokens":8,
 "estimated_tokens_delta":2,"uuid":"...","session_id":"..."}
```

**一句「Reply with exactly: OK」产生了 2890 行事件，其中 2877 行是这个类型 —— 占 99.6%。**

| 事件类型 | 条数 |
|---|---|
| `system/thinking_tokens` | **2877** |
| `assistant` | 8 |
| `user` | 3 |
| `system/init` | 1 |
| `result/success` | 1 |

→ **一条都不能落库**，只能用于实时渲染。容量设计要按这个量级算：如果每个任务按千行量级落库，加上 `stream_event` 会是灾难性的。

### `system` / `subtype:"api_retry"`

```json
{"type":"system","subtype":"api_retry","attempt":3,"max_retries":10,
 "retry_delay_ms":2114,"error_status":401,"error":"authentication_failed"
}
```

认证失败**不会立刻报错**，而是退避重试 10 次。实测退避序列（毫秒）：
`622 → 1017 → 2114 → 4761 → 8643 → 17294 → 35935 → 38696 → 35280`

**共计约 3 分钟才失败。**

→ runner 必须监听此事件并对不可恢复错误（401/403/CreditsError）**立即失败**，否则一个坏凭证会让任务白占并发位三分钟。
→ **不能只靠退出码判断成败。**

---

## 2. `--verbose` 是必需的

不传时报错并退出（退出码 1），**stderr 输出一行**、stdout 零行：

```
Error: When using --print, --output-format=stream-json requires --verbose
```

→ 注意这是**少数走 stderr 的错误**。认证失败走的是 stdout 的 NDJSON 事件。**两路都要看。**

---

## 3. hook 在 `bypassPermissions` 下会触发 ✅ ← 最关键的一项

**测试**：以 `--permission-mode bypassPermissions` 运行，要求模型执行 `git push origin main`；配置一个 PreToolUse hook 拦截 `git push` 并写日志。

**结果**：

- hook 日志记录了 41 次触发，包含 `tool=Bash cmd=git push origin main`
- 模型收到的工具结果是：
  ```
  PreToolUse:Bash hook error: [guard.sh]: BLOCKED: git push 在此环境被禁止
  is_error=True
  ```
- 远端裸仓库的 refs **为空** —— push 确实没有发生
- 模型**如实汇报了被拦截**，没有尝试绕过

→ **边界方案确定用 PreToolUse hook，不需要写 MCP server。** 工作量比预案少一截。

---

## 4. `--add-dir` 可以读到 worktree 外的文件

在工作目录外放 `ctx/context.md`，内容 `SECRET_WORD=platypus`，用 `--add-dir` 授权后：

```
claude -p "Read the file .../ctx/context.md and reply with only the value after the equals sign."
→ result: 'platypus'   退出码 0
```

→ 上下文三层可以放在 worktree 之外，**完全不用碰被调度仓库的 `.gitignore`**。

---

## 5. `--max-turns` 用尽时的行为

```
退出码 = 1
subtype  = "error_max_turns"
is_error = true
num_turns = 3
result   = None
```

→ 印证「**权威判据是 `result` 的 `subtype`/`is_error`，不是退出码**」。

---

## 6. 64 KiB 超长行风险：未复现 ❌

原设计里有一条强断言：「`Write`/`Read`/`Bash` 的输出轻松超过 64 KiB，解析器会在长任务里随机崩溃」。**实测推翻了它。**

| 测试 | 输入规模 | 最长行 |
|---|---|---|
| 普通问答 | — | 6,934 字节 |
| 读大文件 | 420 KB / 20000 行 | 1,779 字节 |
| 命令输出 | 120 KB | 2,685 字节 |

**没有任何一行超过 64 KiB（0 行）。**

原因有二：

1. **Claude Code 会先截断 `tool_result`**
2. 模型本身会规避 —— 让它读 420 KB 文件时它直接用 `wc -l` 数行，原话是 *"I counted with `wc -l` rather than pulling the file into context"*

**设计不变，但理由换了。** 「日志文件 + tail 协程」保留，因为另外两条独立成立：

1. **背压**：一条简单命令就产生 2890 个事件，同步落库会让读取循环成为瓶颈、拖死子进程
2. **崩溃日志恢复**：`result` 事件是唯一权威判据，崩在它落库和状态更新之间就全丢 —— 必须能从原始日志尾部挽回

---

## 7. `stdin` 继承

从脚本里以 `bash -s <<'EOF'` 方式拉起 `claude -p` 时，**claude 继承了脚本的 stdin**，把我脚本剩余的内容读成了上下文（从它生成的 `tool_use` 参数里能看到脚本尾巴）。

→ **`stdin=DEVNULL` 是必需的**，不是可选项。

---

## 8. 记账相关的量化数据

一次「Reply with exactly: OK」：

```
input_tokens              26198
cache_read_input_tokens   78080
output_tokens              3423
num_turns                     4
total_cost_usd          0.26507   ← 不可用
```

**两个要点：**

1. **`total_cost_usd` 不可用。** 它按 Claude 价目计算，而实际走的是 DeepSeek。必须按 `result.usage` 的 token 自算。
2. **固定开销极高。** 26K input + 78K cache read 全是 Claude Code 的系统提示与工具定义 —— 跟任务内容无关。**任务粒度太细不划算**，这会影响产品设计。

另：`assistant` 消息里的 `usage.output_tokens` 是 **0**，而 `result` 里是 3423。**流式产物的 usage 不完整，只有 `result` 的能用来记账。**

---

## 9. 顺带摸清的执行机配置

服务器 `~/.claude/settings.json` 现在指向 **DeepSeek 官方 Anthropic 兼容端点**：

```
ANTHROPIC_BASE_URL             = https://api.deepseek.com/anthropic
ANTHROPIC_MODEL                = deepseek-v4-flash
ANTHROPIC_DEFAULT_OPUS_MODEL   = deepseek-v4-pro
```

- 国内服务，**服务器直连，不需要代理**
- 模型角色**基本都映射到同一个 `deepseek-v4-flash`** → `--model` 参数没有选择空间

---

## 复现方法

```bash
# 上传脚本
scp scripts/spike/*.sh tencent:~/codingstorm/spike/

# 逐项运行
ssh tencent 'chmod +x ~/codingstorm/spike/*.sh && ~/codingstorm/spike/run-hook.sh'
```

各项脚本见 `scripts/spike/`。注意脚本里已统一使用 `</dev/null`（见 §7）。
