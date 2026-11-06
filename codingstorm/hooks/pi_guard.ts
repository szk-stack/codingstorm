/**
 * codingstorm 的 Pi 边界拦截扩展。
 *
 * 为什么需要这一层：Pi 没有权限系统，也没有 Claude Code 的 PreToolUse hook，
 * 默认就按启动用户的权限执行所有工具。能阻断工具调用的只有扩展的 `tool_call` 事件。
 *
 * 判定逻辑不在这里重写 —— 而是把 bash 命令按 Claude hook 的协议喂给同一个
 * `guard.py`，退出码 2 视为阻断。这样两边规则永远只有一份（guard/guard.json）。
 *
 * 由 GuardInstaller 生成到 {root}/guard/pi-guard.ts，用 `pi -e <path>` 挂载。
 * 路径通过环境变量注入，避免把 Windows 盘符和引号写死进生成的代码里。
 */

import { spawnSync } from "node:child_process";

export default function (pi: any) {
  pi.on("tool_call", async (event: any) => {
    // 只有 bash 能改仓库/推远端；read/write/edit 不经过命令匹配
    if (event.toolName !== "bash") return;

    const python = process.env.CODINGSTORM_GUARD_PYTHON;
    const script = process.env.CODINGSTORM_GUARD_SCRIPT;
    // 没注入配置就放行：扩展本身出问题不该让所有任务都跑不动
    if (!python || !script) return;

    const payload = {
      tool_name: "Bash",
      tool_input: { command: String(event.input?.command ?? "") },
      session_id: process.env.CODINGSTORM_TASK_ID ?? null,
    };

    const proc = spawnSync(python, [script], {
      input: JSON.stringify(payload),
      encoding: "utf-8",
      timeout: 10000,
      env: process.env,
    });

    // 拦截器自己崩了按「放行」处理 —— 和 guard.py「读不懂就不拦」保持一致。
    // 安全边界主要靠执行机隔离，而不是靠一个可能坏掉的包装层。
    if (proc.error) return;

    if (proc.status === 2) {
      const reason = (proc.stderr || "").trim() || "被 codingstorm 的边界规则拦下";
      return { block: true, reason };
    }
  });
}
