# codingstorm

按项目隔离的 Claude Code 任务队列。提交需求后**立刻返回**，服务器上的 Claude Code 在独立的
git worktree 里执行，跑完等你审 diff —— 把「等 AI 跑完才能下一条指令」变成「异步投递 + 事后审批」。

为什么这么设计、技术契约怎么核实的，见 [`docs/architecture.md`](docs/architecture.md)（主文档）。
本文件只讲怎么用。

## 它是怎么跑的

```
提交任务 ──► 排队 ──► 从主干切出 worktree ──► claude -p 执行 ──► 自动提交 ──► 待审阅
                                                                              │
                                              批准（合入主干）/ 丢弃 ◄── 你看 diff
```

- **同一项目同时只跑一个任务**，这是硬约束（同一个工作目录里并发跑两个 Claude Code 会互相踩文件）。
  不同项目可以并行，总并发由 `scheduler.max_concurrent` 控制。
- 每个任务从**最近一次已合并的主干**切出分支 `cs/<任务id>-<标题>`，批准即快进合入。
- 队列不阻塞，但**未批准的上游改动对后续任务不可见** —— 想让下一个任务看到，先批准。

## 快速开始

### 1. 安装

需要 Python 3.11+、git，以及 PATH 上有 `claude`（Claude Code CLI，且已登录）。

```bash
pip install -e .
```

装出两个命令：`codingstorm`（服务端）和 `cs`（客户端）。

### 2. 准备被调度的仓库

**接入已有的仓库** —— 先做成裸克隆放到服务器上：

```bash
git clone --bare <你的仓库> ~/codingstorm/repos/demo.git
```

**全新的项目** —— 跳过这步，注册时会自动建好空仓库（见第 5 步）。

**用裸克隆。** 批准走的是 `update-ref`，它只推进引用、**不同步工作树** —— 非裸仓库在第一次批准后
主工作区会与 HEAD 脱节，`git status` 看起来像「所有文件都被删了」。代码会对非裸仓库打一条告警。

codingstorm 会往仓库的 `info/exclude` 写一份默认排除列表（`__pycache__/` 等）。它是**未跟踪的本地
文件**，不进你的 git 历史，但对所有 worktree 生效 —— 没有 `.gitignore` 的仓库，收尾的 `git add -A`
会把 AI 跑代码产生的字节码缓存一起提交进去。

### 3. 配置

```bash
cp codingstorm.example.toml codingstorm.toml
```

不传 `--config` 时全部走默认值。常改的几项：

| 配置 | 默认 | 说明 |
|---|---|---|
| `root` | `~/codingstorm` | 所有运行时数据的位置 |
| `server.host` / `port` | `127.0.0.1` / `8787` | 默认只监听本机 |
| `scheduler.max_concurrent` | `2` | **由内存决定** —— 每个 Claude Code 进程约 200–400MB |
| `task.max_turns` | `50` | 不设上限的话，一个死循环 agent 能烧一整晚 |
| `task.wall_clock_timeout_s` | `3600` | 墙钟上限，超了整组 SIGTERM→SIGKILL |
| `task.idle_timeout_s` | `300` | 多久没有任何输出就判定卡死 |
| `context.sediment` | `true` | 任务结束后自动写变更记录（会多一次模型调用） |
| `claude.guard_block_push` | `true` | 拦 `git push` 与改 remote |

每项在 `codingstorm.example.toml` 里都有注释说明为什么这么设。

### 4. 启动

```bash
python -m codingstorm.app --config codingstorm.toml
# 等价：codingstorm --config codingstorm.toml
```

其他参数：`--host` / `--port` 覆盖监听地址，`--no-scheduler` 只起 HTTP 不执行任务（调试用），
`--log-level`。启动时**先做崩溃对账** —— 杀掉上次遗留的孤儿进程、把中断的任务标出来。

### 5. 注册项目

