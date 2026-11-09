"""把边界拦截规则装到执行机上。

Claude Code 用 **PreToolUse hook**：`--permission-mode bypassPermissions` 会绕过
所有权限检查，所以 `permissions.deny` 规则不可靠，边界只能靠 hook（它不受权限
模式影响）。装配方式用 `--settings`（实测是**合并**进用户配置，不是替换）。

Pi 没有权限系统、也没有 PreToolUse hook，只有扩展的 `tool_call` 事件能阻断调用。
所以同一个 `guard.py` 会被包一层 TypeScript 扩展（`pi-guard.ts`），用 `-e` 挂上；
判定逻辑仍然只有一份，避免两边规则漂移。

钩子和规则都写在 `{root}/guard/` 下，codingstorm 生成，用户可改。
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from codingstorm.config import Config

log = logging.getLogger("codingstorm.guard")

HOOK_SOURCE = Path(__file__).parent / "hooks" / "guard.py"
PI_EXTENSION_SOURCE = Path(__file__).parent / "hooks" / "pi_guard.ts"


@dataclass(frozen=True)
class GuardPaths:
    root: Path
    script: Path
    rules: Path
    settings: Path
    codebuddy_settings: Path
    audit_log: Path


class GuardInstaller:
    def __init__(self, config: Config):
        self.config = config

    @property
    def paths(self) -> GuardPaths:
        root = self.config.root / "guard"
        return GuardPaths(
            root=root,
            script=root / "guard.py",
            rules=root / "guard.json",
            settings=root / "settings.json",
            codebuddy_settings=root / "codebuddy-settings.json",
            audit_log=root / "audit.log",
        )

    def install(self) -> Path | None:
        """落盘 Claude Code 的 settings（hooks + 可选 env），返回路径。

        两种情况都要生成这个文件：

        - 边界拦截开启 → 写 PreToolUse hook；
        - 配了 `[claude.env]` → 写 env 覆盖（`--settings` 优先级高于用户设置，
          用来绕开 CC Switch 之类只在本地起代理的工具）。
        """
        cfg = self.config.claude
        if not cfg.guard_enabled and not cfg.env:
            return None

        p = self.paths
        p.root.mkdir(parents=True, exist_ok=True)

        settings: dict[str, object] = {}
        parts: list[str] = []

        if cfg.guard_enabled:
            # 因为 guard.py 用 `python3 <script>` 调用，这个 shebang 只是给人看的
            shutil.copyfile(HOOK_SOURCE, p.script)
            self._write_rules()
            settings.update(self._hook_settings())
            parts.append("边界拦截钩子")

        if cfg.env:
            settings["env"] = dict(cfg.env)
            parts.append("env 覆盖")

        p.settings.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        log.info("已装配 Claude settings（%s）：%s", " + ".join(parts), p.settings)
        return p.settings

    def install_codebuddy_guard(self) -> Path | None:
        """落盘 CodeBuddy 的 hook settings（未启用则 None）。

        CodeBuddy 的 headless 模式兼容 Claude Code 的 `--settings` 和 PreToolUse
        hook，但**不能**把 `[claude.env]` 混进来 —— 那是 Claude 的 provider 配置，
        会让 CodeBuddy 用错端点。所以单独生成一份只含 hooks 的文件。
        """
        if not self.config.claude.guard_enabled:
            return None

        p = self.paths
        p.root.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(HOOK_SOURCE, p.script)
        self._write_rules()
        p.codebuddy_settings.write_text(
            json.dumps(self._hook_settings(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        log.info("已装配 CodeBuddy 边界拦截钩子：%s", p.codebuddy_settings)
        return p.codebuddy_settings

    def _hook_settings(self) -> dict[str, object]:
        # 用解释器显式调用，不依赖脚本的可执行位和 shebang
        command = f"{sys.executable} {self.paths.script}"
        return {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": command}]},
                    {"matcher": "WebFetch", "hooks": [{"type": "command", "command": command}]},
                ]
            }
        }

    def install_pi_guard(self) -> Path | None:
        """落盘 Pi 的 `tool_call` 扩展（未启用则 None）。

        Pi 的扩展在进程内执行，所以这里只生成一个薄包装：把 bash 命令按 Claude
        hook 的协议喂给 `guard.py`，退出码 2 就阻断。规则文件是同一份。
        """
        if not self.config.claude.guard_enabled:
            return None

        p = self.paths
        p.root.mkdir(parents=True, exist_ok=True)
        # 扩展要调 guard.py 做判定；只启用 Pi 时也必须把它落盘
        shutil.copyfile(HOOK_SOURCE, p.script)
        self._write_rules()
        shutil.copyfile(PI_EXTENSION_SOURCE, p.root / "pi-guard.ts")
        log.info("已装配 Pi 边界拦截扩展：%s", p.root / "pi-guard.ts")
        return p.root / "pi-guard.ts"

    def _write_rules(self) -> None:
        cfg = self.config.claude
        p = self.paths
        p.rules.write_text(
            json.dumps(
                {
                    "block_push": cfg.guard_block_push,
                    "block_remote_change": True,
                    "block_network": cfg.guard_block_network,
                    "extra_deny": list(cfg.guard_extra_deny),
                    "allow": list(cfg.guard_allow),
                    "audit_log": str(p.audit_log),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
