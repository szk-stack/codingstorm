# 接入 CodeBuddy CLI

> 目标：把 CodeBuddy Code CLI 作为第三个执行器（`claude` / `pi` / `codebuddy`）。
> 官方文档：<https://www.codebuddy.cn/docs/cli/cli-reference>、
> <https://www.codebuddy.cn/docs/cli/headless>

## 1. 为什么能直接复用 Claude 的解析器

CodeBuddy 的 headless 模式是 **Claude Code 兼容**的：

- `codebuddy -p "..." --output-format stream-json` 输出同样的事件流：
  第一行 `system/init`，中间 `assistant` / `user` 消息，最后 `result`；
- `result` 同样带 `subtype` / `is_error` / `result` / `usage`；
- 工具调用/结果同样是 `assistant.message.content[].tool_use` 和
  `user.message.content[].tool_result`。

所以 `CodeBuddyRun` 直接继承 `ClaudeRun`，只覆盖命令构造。以后 CodeBuddy 的
usage 字段如果和 Claude 分叉，只改 `CodeBuddyRun`，不影响 Claude。

## 2. 命令差异

| | Claude Code | CodeBuddy |
|---|---|---|
| 二进制 | `claude` | `codebuddy`（或 `cbc`） |
| 非交互 | `-p` | `-p` / `--print` |
| 结构化输出 | `--output-format stream-json --verbose` | 同左 |
| 新会话 | `--session-id <uuid>` | `--session-id <uuid>`（2.181.0 支持） |
| 续跑 | `--resume <uuid>` | `--resume <id>` |
| 权限 | `--permission-mode bypassPermissions` | 同左，**另加 `--dangerously-skip-permissions`** |
| 工具黑名单 | `--disallowed-tools` | `--disallowedTools`（驼峰） |
| 轮数 | `--max-turns` | `--max-turns` |
| 额外目录 | `--add-dir` | `--add-dir` |
| 设置 | `--settings` | `--settings` |

关键点：

1. **新会话照常传 `--session-id`**（2.181.0 的 `--help` 已确认支持），续跑把同一个
   id 传给 `--resume`。这样平台不用依赖 init 事件回填也能找到会话；init 里返回的
   `session_id` 仍然会覆盖记录，保证以 CLI 实际使用的 id 为准。
2. **非交互必须 `--dangerously-skip-permissions`**。官方文档明确说 `-p` 下不加这个，
   文件读写 / 命令执行会被拦下来等授权 —— 无人值守就卡死了。边界仍由 PreToolUse
   hook 负责，不靠权限弹窗。
3. `--settings` 用的是**单独一份** `{root}/guard/codebuddy-settings.json`，只含 hook，
   不混入 `[claude.env]`（那是 Claude 的 provider 配置，CodeBuddy 不该继承）。
4. **退出码不可靠**：实测未登录时 CLI 退出 0，但 `result.is_error=true`、
   `subtype=error_during_execution`。平台和 Claude 一样，只看 `result`，不看退出码。
5. **提问工具名不同**：Claude 是 `AskUserQuestion`，CodeBuddy 2.181 的 init 工具列表里
   是 `AskUserForStructuredInput`，两个都放进 `--disallowedTools`，否则无人值守时
   可能停在结构化提问上。

## 3. 配置

```toml
[runtime]
default = "codebuddy"

[codebuddy]
binary = "codebuddy"
# 留空 = 用 CodeBuddy 自己的默认模型
# model = "gpt-5"
# 透传参数，给 CLI 升级后新增的开关用
extra_args = []
```

只让某个项目用：

```bash
cs runtime demo codebuddy
```

界面注册项目时也可以直接选「CodeBuddy」。

## 4. 边界

`GuardInstaller.install_codebuddy_guard()` 会生成：

```json
{
  "hooks": {
    "PreToolUse": [
      {"matcher": "Bash", "hooks": [{"type": "command", "command": "python .../guard.py"}]},
      {"matcher": "WebFetch", "hooks": [{"type": "command", "command": "python .../guard.py"}]}
    ]
  }
}
```

CodeBuddy 的 hook 协议与 Claude Code 兼容，判定逻辑仍然是同一个 `guard.py`。
和 Claude 一样，这是护栏不是沙箱。

## 5. 验证情况

### 已做

- 临时装了 CodeBuddy CLI **2.181.0**（`npm install --prefix /tmp ...`，未全局安装），
  `--help` 确认了命令里用到的全部开关：`--session-id`、`--resume`、`--max-turns`、
  `--permission-mode bypassPermissions`、`--dangerously-skip-permissions`、
  `--disallowedTools`、`--add-dir`、`--settings`、`--tools ""`、
  `--no-session-persistence`。