浏览器打开 `http://127.0.0.1:8787/`，在「项目」栏填个名字点注册。或者直接调接口：

```bash
curl -X POST http://127.0.0.1:8787/api/projects \
  -H 'content-type: application/json' \
  -d '{"name":"demo"}'
```

全部字段：

| 字段 | 默认 | 说明 |
|---|---|---|
| `name` | **必填** | 字母、数字和 `._-`，全局唯一。以后 `cs submit <这个名字>` 用它 |
| `repo_path` | `{root}/repos/<name>.git` | 留空就用它；**不存在时自动 `git init --bare` 建一个空仓库** |
| `target_branch` | `main` | 仓库已有提交时，这条分支必须存在 |

注册时会当场校验，不合格返回 400 并说明原因：路径不存在、路径不是 git 仓库、
`target_branch` 在仓库里找不到（这时会把实际存在的分支列出来）。

> **`cs` 没有注册项目的子命令。** 项目只在页面或接口里注册一次，之后都用 `cs`。

### 6. 从零新建一个项目

服务器连不上 GitHub，所以本地建好再推过去：

```bash
# 本地
mkdir myapp && cd myapp
git init -b main
echo "# myapp" > README.md && git add -A && git commit -m init

# 注册 —— 空仓库会自动建好
curl -X POST http://127.0.0.1:8788/api/projects \
  -H 'content-type: application/json' -d '{"name":"myapp"}'

# 把本地接到服务器上，推第一次
git remote add server tencent:codingstorm/repos/myapp.git
git push -u server main
```

推完就能用了。以后 AI 合并的改动用 `git pull server main` 拿回来。

> **一定要先 push 再提交任务。** 空仓库没有分支，也不该有任务跑在上面 ——
> 提交时就会被挡下（409，并给出上面那两条命令）。这条约束是实测加上的：
> 早先放任它入队，任务是注定失败的，而人只会看到一个「失败」，得翻说明才知道原因。

### 7. 提交第一个任务

```bash
cs submit demo "给 stats.py 加一个 mode(numbers) 函数" --kind requirement
```

立刻返回任务 id，不用等。过一会儿 `cs ls --status awaiting_review` 就能看到它待审。

## `cs` 命令

```bash
cs projects                    # 有哪些项目
cs submit <项目> "<标题>" [--body "..."] [--kind requirement|instruction|bug|task] [--priority N]
cs ls [--project <名>] [--status <状态>]
cs show <任务id>               # 详情 + 每次尝试的 token 与成本
cs events <任务id>             # 执行过程（模型文本、工具调用、工具输出）
cs diff <任务id>               # 改动内容
cs say <任务id> "<追加的话>"    # 接着上一轮继续说（多轮对话）
cs approve <任务id>            # 批准并合入主干
cs discard <任务id>            # 丢弃
cs requeue <任务id>            # 重新入队
cs usage                       # 用量与成本
```

- **任务 id 支持唯一前缀**，打前几位就够；前缀匹配到多条会提示写长一点。项目参数同样接受名字或 id。
- 服务地址默认 `http://127.0.0.1:8788`，用环境变量 `CODINGSTORM_API` 或 **写在子命令之前**的
  `--base`（`cs --base http://127.0.0.1:8787 ls`）改。
  ⚠️ 这个默认值和配置里的 `server.port` 默认值（**8787**）不一致 —— 在默认端口上跑服务时，
  要么设 `CODINGSTORM_API`，要么每次都带 `--base`。
- `kind` 只是标签，不改变执行流程。`priority` 越大越先被领取，同优先级按提交时间先到先得。
- `cs ls` 最多返回 200 条（服务端上限）。
- **没有 `cs cancel`** —— 取消只能走接口（见下）。而且只有还在排队的任务能取消。

## 任务状态

