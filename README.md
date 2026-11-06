# codingstorm

**一句话**：想让 AI 改代码，发一句话就可以离开；它改完会自动提交，你回头看改动，满意才点批准合进主干。

平时用 Claude Code 得开着终端守着它跑完，才能提下一个需求。codingstorm 把这个过程拆开了：

```
你：发一条需求 ──► 立刻返回，去干别的
                     │
                     └─ 服务器上：AI 在独立副本里改代码、跑一遍、自动提交
你：回来看改动 ──► 点「批准」合入主干 ／ 点「丢弃」／ 让它接着改
```

装好之后（见下面的「先跑起来」），一次典型的使用是这样的：

```bash
cs submit demo "给 stats.py 加一个 mode(numbers) 函数"
# 立刻返回任务 id，不用等
cs diff <任务id>          # 回来看它改了什么
cs approve <任务id>       # 满意，合进主干
```

**它不是什么**（免得一开始就想歪）：不是编辑器插件，也不是聊天窗口；**不会自动把代码合进主干**，每份改动都要你亲手批准；它需要一台能跑 `claude` 命令的机器，你在自己电脑上发指令、服务端在那台机器上执行。

> 这文件只讲怎么用。为什么这么设计、技术契约怎么核实的，见 [`docs/architecture.md`](docs/architecture.md)；踩过的坑在 [`docs/pitfalls.md`](docs/pitfalls.md)。

---

## 先跑起来：5 分钟

### 开始前，确认这台机器上有三样东西

| 检查 | 命令 | 期望 |
|---|---|---|
| Python 3.11+ | `python --version` | 3.11 或更高 |
| git | `git --version` | 有输出即可 |
| Claude Code CLI | `claude --version` | 有输出，且这个账号已登录 |

这三样必须在**跑服务端的那台机器**上。`claude` 要能在 PATH 里直接找到，并且能正常调用模型——任务执行时不再有任何人工授权环节。

### 第 1 步 · 安装

```bash
pip install -e .
```

装出两个命令：`codingstorm`（服务端）和 `cs`（发指令用的客户端）。

### 第 2 步 · 启动服务

```bash
python -m codingstorm.app
```

不用先写配置文件，全部有默认值。打开 **http://127.0.0.1:8788/** 能看到界面，或者：

```bash
curl http://127.0.0.1:8788/api/health
```

有 JSON 返回就是活的。启动时它会先做一次崩溃对账——清理上次遗留的进程、把被中断的任务标出来。

