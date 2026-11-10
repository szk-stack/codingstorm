# codingstorm 使用教程

> 这份文档按「第一次打开 → 跑通一条任务 → 日常使用 → 排错」的顺序写。
> 想先看它解决什么问题、为什么这么设计，读 [`../README.md`](../README.md) 和
> [`architecture.md`](architecture.md)；只想查命令，直接跳到最后的[命令卡](#命令卡)。

## 1. 它是什么

codingstorm 是一个**按项目隔离的 AI 编码任务队列**。核心是把「坐在终端前等 AI 改完」
拆成两步：

```
你：提交一条需求 ──► 立刻返回，去干别的
                        │
                        ▼
                 服务器上：AI 在独立的 worktree 里改代码、跑测试、自动提交
                        │
你：回来看 diff ──► 批准合入主干 / 丢弃 / 让它接着改
```

三个关键约束，先记住能少踩很多坑：

1. **每任务一条分支、一个临时工作区**。任务之间看不到彼此未合并的改动。
2. **批准才合入主干**，而且批准不可逆；有冲突会拒绝，不会硬合。
3. **任务一旦提交，AI 就无人值守地跑**，不会再问你问题。所以任务描述要写清楚。

---

## 2. 启动与检查

### 2.1 执行机上要有

| 依赖 | 检查 | 说明 |
|---|---|---|
| Python 3.11+ | `python --version` | 服务端和 `cs` 都靠它 |
| git | `git --version` | 平台要建 worktree、rebase、推进分支 |
| 执行器 | `claude --version` / `pi --version` / `codebuddy --version` | 至少一个，且已登录 / 配好模型 |

### 2.2 安装与启动

```bash
pip install -e ".[dev]"        # 装出 codingstorm 和 cs 两个命令
python -m codingstorm.app      # 默认 http://127.0.0.1:8788/
```

不用先写配置文件，全部有默认值。验证：

```bash
curl http://127.0.0.1:8788/api/health
# {"ok":true}
```

想改端口、并发、执行器，再：

```bash
cp codingstorm.example.toml codingstorm.toml
python -m codingstorm.app --config codingstorm.toml
```

### 2.3 选执行器（Claude Code / Pi / CodeBuddy）

默认 Claude Code。全局换 Pi：

```toml
[runtime]
default = "pi"

[pi]
binary = "pi"
model = "deepseek/deepseek-chat"   # 无人值守建议写死
```

换 CodeBuddy（它的 headless 模式兼容 Claude Code，配置更少）：

```toml
[runtime]
default = "codebuddy"

[codebuddy]
binary = "codebuddy"        # 或 cbc
# model = "gpt-5"           # 留空用 CodeBuddy 自己的默认模型
```

只让某个项目换：

```bash
cs runtime demo pi          # 也可以 codebuddy / claude / inherit
cs runtime demo             # 看当前值
```

界面里也有两个入口：注册项目时的「执行器」下拉，以及选中项目后任务队列标题右侧的
切换下拉。**执行器是项目级的，不是每条任务级**——同一条任务的多轮对话必须用同一个
执行器，否则旧会话打不开。

> Pi 和 Claude Code 的差异、已验证范围见 [`pi-agent-integration.md`](pi-agent-integration.md)。
> 最重要的一条：Pi 在助手回复失败时仍然退出 0，平台看事件流判成败，不看退出码。
> CodeBuddy 的事件流与 Claude Code 同构，新会话用 `--session-id`、续跑用 `--resume`；
> headless 下需要 `--dangerously-skip-permissions`。详见
> [`codebuddy-integration.md`](codebuddy-integration.md)。

### 2.4 不依赖 CC Switch / 本地代理

如果 Claude Code 的 `~/.claude/settings.json` 指向一个本地代理（比如 CC Switch 的
`http://127.0.0.1:15721`），不打开那个工具时代理不会监听，任务就会连不上模型。

给 codingstorm 单独注入直连配置即可：

```toml
[claude.env]
ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"
ANTHROPIC_AUTH_TOKEN = "sk-..."
ANTHROPIC_MODEL = "deepseek-v4-flash"
ANTHROPIC_DEFAULT_SONNET_MODEL = "deepseek-v4-flash"
ANTHROPIC_DEFAULT_OPUS_MODEL = "deepseek-v4-pro"
```

- 这些值会写进平台生成的 `--settings` 文件，**优先级高于 `~/.claude/settings.json`**；
- 只影响 codingstorm 拉起的任务，你自己在终端里跑的 `claude` 不受影响；
- token 是明文，`codingstorm.toml` 不要进版本库；
- 换供应商时改这里、重启服务即可，不需要改全局 Claude 配置。

---

## 3. 五分钟跑通第一条任务

### 第 1 步：注册项目

浏览器打开 <http://127.0.0.1:8788/>，左栏「注册项目」填个名字（如 `demo`），
仓库路径留空，平台会建一个空裸仓库。或者：

```bash
curl -X POST http://127.0.0.1:8788/api/projects \
  -H 'content-type: application/json' \
  -d '{"name":"demo"}'
```

两条硬规则：

- **项目名只能用字母、数字、点、下划线、短横线**（它会变成 `repos/`、`worktrees/`、
  `contexts/` 下的目录名）；中文请用拼音，比如 `测试项目1` → `test-project-1`。
- **仓库路径**：
  - 留空 → 新建空裸仓库；
  - 填一个不存在的路径 → 自动创建目录并 `git init`；
  - 填一个存在但不是 git 仓库的目录 → 原地 `git init`；目录里有文件会做一次
    初始提交（`codingstorm: 导入现有文件`）；
  - 已经是一个 git 仓库 → 直接使用；已有分支时必须包含 `target_branch`。

本机运行服务时，仓库路径右边有 **「浏览…」**，可以打开系统文件夹选择器直接选目录。
对话框是**后端进程**弹的，所以服务必须和你在同一台机器上；远程服务器上弹的会是
服务器桌面的对话框。

> 普通仓库（不是裸仓库）也能用，但批准任务时平台用 `update-ref` 推分支，不会同步
> 你那个目录里的工作区；批准后需要在目录里 `git reset --hard` 才会看到新文件。
> 长期用建议还是做裸克隆。

也可以手动做一个裸克隆再注册：

```bash
git clone --bare <你的仓库> ~/codingstorm/repos/demo.git
curl -X POST http://127.0.0.1:8788/api/projects \
  -H 'content-type: application/json' \
  -d '{"name":"demo","repo_path":"/home/ubuntu/codingstorm/repos/demo.git"}'
```

### 第 2 步：提交任务

```bash
cs submit demo "写一个快速排序函数和对应测试"
# 已入队 <任务id>（排队中）：写一个快速排序函数和对应测试
```

命令立刻返回。可以加参数：

```bash
cs submit demo "修复登录页在窄屏下按钮溢出" \
  --body "复现步骤：宽度 320px 时..." \
  --kind bug \
  --priority 10
```

`kind` 只是标签，不改变流程；`priority` 越大越先被领取。

### 第 3 步：看执行过程

```bash
cs ls                    # 状态；跑到 awaiting_review 就是干完了
cs show <任务id>          # 详情、失败原因、token 用量
cs events <任务id>        # 它一步步做了什么
```

网页上对应任务详情的「执行过程」页签，工具调用会渲染成可展开的卡片。

### 第 4 步：审 diff，决定要不要

```bash
cs diff <任务id>          # 改了哪些文件、哪些行
cs approve <任务id>       # 满意 → 合入主干
cs discard <任务id>       # 不满意 → 丢掉，主干纹丝不动
```

网页上在任务详情「改动」页签，diff 下面就是批准 / 丢弃按钮。

### 第 5 步：不满意就接着改

```bash
cs say <任务id> "改成返回列表，不要返回单个值"
cs say <任务id> "再加一个边界用例"
```

同一条任务、同一条分支、同一个会话，AI 记得前面几轮做过什么。改动是累积的，
最后一次性批准或丢弃。

---

## 4. Web UI 导览

```
┌───────────────┬──────────────────────────────────────────────┐
│ 项目           │ 执行窗口横幅（启用时）                        │
│  · demo        │ 任务队列  [执行器下拉]                       │
│ 注册项目        │ 定时任务                                     │
│               │ ┌──────────────────────────────────────────┐ │
│ 项目上下文      │ │ 任务详情                                  │ │
│  指针图         │ │ 对话 / 执行过程 / 改动 / 阶段             │ │
│  变更记录       │ │                                          │ │
│  文档 / 文件    │ └──────────────────────────────────────────┘ │
└───────────────┴──────────────────────────────────────────────┘
```

- **左栏项目列表**：显示 `主干 · 执行器`；点项目切换。
- **注册项目**：名字必填；仓库路径留空自动建；执行器可选跟随全局 / Claude / Pi。
- **项目上下文**：四个页签——指针图、变更记录、文档、文件。
  - **文件**页签能在「主干」和「本次任务」之间切，不用 clone 就能看仓库内容。
- **右栏任务队列**：标题右边是当前项目的执行器下拉，切换只影响之后启动的任务。
- **任务详情**：
  - **对话**：多轮往返；
  - **执行过程**：实时推送，工具调用是可展开卡片；
  - **改动**：diff + 批准 / 丢弃；
  - **阶段**：严格模式专用。
- **实时**：WebSocket + 2 秒轮询兜底；时间按配置时区显示。
- 路由是 `#/task/<id>`，链接可分享、刷新回到原处。
- **顶栏切换**：`任务队列` / `助手` 两套整屏视图，用 `#/assistant` 区分；助手页是个 iframe，切走再切回来对话还在。

---

## 5. 功能详解

### 5.1 任务状态与生命周期

| 状态 | 含义 | 你能做什么 |
|---|---|---|
| `queued` | 排队中 | 取消（只能走接口） |
| `running` | 正在执行 | 等 |
| `awaiting_review` | 跑完了，等你看 diff | 批准 / 丢弃 / 继续对话 |
| `awaiting_decision` | 严格模式停在人工门 | 发【进入开发】/ 丢弃 |
| `merged` | 已合入主干（终态） | — |
| `discarded` | 已丢弃（终态） | — |
| `cancelled` | 排队时被取消（终态） | — |
| `failed` | 执行失败 | 重新入队 / 丢弃 / 继续对话 |
| `interrupted` | 被中断（平台重启等） | 重新入队 / 丢弃 |

批准路径是：先尝试把任务分支 rebase 到最新主干 → `git update-ref` 快进主干 →
清理 worktree 和分支。**有冲突返回 409，不会硬合。**

### 5.2 执行器：Claude Code / Pi / CodeBuddy

| | Claude Code | Pi | CodeBuddy |
|---|---|---|---|
| 全局默认 | `[runtime] default = "claude"` | `"pi"` | `"codebuddy"` |
| 项目覆盖 | 注册时选 / `cs runtime <项目> <名字>` / 界面下拉 | 同左 | 同左 |
| 轮数上限 | `--max-turns` | 平台数 `turn_end`，下一轮前拦 | `--max-turns` |
| 边界 | PreToolUse hook | `tool_call` 扩展调同一个 `guard.py` | PreToolUse hook（同 Claude） |
| 会话 | `--session-id` / `--resume` | 同一个 `--session-id` 打开或新建 | `--session-id` / `--resume` |
| 成本 | 按 token × 你的价目表 | 同左（不采信它自己报的 cost） | 同左 |

CodeBuddy 的 headless 事件流与 Claude Code 同构（init → assistant → result），所以平台
复用同一套解析器；差异只在命令参数：非交互操作需要 `--dangerously-skip-permissions`，工具黑名单用
`--disallowedTools`（驼峰）。

切换执行器时，如果任务还没结束：工作区会复用（改动不丢），但会开新会话并重新注入
完整上下文——旧 CLI 的会话文件新 CLI 打不开。

**CodeBuddy 的模型可以按项目选**：队列标题右侧的「模型」下拉只在项目的有效执行器是
CodeBuddy 时出现，注册项目时也能预选。选完只影响该项目**之后启动**的任务，正在跑的
那条不受影响；「跟随全局」就是 `[codebuddy].model`。左栏项目名下面会显示当前模型。

候选清单的来源按顺序是：`[codebuddy] models` 里写死的清单 → 本机
`codebuddy --help` 里的 `Currently supported: (...)` → 内置常用列表。CLI 装了就没必
要自己维护名单，升级出新模型后重启服务即可；本机没装 CLI 也不用担心，下拉仍有一份兜底。
想知道这次用的是哪份来源，看 `GET /api/runtime/models` 的 `source` 字段
（`config` / `cli` / `builtin`）。

手动改模型覆盖：

```bash
curl -X PATCH http://127.0.0.1:8788/api/projects/<项目id>/model \
  -H 'content-type: application/json' -d '{"model":"glm-5.3"}'
```

> 注意模型 id 是 CodeBuddy 的，不是 DeepSeek / Claude 的；给 Claude / Pi 项目选了也不会生效，
> 所以这两种项目的下拉是隐藏的。

### 5.3 多轮对话

```bash
cs say <任务id> "把返回值改成列表"
```

- 只有 **待审阅** 和 **失败** 的任务能继续。
- 继续时不重建工作区，从已有分支检出。
- 只发新那一句给模型，不重复注入项目约定（上下文已经在会话里）。
- 每继续一轮多一次模型调用，会话越长输入 token 越多。
- 沉淀会按累积 diff 重写这条任务的变更记录，不会留下过时描述。

### 5.4 严格模式（先出方案，再动代码）

适合修 bug 这类希望「先看方案再动手」的任务。先在配置里开总开关：

```toml
[agents]
enabled = true
```

然后：

```bash
cs submit demo "修复清理缓存弹窗的目录列表无法滚动" --kind bug --mode strict
cs stage <任务id>          # 看当前阶段、产物、等待的令牌
cs say <任务id> "【进入开发】"   # 只有这个精确字符串能放行
```

流程：诊断 + 出方案（`diagnosis.md` / `idea.md`）→ 停在 `awaiting_decision` →
人发令牌 → 实现、净室评审、验证 → 回到普通审 diff 流程。

- 令牌校验在服务端，不写进 prompt 求模型自觉；带空格的变体一律 409。
- 阶段产物在 `{root}/runs/<项目>/<任务>/`，**在 worktree 之外**，不会进 diff。
- 挂起时不提交、不沉淀、不占并发位。
- 定时任务生成的任务一律 `plain`。

### 5.5 定时任务与执行窗口

这两件事经常被当成一件，其实是两件：

| | 决定什么 | 配在哪 |
|---|---|---|
| **定时任务** | 什么时候**入队** | 界面右栏 / `cs schedule` |
| **执行窗口** | 什么时候**能开始执行** | `[scheduler.window]` |

```bash
cs schedule add demo "每天跑一遍文档里的命令，把失效的改掉" --daily 09:00
cs schedule add demo "每周整理一次变更记录" --weekly 03:00 --days mon,wed
cs schedule add demo "一次性迁移" --at "2026-12-01 02:00"

cs schedule ls
cs schedule run <id>        # 立刻跑一次，不动原定排期
cs schedule pause|resume <id>
cs schedule rm <id>         # 已生成的任务留住
```

- 每次触发是**生成一条新任务**，不是复用。
- 错过就跳过，不补跑；一次性任务错过会标「已错过」留在列表里。
- 产出仍然要人工审；上一轮没审时界面会标红提醒。

执行窗口（谷时省钱）：

```toml
[scheduler.window]
enabled  = true
timezone = "Asia/Shanghai"        # 必须显式写
windows  = [["00:30", "08:30"]]   # 可多段；start > end 表示跨天
```

- 只拦新任务的启动，不打断已经跑起来的。
- 急事：`cs window off`，或者界面横幅右边「临时关闭窗口」。
- 这是内存态开关，重启即恢复；单个项目可以 `cs window --project demo always`。

### 5.6 上下文三层：让 AI 记住项目约定

放在 `{root}/contexts/<项目>/`，页面上可直接编辑：

```
pointer.md      项目约定，每个任务都会全量注入（软上限 6KB）
journal.md      变更记录，任务结束后自动追加，只注入最近一段
docs/INDEX.md   文档索引，会被注入；正文由 AI 需要时自己读
```

`pointer.md` 只该写四类内容：

1. 带参数的构建 / 测试 / 检查命令；
2. AI 看不出来的非显然约束；
3. 项目专有工具的用法；
4. 每条任务都适用的硬规则。

**不要写「这个项目是做什么的」「目录结构是怎样的」**——AI 读代码就能得到，
写进去只会稀释信噪比。HTML 注释在注入前会被剥掉，可以放心当备注写。

> ⚠️ 沉淀是每个任务额外一次模型调用，成本大致翻倍。成本敏感就设
> `context.sediment = false`。

### 5.7 安全边界

任务以全自动模式跑，无人值守必须如此。边界靠拦截器：

- 默认拦 `git push` 和 `git remote add/set-url/remove/rename`。
- 网络默认**放行**（开了会误伤依赖安装）；`guard_block_network = true` 可以打开。
- `guard_extra_deny` / `guard_allow` 追加正则规则和白名单。
- 每次工具调用写审计日志（`{root}/guard/audit.log`）。
- Claude Code 用 PreToolUse hook；Pi 用 `tool_call` 扩展，判定逻辑是同一个 `guard.py`。

**它不是沙箱。** 真正的边界是执行机隔离：别把重要的东西、长期凭证放在执行机上。

### 5.8 成本记账

**不采信 CLI 自己报的 `total_cost_usd` / `cost`。** 走第三方中转时那是按另一端价目算的。
成本只按 token × 你自己填的价目表：

```bash
cp prices.example.toml ~/codingstorm/prices.toml
```

- 单价单位是**每百万 token**；
- 没填的模型成本留空，界面如实显示「未配置价目表」；
- `version` 跟成本一起落库，改价目表不会改写历史成本；
- `cs usage` 把「执行任务」和「记录沉淀」分开统计。

### 5.9 项目文件浏览

左栏项目上下文 →「文件」页签：

- 默认看主干；
- 切到「本次任务」用提交 sha 定位，任务合并后也能回看当时改成了什么样；
- 二进制文件只报类型，不回内容。

### 5.10 命令行与接口

`cs` 是 HTTP API 的客户端，脚本、cron、SSH 里都能用。完整命令见[命令卡](#命令卡)，
HTTP 接口表见 [`../README.md#http-接口`](../README.md#http-接口)。

`cs` 支持任务 id 唯一前缀；项目参数接受名字或 id。服务地址默认
`http://127.0.0.1:8788`，换过端口就设 `CODINGSTORM_API`，或把 `--base` 写在子命令之前。

### 5.11 对话式助手（用大白话建任务）

顶栏右边可以在 **任务队列 / 助手** 之间切。助手是个聊天窗口：说需求，它整理成结构化任务丢进队列，**丢完立刻回来**，不等任务跑完。

它和任务队列是两个进程（**同一个仓库里的两份代码**）：

```
浏览器 ──┬─ 任务队列：FastAPI（8788）REST + WebSocket ← codingstorm/
         └─ 助手页：iframe → 助手进程（8001）        ← assistant/
```

- **能做什么**：看项目列表、看项目上下文、看最近任务、建任务。工具白名单只有这四个。
- **不能做什么**：改代码、跑命令、等任务执行完。风险边界比执行器小得多 —— 它最坏的结果是建了一条不该建的任务，删掉就行。
- **不等待**：`create_task` 拿到 `task_id` 就返回。这是刻意的，否则一轮对话要挂几十分钟。
- **上下文不连续**：它的工具都是平台接口，不读代码内容；要它了解项目，用左栏的「项目上下文」维护指针图。

启动：

```bash
# 手动（在仓库根目录进 assistant/）
cd assistant && start-assistant.bat
```

```toml
# 或者让平台启动时顺手拉起；dir 留空会自动找到仓库里的 assistant/
[assistant]
autostart = true
```

助手没起来时，「助手」页会显示具体原因（找不到 assistant/server.py / 端口没应答）和「启动助手」按钮；点一下就会拉起并把日志写到 `{root}/logs/assistant.log`。

> Key：优先 `[assistant] api_key`，留空则复用 `[claude.env]` 的 `ANTHROPIC_AUTH_TOKEN`。
> 助手配置模板是 `assistant/config.example.py`（已是需求分析师版）；本地 `assistant/config.py` 被忽略，不进版本库。

---

## 6. 典型工作流

### 修一个 bug

```bash
# 1. 先让它诊断、出方案（需要开 [agents].enabled）
cs submit demo "修复导出 CSV 时中文乱码" --kind bug --mode strict
cs stage <id>
# 2. 看 diagnosis.md / idea.md 没问题，放行
cs say <id> "【进入开发】"
# 3. 跑完审 diff
cs diff <id>
cs approve <id>
```

### 加一个功能

```bash
cs submit demo "给 stats.py 加 mode(numbers) 函数并补测试"
# 去干别的
cs ls
cs diff <id>
cs say <id> "边界用例再加一个空列表"
cs approve <id>
```

### 定时巡检

```bash
cs schedule add demo "跑一遍 README 里的命令，把失效的改掉" --daily 09:00
# 第二天回来看产出，仍然要人工批准
```

### 从零起一个新项目

1. 注册名字，仓库路径留空；
2. 第一次提交任务时平台会造一个初始提交；
3. 想拉到本地接着开发：从服务器 clone，不要本地 `git init` 再 push（没有共同祖先会被拒）。

```bash
git clone tencent:/home/ubuntu/codingstorm/repos/demo.git demo
```

---

## 7. 排错

| 症状 | 最常见原因 | 怎么办 |
|---|---|---|
| `cs` 报连不上 8788 | 服务端没起 / 换了端口 | `curl /api/health`；设 `CODINGSTORM_API` 或 `--base` |
| 任务一直「排队中」 | 执行窗口没开 / 并发占满 / `--no-scheduler` | `cs window`；`cs ls` 看是否有别的任务在跑 |
| 提交任务立刻失败 | 执行器没登录、不在 PATH、模型没配 | 看 `{root}/logs/<项目>/<任务id>.stderr` |
| Windows 上报 `[WinError 2]` | `claude` 是 npm 生成的 sh 垫片，Python 不能直接执行 | 平台会自动解析到包内 `claude.exe`；仍失败就把 `[claude] binary` 指到那个 `.exe` |
| 提交返回 409（找不到分支） | 仓库有分支但没有 `target_branch` | 错误信息里列了实际分支，改注册配置 |
| 批准返回 409（冲突） | 主干前进了，rebase 不上 | 不会硬合；需要人工处理冲突 |
| 跑了很久后失败，`subtype=error_max_turns` | 达到 `[task].max_turns` 轮数上限，不是模型挂了 | 点「继续」在同一个会话/工作区接着跑，或调大/设为 `0`（不限制；仍受 idle 超时保护） |
| 网页像没更新 | 浏览器缓存 | 强刷一次（外壳已 no-cache） |
| 上一轮的改动没出现在下一个任务里 | 还没批准 | 未合并改动对后续任务不可见，先批准 |
| Pi 任务失败但退出码是 0 | Pi 助手失败仍退 0 | 看 `cs show` 的错误原因和 `.ndjson` 里最后一条 assistant 的 `stopReason` |

服务端日志在启动方式决定的地方；原始事件流在 `{root}/logs/<项目>/<任务id>.ndjson`。

---

## 8. 已知限制

- 默认只监听本机，远程访问自己加反向代理。
- 并发上限由执行机内存决定（Claude Code 约 200–400MB/进程；Pi 是 Node 进程，同样不轻）。
- 任务粒度太细不划算：CLI 的固定开销很高，一句话塞多个需求也不会更快。
- 任务之间看不到彼此未合并的改动，这是设计的一部分。
- 执行窗口的「临时关闭」不持久，重启恢复。
- 定时规则只有一次性 / 每天 / 每周几，没有完整 cron，也没有「每隔 N 分钟」。
- 项目注册后只能改执行器（`cs runtime` / PATCH）；名字、仓库路径、目标分支仍要改库。
- Pi 上的严格模式还没有端到端验证。
- guard 是护栏，不是沙箱；`bash run.sh` 里再 push 拦不住。

---

<a id="命令卡"></a>

## 9. 命令卡

```bash
# 项目 / 执行器
cs projects
cs runtime <项目> [claude|pi|codebuddy|inherit]

# 任务
cs submit <项目> "<标题>" [--body "..."] [--kind requirement|instruction|bug|task] [--priority N] [--mode plain|strict]
cs ls [--project <名>] [--status <状态>]
cs show <任务id>
cs events <任务id>
cs diff <任务id>
cs stage <任务id>
cs say <任务id> "<追加的话>"
cs approve <任务id>
cs discard <任务id>
cs requeue <任务id>
cs usage

# 定时任务
cs schedule ls [--project <名>]
cs schedule add <项目> "<标题>" --daily 09:00
cs schedule add <项目> "<标题>" --weekly 03:00 --days mon,wed
cs schedule add <项目> "<标题>" --at "2026-12-01 02:00"
cs schedule run|pause|resume|rm <id>

# 执行窗口
cs window
cs window off | on
cs window --project <名> always|inherit|custom --windows "09:00-18:00"
```

服务地址：默认 `http://127.0.0.1:8788`；换过端口用 `CODINGSTORM_API` 或
`cs --base http://127.0.0.1:9000 ls`（`--base` 写在子命令之前）。