| 状态 | 含义 | 能做什么 |
|---|---|---|
| `queued` | 排队中 | 取消 |
| `running` | 正在执行 | 等 |
| `awaiting_review` | 跑完了，等你看 diff | 批准 / 丢弃 / 继续对话 |
| `merged` | 已合入主干（终态） | — |
| `discarded` | 已丢弃（终态） | — |
| `cancelled` | 排队时被取消（终态） | — |
| `failed` | 执行失败 | 重新入队 / 丢弃 / 继续对话 |
| `interrupted` | 被中断（平台重启等） | 重新入队 / 丢弃 |

失败原因在 `cs show <id>` 的「说明」一栏。

**批准是不可逆的**，而且会先尝试把你的分支 rebase 到最新主干；有冲突则返回 409
（「分支 X 与 main 有冲突，需要人工处理」），不会硬合。

`requeue` 重跑当前这一轮：**第一轮的任务会重建工作区**（旧的带着未提交的改动、且基于旧基点，
留着只会误导）；**追加过消息的任务则接着上一轮跑**，不重建。

## 多轮对话

一个任务不是只能问一次。跑完（待审阅）或失败之后可以接着聊 —— **同一条任务、同一条分支、
同一个会话**，AI 记得前面几轮做过什么：

```bash
cs say <任务id> "改成返回列表，不要返回单个值"
cs say <任务id> "再加个单元测试"
```

或者在 Web UI 的任务详情页「对话」页签里直接输入。

几件要知道的事：

- **只有「待审阅」和「失败」能继续。** 已合并/已丢弃的任务工作区和分支都清掉了，接着聊没意义。
- **改动是累积的。** diff 始终是「主干 → 最新」，最后一次性批准或丢弃。
- **继续时不重建工作区**，直接从已有分支检出，所以前几轮的提交都在。
- **只发新那一句给模型**，不重复注入项目约定 —— 上下文已经在会话里了。
- 怎么实现的：`claude --resume <会话id>`。**不用 `--continue`** —— 它找的是「cwd 里最近的会话」，
  多个任务共享目录时会串到别人的会话上。
- 每继续一轮就多一次模型调用，而且会话越长输入 token 越多。

> ⚠️ 有个坑值得记一笔：沉淀（写变更记录）是另一次独立调用、**另一个会话**。
> 如果接会话时没把它排掉，多轮对话会看起来「能答上来」—— 因为沉淀那次的 prompt 里
> 也有任务标题和 diff —— 实际上完全没接在任务历史上。代码里按 `origin='task'` 过滤掉了。

## Web UI

地址就是服务地址。左右两栏：**左栏**是项目列表（带注册表单）和项目上下文编辑区，
**右栏**上面是任务队列、下面是任务详情。

- 任务详情三个页签：**对话**（多轮往返，可继续输入）、**执行过程**（实时推送，工具调用渲染成可展开
  卡片）和**改动**（diff + 批准/丢弃按钮）。
- **文件**：左栏项目上下文里有个「文件」页签，能直接浏览仓库内容（目录树 + 点开看正文），
  不用 clone 下来。上面可以在「主干」和「本次任务」之间切 —— 后者看的是这条任务改出来的版本，
  用提交 sha 定位，所以任务合并之后也还能看。
- 路由是 hash 形式的 `#/task/<id>`、`#/task/<id>/diff`，链接可分享、刷新回到原处。
- 实时靠 WebSocket，另有 2 秒轮询兜底（事件可能因队列满被丢弃，状态得有独立通道保证最终一致）。
- 没有构建步骤，也不依赖任何 CDN。

## 上下文：让 AI 记住项目约定

模型每个任务都是全新会话（不 resume），跨任务的记忆靠**文档**。这些文件放在
`{root}/contexts/<项目名>/`，页面上可以直接编辑：

```
contexts/<项目名>/
  pointer.md      项目约定，每个任务都会全量注入。软上限 6KB
  journal.md      变更记录，任务结束后自动追加，只注入最近一段（默认 6000 字符）
  docs/
    INDEX.md      文档索引，会被注入
    其他文档.md    只登记在索引里，正文由 AI 需要时自己 Read
```