- 用真实 CLI 跑了一次未登录的 headless 调用，拿到了真实的
  `system/init` → `assistant` → `result` 事件：
  - init 的 `session_id` 就是我们传的 `--session-id`；
  - result 的 usage 字段名与 Claude 完全一致；
  - 未登录时 `result.subtype=error_during_execution`、`is_error=true`，
    错误文本在 `result.errors[]`；`CodeBuddyRun` 已把它映射成可读的失败原因
    （否则只会显示 `执行失败（subtype=...）`）。
- 登录后又跑了一次真实调用（`--model deepseek-v4.1-flash --tools ""`）：
  `result.subtype=success`、`result="OK"`、usage `6277 in / 2 out`，
  `apiKeySource=copilot.tencent.com`，确认登录态和模型 ID 都可用。
- 命令构造的单测：新会话带 `--session-id`、续跑带 `--resume`、
  `--dangerously-skip-permissions`、`--disallowedTools AskUserQuestion`、
  `--settings` / `--add-dir` / `--max-turns`。
- 事件解析复用 `ClaudeRun`，用构造事件验证了 session、文本、usage 映射。
- CodeBuddy 的 settings 不含 `[claude.env]`。
- API / CLI / 前端的选择枚举都能接受 `codebuddy`。

### 还没做（带工具的任务 / guard / 多轮）

最小模型调用已经通了，但还没有跑过带 Bash/读写工具的完整任务。第一次在真实项目里跑之前，建议确认（任何一条不符，先看
`{root}/logs/<项目>/<任务id>.ndjson`）：

1. `codebuddy --version` 有输出，且已登录 / 配好模型；
2. `codebuddy -p "Reply with exactly: OK" --output-format stream-json --verbose -y`
   第一行是 `system/init`，最后有 `result`，`is_error=false`；
3. 跑一条会调 bash 的任务，确认 `tool_execution` 结果能落到「执行过程」；
4. 提交一条会 `git push` 的任务，确认被 guard 拦下；
5. `cs say` 第二轮，确认 `--resume` 打开了同一会话（`attempts.session_id` 不变）；
6. 确认 `result.usage` 的字段名仍是 `input_tokens` / `output_tokens` /
   `cache_read_input_tokens` / `cache_creation_input_tokens`；如果变了，
   在 `CodeBuddyRun` 里覆盖 `_absorb_result` 做映射。

## 6. 已知限制

- **Windows 上用 npm 版要注意 `cmd.exe` 的 8191 字符命令行上限。** `codebuddy.cmd`
  会经过 cmd.exe，而任务 prompt 带项目约定 + 变更记录，很容易超。建议用官方原生
  安装器（`irm https://www.codebuddy.cn/cli/install.ps1 | iex`），把
  `[codebuddy].binary` 指到 `codebuddy.exe`；或者把服务跑在 Linux 上。
- **并发 / 本地控制端口**：CodeBuddy 即使 headless 也会起一个本地 HTTP 服务，
  默认「复用稳定端口」。从 WorkBuddy 之类宿主终端里启动 codingstorm 时，宿主注入的
  `CODEBUDDY_SESSION_ID` 会被子进程继承，于是任务进程挑中宿主正在监听的同一个端口，
  报 `Failed to start server. Is port 54153 in use?`。平台现在固定传 **`--port 0`**
  （系统临时分配），不依赖宿主的会话状态。仍然建议先保持
  `scheduler.max_concurrent = 1` 跑通再加并发。
- **账号限流**：`result.errors[]` 里的 `429 您的使用量已超出频率限制` 是账号侧状态，
  平台会把它原样显示在任务详情里；换模型或等重置，重试任务用 `cs requeue <id>`。
- CodeBuddy 的 `--resume` 是按会话 id 找的；如果它把会话存在项目级目录，
  换 worktree 路径可能找不到。**没有实测**，出问题先看 stderr。
- 用量字段按 Claude 的结构读；字段名分叉时 token 会记成 0，成败和文本不受影响。
- `--dangerously-skip-permissions` 是必要的，但也意味着**边界完全靠 guard hook**，
  和 Claude 的 `bypassPermissions` 一样，不要把它当沙箱。HIGH/CRITICAL 命令在
  `-y` 下仍可能要求确认；无人值守场景下平台会按 idle timeout 终止。
- CodeBuddy 版本迭代快；新开关先用 `[codebuddy].extra_args` 透传，别改死在代码里。