> 想改端口、并发数等配置，再 `cp codingstorm.example.toml codingstorm.toml` 然后 `python -m codingstorm.app --config codingstorm.toml`。配置项见[配置详解](#配置详解)。

### 第 3 步 · 注册一个项目

浏览器打开 http://127.0.0.1:8788/ ，在左栏「项目」里填个名字（比如 `demo`）点注册。或者用命令：

```bash
curl -X POST http://127.0.0.1:8788/api/projects \
  -H 'content-type: application/json' \
  -d '{"name":"demo"}'
```

**只要一个名字就行。** 平台会自动建好一个空的 git 仓库；第一次提交任务时再给它造初始提交，AI 就在上面写第一版。

注册时会当场校验，不合格返回 400 并说明原因。

> `cs` 没有注册项目的子命令，项目只在页面或接口里注册这一次。

### 第 4 步 · 提交第一个任务

```bash
cs submit demo "写一个快速排序函数和对应测试"
```

你会看到 `已入队 <任务id>（排队中）：写一个快速排序函数和对应测试`，**命令立刻结束**，不用等 AI。这时候可以去干别的。

### 第 5 步 · 回来看结果、决定要不要

```bash
cs ls                    # 看状态；跑到 awaiting_review（待审阅）就是干完了
cs show <任务id>          # 详情、失败原因、token 用量
cs events <任务id>        # 它一步步做了什么
cs diff <任务id>          # 改了哪些文件、哪些行
cs approve <任务id>       # 满意 → 合入主干
cs discard <任务id>       # 不满意 → 丢掉，主干纹丝不动
```

同样的操作在网页上点也行：任务详情页有「改动」页签，diff 下面就是批准/丢弃按钮。批准之后主干上就有代码了。

**到这一步你已经会用 codingstorm 了。** 后面全是按需查阅的内容。

---

## 两条路：从零开始，还是接一个已有仓库

| | 你要做什么 | 什么时候用 |
|---|---|---|
| **全新项目**（上面的第 3 步） | 只填名字，其余平台包办 | 想让 AI 从零写个项目、或先试一试 |
| **已有项目** | 先把仓库做成裸克隆放到服务器上，注册时填路径 | 已经有一份代码要接着改 |

**接入已有仓库**：

```bash
git clone --bare <你的仓库> ~/codingstorm/repos/demo.git
```

然后注册时把路径带上：

```bash
curl -X POST http://127.0.0.1:8788/api/projects \
  -H 'content-type: application/json' \
  -d '{"name":"demo","repo_path":"/home/ubuntu/codingstorm/repos/demo.git"}'
```

**为什么必须是裸克隆**：批准时平台用 `update-ref` 推进分支引用，它不会同步工作区的文件。非裸仓库第一次批准后，主工作区会和 HEAD 脱节，`git status` 看起来像「所有文件都被删了」。代码会给非裸仓库打一条告警。平台还会往仓库的 `info/exclude` 写一份排除列表（`__pycache__/` 等）——它是未跟踪的本地文件，不进你的 git 历史，但能防止 AI 跑代码产生的字节码缓存被一起提交。

注册项目的全部字段：

| 字段 | 默认 | 说明 |
|---|---|---|
| `name` | **必填** | 字母、数字和 `._-`，全局唯一。以后 `cs submit <这个名字>` 用它 |
| `repo_path` | `{root}/repos/<name>.git` | 留空就用它，不存在时自动 `git init --bare` 建空仓库 |
| `target_branch` | `main` | 仓库已有提交时，这条分支必须存在 |

**从零起的项目，之后想拉到本地接着开发**：从服务器 clone，不要本地 `git init` 再 push。

```bash
git clone tencent:/home/ubuntu/codingstorm/repos/demo.git demo
```

平台造的那条初始提交是仓库的根，本地另起炉灶的仓库跟它没有共同祖先，push 会被拒。反过来，接已有项目时平台不会去动已经有分支的仓库，只给真正空的仓库造起点。唯一会被挡下的情况是：仓库里有分支、但没有你配的 `target_branch` 那条——提交任务会返回 409 并列出实际存在的分支。

---

## 卡住了？先看这里

| 症状 | 最常见的原因 | 怎么查 / 怎么办 |
|---|---|---|
| `cs` 报「连不上 http://127.0.0.1:8788」 | 服务端没起，或起了别的端口 | `curl http://127.0.0.1:8788/api/health`；换过端口就设 `CODINGSTORM_API` 或用 `cs --base <地址>` |
| 任务一直是「排队中」 | 执行窗口没开 / 并发占满 / 启动服务时带了 `--no-scheduler` | `cs window` 看窗口；`cs ls` 看别的任务是不是在跑 |
| 提交任务立刻变「失败」 | `claude` 没登录、不在 PATH，或者模型调用报错 | 看 `{root}/logs/<项目>/<任务id>.stderr`，这是 `claude` 自己的输出 |
| 提交任务返回 409，说找不到分支 | 仓库有分支，但 `target_branch` 配错了 | 错误信息里列了实际存在的分支，注册时改成那条 |
| 批准返回 409，说与主干有冲突 | 主干已经前进，这条分支 rebase 不上 | 不会硬合；需要人工处理冲突 |
| 网页看着像没更新 | 浏览器缓存了旧的 JS | 强制刷新一次（页面外壳已加 no-cache） |
| 界面上的改动没出现在下一个任务里 | 上一个任务还没批准 | 未合并的改动对后续任务不可见，先批准 |

服务端日志在启动方式决定的地方（部署脚本写 `~/codingstorm/app.log`），原始事件流在 `{root}/logs/<项目>/<任务id>.ndjson`。

---

## 按需要查

| 我想… | 看哪一节 |
|---|---|
| 查某个命令怎么写 | [常用命令](#常用命令) |
| 看懂任务现在处于什么状态 | [任务状态](#任务状态) |
| 让 AI 在原有改动上接着改 | [多轮对话](#多轮对话) |
| 修 bug 时先出方案、等我点头再动手 | [严格模式](#严格模式phase-8a) |
| 让它每天自动跑 / 只在半夜跑省钱 | [定时任务与执行窗口](#定时任务与执行窗口) |
| 了解界面上每个区域 | [Web UI](#web-ui) |
| 让 AI 记住项目的构建命令和约定 | [上下文](#context) |
| 改端口、并发、超时等配置 | [配置详解](#配置详解) |
| 知道它能碰什么、不能碰什么 | [安全边界](#安全边界) |
| 看花了多少钱 | [成本记账](#成本记账) |
| 找数据库、日志、工作区在哪 | [数据目录](#数据目录) |
| 自己写脚本调接口 | [HTTP 接口](#http-接口) |
| 部署到服务器 / 跑测试 | [部署](#部署) · [开发](#开发) |
| 知道有哪些做不到的 | [已知限制](#已知限制) |

---

## 常用命令

最常敲的六条：

```bash
cs submit <项目> "<标题>"   # 发一个任务，立刻返回
cs ls                      # 看所有任务的状态
cs diff <任务id>            # 看改动
cs say <任务id> "<追加的话>" # 让它接着改
cs approve <任务id>         # 批准合入
cs discard <任务id>         # 丢弃
```

完整清单：

```bash
cs projects                    # 有哪些项目
cs submit <项目> "<标题>" [--body "..."] [--kind requirement|instruction|bug|task] [--priority N] [--mode plain|strict]
cs ls [--project <名>] [--status <状态>]
cs show <任务id>               # 详情 + 每次尝试的 token 与成本
cs events <任务id>             # 执行过程（模型文本、工具调用、工具输出）
cs diff <任务id>               # 改动内容
cs stage <任务id>              # 严格模式的阶段、产物与等待令牌
cs say <任务id> "<追加的话>"    # 接着上一轮继续说（多轮对话）
cs approve <任务id>            # 批准并合入主干
cs discard <任务id>            # 丢弃
cs requeue <任务id>            # 重新入队
cs usage                       # 用量与成本

cs schedule ls [--project <名>]                # 有哪些定时任务
cs schedule add <项目> "<标题>" --daily 09:00
cs schedule add <项目> "<标题>" --weekly 03:00 --days mon,wed
cs schedule add <项目> "<标题>" --at "2026-12-01 02:00"
cs schedule run <id>           # 立刻跑一次（不影响原定排期）
cs schedule pause|resume <id>  # 停用 / 启用
cs schedule rm <id>            # 删除（已生成的任务留着）

cs window                      # 执行窗口现在开没开
cs window off | on             # 临时关闭 / 恢复窗口限制
cs window --project <名> always|inherit|custom --windows "09:00-18:00"
```

- **任务 id 支持唯一前缀**，打前几位就够；前缀匹配到多条会提示写长一点。项目参数同样接受名字或 id。
- 服务地址默认 `http://127.0.0.1:8788`，和服务端默认端口一致。换过端口就设环境变量 `CODINGSTORM_API`，或者把 `--base` **写在子命令之前**：`cs --base http://127.0.0.1:9000 ls`。
- `kind` 只是标签，不改变执行流程。`priority` 越大越先被领取，同优先级按提交时间先到先得。
- `cs ls` 最多返回 200 条（服务端上限）。
- **没有 `cs cancel`** —— 取消只能走接口，而且只有还在排队的任务能取消。

## 任务状态

| 状态 | 含义 | 能做什么 |
|---|---|---|
| `queued` | 排队中 | 取消 |
| `running` | 正在执行 | 等 |
| `awaiting_review` | 跑完了，等你看 diff | 批准 / 丢弃 / 继续对话 |
| `awaiting_decision` | 严格模式跑完诊断，等人发【进入开发】 | 发令牌 / 丢弃 |
| `merged` | 已合入主干（终态） | — |
| `discarded` | 已丢弃（终态） | — |
| `cancelled` | 排队时被取消（终态） | — |
| `failed` | 执行失败 | 重新入队 / 丢弃 / 继续对话 |
| `interrupted` | 被中断（平台重启等） | 重新入队 / 丢弃 |

失败原因在 `cs show <id>` 的「说明」一栏。

**批准是不可逆的**，而且会先尝试把你的分支 rebase 到最新主干；有冲突则返回 409，不会硬合。

`requeue` 重跑当前这一轮：**第一轮的任务会重建工作区**（旧的带着未提交的改动、且基于旧基点，留着只会误导）；**追加过消息的任务则接着上一轮跑**，不重建。

## 多轮对话

一个任务不是只能问一次。跑完（待审阅）或失败之后可以接着聊 —— **同一条任务、同一条分支、同一个会话**，AI 记得前面几轮做过什么：

```bash
cs say <任务id> "改成返回列表，不要返回单个值"
cs say <任务id> "再加个单元测试"
```

或者在 Web UI 的任务详情页「对话」页签里直接输入。

- **只有「待审阅」和「失败」能继续。** 已合并/已丢弃的任务工作区和分支都清掉了，接着聊没意义。
- **改动是累积的。** diff 始终是「主干 → 最新」，最后一次性批准或丢弃。
- **继续时不重建工作区**，直接从已有分支检出，所以前几轮的提交都在。
- **只发新那一句给模型**，不重复注入项目约定 —— 上下文已经在会话里了。
- 每继续一轮就多一次模型调用，而且会话越长输入 token 越多。

> 实现细节：接的是 `claude --resume <会话id>`，不用 `--continue`（后者按 cwd 找「最近的会话」，多任务共享目录时会串到别人的会话上）。另外写变更记录的「沉淀」是另一次独立调用、另一个会话，代码里按 `origin='task'` 过滤掉了，否则多轮对话会看起来「能答上来」实际接错了会话。

## 严格模式（Phase 8a）

默认关闭，用于修 bug 这类希望「先看方案再动代码」的任务。先在 `codingstorm.toml` 里设 `[agents] enabled = true`，然后：

```bash
cs submit demo "修复清理缓存弹窗的目录列表无法滚动" --kind bug --mode strict
cs stage <任务id>               # 看当前阶段、阶段产物清单、等待的令牌
cs say <任务id> "【进入开发】"   # 只有这个精确字符串能放行
```

它把一次执行拆成两段：先诊断 + 出方案（`diagnosis.md` / `idea.md`），停在 `awaiting_decision` 等人点头，之后才允许改产品代码；实现、净室评审、验证跑完，交回原来的审 diff 流程。人工介入仍是两个点（令牌 + `cs approve`），普通任务完全不受影响。

- **只有精确令牌能解锁**。「继续」「可以」「按这个做」以及带空格的变体一律 409；校验在服务端，不写进 prompt 求模型自觉。
- 阶段产物和 `state.json` 在 `{root}/runs/<项目>/<任务id>/`，**在 worktree 之外** —— 收尾的 `git add -A` 不会把它们提交进 diff。
- 挂起时不提交、不沉淀、不占并发位；平台重启后靠磁盘上的状态文件续跑，接着的是同一条 `--resume` 会话。
- 定时任务生成的任务一律 `plain`；`kind` 仍只是标签，决定流程的是 `mode`。
- 目前不提供截图 / 预览 / Compare 链接（没有浏览器与共享测试机），也不做自动批准。
- 设计依据与后续规划见 [`docs/phase8-agent-layer.md`](docs/phase8-agent-layer.md)。

## 定时任务与执行窗口

这两件事经常被当成一件，其实是两件：

| | 决定什么 | 配在哪 |
|---|---|---|
| **定时任务** | 什么时候**入队** | 界面右栏「定时任务」面板，或 `cs schedule` |
| **执行窗口** | 什么时候**能开始执行** | `codingstorm.toml` 的 `[scheduler.window]` |

所以「每天 9 点」的定时任务配上 00:30–08:30 的窗口，入队后要等到第二天凌晨才真正开跑 —— 这不是 bug，是省钱优先的必然结果。界面上会把这类任务标成「等窗口」。

### 执行窗口（谷时省钱）

中转服务的凌晨时段常有大幅折扣，白天提交、夜里跑便宜得多。

```toml
[scheduler.window]
enabled  = true
timezone = "Asia/Shanghai"        # 必须显式写，别靠系统时区
windows  = [["00:30", "08:30"]]   # 可配多段；start > end 表示跨天，如 22:00-06:00
```

- **只拦新任务的启动，不打断已经跑起来的。** 中途 kill 掉的钱不会退，重跑还要再花一遍。
- 窗口外排队中的任务，界面和 `cs ls` 都会标出来，免得看着像卡死了。
- **急事**：界面上横幅右边有「临时关闭窗口」，或者 `cs window off`。这是内存态开关，**平台重启就恢复** —— 急了关一次，忘了也不会一直烧钱。
- **单个项目可以例外**：横幅右边直接选，或者 `cs window --project <名> always`。

### 定时任务

三种规则，都不需要学 cron 表达式：

```bash
cs schedule add demo "每天跑一遍文档里的命令，把失效的改掉" --daily 09:00
cs schedule add demo "每周整理一次变更记录" --weekly 03:00 --days mon,wed
cs schedule add demo "一次性迁移" --at "2026-12-01 02:00"
```

- **每次触发是生成一条新任务**，不是复用同一条。一条任务对应一个分支、一个工作区、一份待审 diff。
- **错过就跳过，一律不补跑。** 轮询是一秒一次，只有平台停机才会迟到；迟到在 `misfire_grace_s`（默认 10 分钟）内的算准时，超过就跳过。一次性任务错过会标成「已错过」留在列表里，不静默消失 —— 可以自己重排或点「立即跑一次」。
- **产出和手工任务一样要人工审。** 所以「上一轮还没审」时界面会标红提醒：未合并的改动对后续任务不可见，本轮会切在旧主干上。
- 任务和定时任务的对应关系是 `tasks.schedule_id`，任务详情里能追回是哪条定时任务生成的。
- 删除定时任务**不会删掉它已经生成的任务** —— 那可能是待审的产物，跟着一起消失才是真的丢东西。

## Web UI

地址就是服务地址（默认 http://127.0.0.1:8788/）。左右两栏：**左栏**是项目列表（带注册表单）和项目上下文编辑区，**右栏**从上到下是：执行窗口横幅、任务队列、定时任务面板、任务详情。

- **执行窗口横幅**（启用窗口时才有）：显示当前开没开、还有多久开关，右边两个控件 —— 临时关闭窗口、以及当前项目的窗口例外。
- **定时任务面板**：列出当前项目的定时任务，每条能「立即跑一次 / 停用 / 删除 / 看上一轮」。
- 任务详情四个页签：**对话**（多轮往返）、**执行过程**（实时推送，工具调用渲染成可展开卡片）、**改动**（diff + 批准/丢弃按钮）、**阶段**（仅严格模式）。
- **文件**：左栏项目上下文里有个「文件」页签，能直接浏览仓库内容，不用 clone 下来。上面可以在「主干」和「本次任务」之间切 —— 后者用提交 sha 定位，所以任务合并之后也还能看当时改成了什么样。
- 路由是 hash 形式的 `#/task/<id>`，链接可分享、刷新回到原处。
- 实时靠 WebSocket，另有 2 秒轮询兜底；窗口状态另有 30 秒轮询。**时间一律按配置的时区显示**，不用浏览器时区。
- 没有构建步骤，也不依赖任何 CDN。

<a id="context"></a>

## 上下文：让 AI 记住项目约定

**每条任务的第一轮是全新会话**，跨任务的记忆靠文档。这些文件放在 `{root}/contexts/<项目名>/`，页面上可以直接编辑：

```
contexts/<项目名>/
  pointer.md      项目约定，每个任务都会全量注入。软上限 6KB
  journal.md      变更记录，任务结束后自动追加，只注入最近一段（默认 6000 字符）
  docs/
    INDEX.md      文档索引，会被注入
    其他文档.md    只登记在索引里，正文由 AI 需要时自己 Read
```

`pointer.md` 里只该写四类内容：带参数的构建/测试命令、AI 看不出来的非显然约束、项目专有工具的用法、每条任务都适用的硬规则。**不要写「这个项目是做什么的」「目录结构是怎样的」** —— AI 读代码就能得到，写进去只会稀释信噪比（实测这类内容反而让表现变差）。

模板和文件里的 HTML 注释在注入前会被剥掉，不占 token，可以放心当备注写。

`journal.md` 的每条记录带三个字段（写在注释里）：`source` 为什么改、`applicability` 何时适用、`expiry` 什么条件下可以删。

> ⚠️ **沉淀会让成本大致翻倍。** 它是每个任务额外的一次模型调用，而 Claude Code 的固定开销很高（实测一次约 2.5 万 input token）。成本敏感就设 `context.sediment = false` 关掉。

## 配置详解

先复制示例再改，启动时用 `--config` 指定：

```bash
cp codingstorm.example.toml codingstorm.toml
python -m codingstorm.app --config codingstorm.toml
```

不传 `--config` 时全部走默认值。常改的几项：

| 配置 | 默认 | 说明 |
|---|---|---|
| `root` | `~/codingstorm` | 所有运行时数据的位置 |
| `server.host` / `port` | `127.0.0.1` / `8788` | 默认只监听本机 |
| `scheduler.max_concurrent` | `2` | **由内存决定** —— 每个 Claude Code 进程约 200–400MB |
| `task.max_turns` | `50` | 不设上限的话，一个死循环 agent 能烧一整晚 |
| `task.wall_clock_timeout_s` | `3600` | 墙钟上限，超了整组 SIGTERM→SIGKILL |
| `task.idle_timeout_s` | `300` | 多久没有任何输出就判定卡死 |
| `context.sediment` | `true` | 任务结束后自动写变更记录（会多一次模型调用） |
| `agents.enabled` | `false` | 严格模式总开关，默认关；见[严格模式](#严格模式phase-8a) |
| `claude.guard_block_push` | `true` | 拦 `git push` 与改 remote |
| `scheduler.window.enabled` | `false` | 只在指定时段跑任务（谷时省钱） |
| `scheduler.misfire_grace_s` | `600` | 定时任务迟到多久还算「准时」 |

每项在 `codingstorm.example.toml` 里都有注释说明为什么这么设。启动参数还有 `--host` / `--port` 覆盖监听地址、`--no-scheduler` 只起 HTTP 不执行任务（调试用）、`--log-level`。

## 安全边界

任务以 `bypassPermissions` 全自动放行 —— 无人值守必须如此，否则它会在需要授权时挂住。边界靠 **PreToolUse hook** 拦（`permissions.deny` 在 bypass 模式下不可靠，hook 不受权限模式影响）：

- 默认拦 `git push` 和 `git remote add/set-url/remove/rename`
- 网络默认**放行**（`guard_block_network = true` 会连带挡掉正常的依赖安装，容易误伤）。打开后同时管住 Bash 里的网络命令和 `WebFetch` / `WebSearch` 工具
- `guard_extra_deny` / `guard_allow` 可以追加正则规则和白名单（白名单优先级最高）
- 每次工具调用都写审计日志（命令截断到 500 字符）

被拦下时模型会收到拒绝原因和规则来源，它会如实报告，而不是假装成功。

另一条必要防线是**别把重要的东西放在执行机上**：能力范围内它什么都能改，分支保护只保护得了代码。

## 成本记账

**不采信 `result.total_cost_usd`。** 走第三方中转时那是按另一端价目算的 —— 实测一句「Reply with exactly: OK」它报 0.26507 美元。成本只按 token × 你自己填的价目表算：

```bash
cp prices.example.toml ~/codingstorm/prices.toml
```

单价单位是**每百万 token**，模型名从任务详情的用量里能看到。没填的模型成本**留空**，界面如实显示「未配置价目表」—— 宁可显示未配置，也不给假数字。

`version` 会跟成本一起落库：**改价目表不会改写历史成本**，那等于篡改账本。

`cs usage` 把「执行任务」和「记录沉淀」分开统计 —— 沉淀是每个任务的固定开销，混进任务里就看不见了。

## 数据目录

启动后 `{root}`（默认 `~/codingstorm`）下会有：

```
codingstorm.db      SQLite（WAL）。项目、任务、尝试、事件、定时任务
contexts/<项目>/    pointer.md / journal.md / docs/（含 INDEX.md）
worktrees/<项目>/   每个任务一个临时工作区，批准或丢弃后清掉
runs/<项目>/<任务id>/   严格模式的 state.json 与阶段产物
logs/<项目>/<任务id>.ndjson   原始事件流（排错时看这个）
logs/<项目>/<任务id>.stderr   子进程 stderr
repos/              被调度的仓库。注册时留空 repo_path 就用这里，不存在会自动新建
```

运行时还需要的文件在 `{root}` 旁边（不进版本库）：`prices.toml`、`codingstorm.toml`。

**排错先看日志**：`.stderr` 是 `claude` 自己的输出；服务端日志在启动方式决定的地方（部署脚本写 `~/codingstorm/app.log`）。

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
| GET | `/api/tasks/{id}/stage` | 严格模式的阶段、产物清单与等待的令牌 |
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
| GET | `/api/schedules` | 列出定时任务（`?project_id=`） |
| GET / POST | `/api/projects/{id}/schedules` | 列出 / 新建定时任务 |
| GET / PATCH / DELETE | `/api/schedules/{id}` | 读 / 改（规则、启停、标题）/ 删 |
| POST | `/api/schedules/{id}/run` | 立刻跑一次（不动原定排期） |
| GET / POST | `/api/window` | 读窗口状态 / 临时关闭或恢复（`{"disabled": bool}`） |
| PUT | `/api/projects/{id}/window` | 项目级窗口覆盖（`{"mode": "inherit\|always\|custom", "windows": [...]}`） |

> 规则不合法、一次性任务的时间已经过去，都返回 **400** 并带上原因；`type` 不是 `once/daily/weekly` 是形状问题，由 pydantic 返回 **422**。

## 部署

`scripts/deploy.sh` 把代码推到执行机并重启：

```bash
./scripts/deploy.sh [ssh别名]     # 默认 tencent
```

它会上传代码、等旧进程真正退出、确认端口释放，再启动并校验「进程数恰好为 1 且健康检查通过」。

> ⚠️ **重启逻辑不能简化。** 早期版本只 `pkill` + `sleep 1` 就启新的 —— 旧进程收到 SIGTERM 后还要优雅退出（等运行中的任务收尾），根本来不及死，结果服务器上堆了 5 个进程，旧版本一直占着端口在服务。

首次部署的前置条件（脚本头部也有）：在目标机上建好 venv 并装上 `fastapi uvicorn websockets`，以及 `~/codingstorm/codingstorm.toml`。部署脚本用的端口是 **8788**，与 `cs` 默认值一致。

## 开发

```bash
pip install -e ".[dev]"
pytest -q          # 295 项
```

代码在 `codingstorm/`：`api.py`（HTTP）、`scheduler.py`（并发、执行窗口、定时触发与生命周期）、`runner.py`（拉起子进程、解析 NDJSON）、`workspace.py`（worktree 与合并）、`context.py`（上下文三层）、`agent_state.py`（严格模式状态机）、`timing.py`（窗口与定时规则的时间计算，纯函数）、`store.py` + `db.py`（数据层）、`cli.py`（`cs`）、`static/`（无构建步骤的前端）。

改架构前先读 `docs/architecture.md`，那里记录了已核实的技术契约和压测出来的实现约束 —— 不少看起来能简化的地方，简了就会踩坑（该文档第 5 节整节都是）。踩过的坑另见 [`docs/pitfalls.md`](docs/pitfalls.md)。

## 已知限制

- **默认只监听本机。** 要远程访问请自己加反向代理。
- **执行机内存决定并发上限。** 每个 Claude Code 进程约 200–400MB，超了会 OOM。
- **任务粒度太细不划算。** Claude Code 的固定开销很高（实测一句「OK」约 2.6 万 input token），一句话塞多个需求也不会更快 —— 一个任务一次执行。
- **任务之间看不到彼此未合并的改动**，这是设计的一部分，不是 bug。周期性的定时任务尤其容易踩：人不审，第二天那条仍然基于旧主干 —— 界面上会标出来提醒。
- **执行窗口的「临时关闭」不持久。** 重启就恢复限制。这是刻意的失败方向：宁愿你重新关一次，也不愿忘了恢复之后一直按全价烧钱。
- **定时规则只有「一次性 / 每天 / 每周几」**，没有完整 cron 表达式，也不支持「每隔 N 分钟」这类间隔触发。
- **不做分时段计价。** 只在谷时跑的话，价目表直接填谷时单价即可。
- 项目一旦注册只能通过数据库改（没有改和删的接口）；`enabled` 字段存在但界面没暴露。例外是窗口覆盖，它走 `PUT /api/projects/{id}/window` 和 `cs window --project`。