`pointer.md` 里只该写四类内容：带参数的构建/测试命令、AI 看不出来的非显然约束、项目专有工具的用法、
每条任务都适用的硬规则。**不要写「这个项目是做什么的」「目录结构是怎样的」** —— AI 读代码就能得到，
写进去只会稀释信噪比（实测这类内容反而让表现变差）。

模板和文件里的 HTML 注释在注入前会被剥掉，不占 token，可以放心当备注写。

`journal.md` 的每条记录带三个字段（写在注释里）：`source` 为什么改、`applicability` 何时适用、
`expiry` 什么条件下可以删。

> ⚠️ **沉淀会让成本大致翻倍。** 它是每个任务额外的一次模型调用，而 Claude Code 的固定开销很高
> （实测一次约 2.5 万 input token）。它是这套机制能活过三个月的前提，但成本敏感就
> `context.sediment = false` 关掉。

## 安全边界

任务以 `bypassPermissions` 全自动放行 —— 无人值守必须如此，否则它会在需要授权时挂住。边界靠
**PreToolUse hook** 拦（`permissions.deny` 在 bypass 模式下不可靠，hook 不受权限模式影响）：

- 默认拦 `git push` 和 `git remote add/set-url/remove/rename`
- 网络默认**放行**（`guard_block_network = true` 会连带挡掉正常的依赖安装，容易误伤）。
  打开后同时管住 Bash 里的网络命令和 `WebFetch` / `WebSearch` 工具
- `guard_extra_deny` / `guard_allow` 可以追加正则规则和白名单（白名单优先级最高）
- 每次工具调用都写审计日志（命令截断到 500 字符）

被拦下时模型会收到拒绝原因和规则来源，它会如实报告，而不是假装成功。

另一条必要防线是**别把重要的东西放在执行机上**：能力范围内它什么都能改，分支保护只保护得了代码。

## 成本记账

**不采信 `result.total_cost_usd`。** 走第三方中转时那是按另一端价目算的 —— 实测一句
「Reply with exactly: OK」它报 0.26507 美元。成本只按 token × 你自己填的价目表算：

```bash
cp prices.example.toml ~/codingstorm/prices.toml
```

单价单位是**每百万 token**，模型名从任务详情的用量里能看到。没填的模型成本**留空**，
界面如实显示「未配置价目表」—— 宁可显示未配置，也不给假数字。

`version` 会跟成本一起落库：**改价目表不会改写历史成本**，那等于篡改账本。

`cs usage` 把「执行任务」和「记录沉淀」分开统计 —— 沉淀是每个任务的固定开销，混进任务里就看不见了。

## 数据目录

启动后 `{root}` 下会有：

```
codingstorm.db      SQLite（WAL）。项目、任务、尝试、事件
contexts/<项目>/    pointer.md / journal.md / docs/（含 INDEX.md）
worktrees/<项目>/   每个任务一个临时工作区，批准或丢弃后清掉
logs/<项目>/<任务id>.ndjson   原始事件流（排错时看这个）
logs/<项目>/<任务id>.stderr   子进程 stderr
repos/              被调度的仓库。注册时留空 repo_path 就用这里，不存在会自动新建
```

运行时还需要的文件在 `{root}` 旁边（不进版本库）：`prices.toml`、`codingstorm.toml`。

**排错先看日志**：`logs/<项目>/<任务id>.stderr` 是 `claude` 自己的输出；服务端日志在启动方式决定的地方
（部署脚本写 `~/codingstorm/app.log`）。

## HTTP 接口

