# Phase 0 验证脚本

在**执行机**（跑 Claude Code 的服务器）上运行。结论见 [`docs/phase0-report.md`](../../docs/phase0-report.md)。

## 用法

```bash
# 1. 拷到执行机
scp -r scripts/spike tencent:~/codingstorm/

# 2. 跑全套
ssh tencent 'chmod +x ~/codingstorm/spike/*.sh && ~/codingstorm/spike/spike.sh'

# 3. 单个测试
ssh tencent '~/codingstorm/spike/spike.sh hook'
```

产物（NDJSON、日志）留在 `~/codingstorm/spike/out/`，可用 `analyze.py` 解析：

```bash
python3 analyze.py ~/codingstorm/spike/out/hook.ndjson
```

## 各项测试

| 参数 | 验证内容 |
|---|---|
| `smoke` | 基本连通性 + 事件序列 |
| `verbose` | `--verbose` 是否必需 |
| `add-dir` | 能否读工作目录之外的文件 |
| `max-turns` | `--max-turns` 用尽时的行为 |
| `lines` | 行长分布（评估 64 KiB PIPE 风险） |
| `hook` | `bypassPermissions` 下 PreToolUse hook 是否触发 |
| `all` | 全部（默认） |

## 注意事项

- **所有调用都带 `</dev/null`。** 不带的话 `claude -p` 会继承调用者的 stdin，把脚本内容读成上下文 —— Phase 0 踩过这个坑。
- `hook` 测试会在 `out/` 下建临时裸仓库当远端，不碰网络。
- 跑之前确认 `~/.claude/settings.json` 的凭证有效，否则会看到 10 次退避重试（约 3 分钟才失败）。