`cs` 和 Web UI 都是这套接口的客户端，可以直接用。

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET / POST | `/api/projects` | 列出 / 注册项目 |
| GET | `/api/projects/{id}` | 项目详情 |
| GET / POST | `/api/projects/{id}/tasks` | 列出 / 提交任务 |
| GET | `/api/tasks` | 列出任务（`?project_id=&status=`） |
| GET | `/api/tasks/{id}` | 任务详情 |
| GET | `/api/tasks/{id}/attempts` | 每次尝试的用量 |
| GET | `/api/tasks/{id}/events` | 事件（`?after_seq=&limit=`，默认 500） |
| GET / POST | `/api/tasks/{id}/messages` | 读 / 追加一轮对话（POST 后任务回到队列） |
| GET | `/api/tasks/{id}/diff` | diff（含服务端渲染好的 HTML 和 stat） |
| POST | `/api/tasks/{id}/approve` | 批准合并 |
| POST | `/api/tasks/{id}/discard` | 丢弃 |
| POST | `/api/tasks/{id}/requeue` | 重新入队 |
| POST | `/api/tasks/{id}/cancel` | 取消（**只有 `queued` 能取消**） |
| GET | `/api/projects/{id}/tree` | 列目录（`?path=&ref=`，ref 缺省是主干） |
| GET | `/api/projects/{id}/file` | 读文件（`?path=&ref=`，二进制只报类型不回内容） |
| GET | `/api/usage` | 用量与成本（`?project_id=`） |
| GET | `/api/projects/{id}/context` | 读上下文（指针图 + 变更记录 + 索引 + 文档清单） |
| PUT | `/api/projects/{id}/context/pointer` | 写指针图 |
| PUT | `/api/projects/{id}/context/index` | 写文档索引 |
| GET / PUT / DELETE | `/api/projects/{id}/context/docs/{路径}` | 读 / 写 / 删一篇文档（PUT 兼作新建） |

## 部署

`scripts/deploy.sh` 把代码推到执行机并重启：

```bash
./scripts/deploy.sh [ssh别名]     # 默认 tencent
```

它会上传代码、等旧进程真正退出、确认端口释放，再启动并校验「进程数恰好为 1 且健康检查通过」。

> ⚠️ **重启逻辑不能简化。** 早期版本只 `pkill` + `sleep 1` 就启新的 —— 旧进程收到 SIGTERM 后还要
> 优雅退出（等运行中的任务收尾），根本来不及死，结果服务器上堆了 5 个进程，**旧版本一直占着端口在
> 服务，改了代码却看不到效果**。

首次部署的前置条件（脚本头部也有）：在目标机上建好 venv 并装上 `fastapi uvicorn websockets`，
以及 `~/codingstorm/codingstorm.toml`。

## 开发

```bash
pip install -e ".[dev]"
pytest -q          # 173 项
```

代码在 `codingstorm/`：`api.py`（HTTP）、`scheduler.py`（并发与生命周期）、`runner.py`（拉起子进程、
解析 NDJSON）、`workspace.py`（worktree 与合并）、`context.py`（上下文三层）、`store.py` + `db.py`
（数据层）、`cli.py`（`cs`）、`static/`（无构建步骤的前端）。

改架构前先读 `docs/architecture.md`，那里记录了已核实的技术契约和压测出来的实现约束 ——
不少看起来能简化的地方，简了就会踩坑（该文档第 5 节整节都是）。踩过的坑另见 [`docs/pitfalls.md`](docs/pitfalls.md)。

## 已知限制

- **默认只监听本机。** 要远程访问请自己加反向代理。
- **执行机内存决定并发上限。** 每个 Claude Code 进程约 200–400MB，超了会 OOM。
- **任务粒度太细不划算。** Claude Code 的固定开销很高（实测一句「OK」约 2.6 万 input token），
  一句话塞多个需求也不会更快 —— 一个任务一次执行。
- **任务之间看不到彼此未合并的改动**，这是设计的一部分，不是 bug。
- 项目一旦注册只能通过数据库改（没有改和删的接口）；`enabled` 字段存在但界面没暴露。
