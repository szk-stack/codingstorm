"""模型候选目录：给前端的项目级模型下拉用。

CodeBuddy 的模型 id 变得比平台快，硬编码一张表很快就会过期，所以优先
**直接问本机 CLI**：`codebuddy --help` 里那行 `--model <model> ... Currently
supported: (...)` 就是官方列表。失败（没装 / 版本改了格式 / 超时）再退到内置兜底，
用户也可以在 `[codebuddy] models` 里写死一份自己的清单。
"""

from __future__ import annotations

import re
import subprocess
from functools import lru_cache

from codingstorm.agents.base import resolve_executable
from codingstorm.config import Config

# CLI 不可用时的兜底，都是 2.181.0 的帮助里确认过的名字。
FALLBACK_MODELS: tuple[str, ...] = (
    "deepseek-v4.1-flash",
    "deepseek-v4-pro",
    "glm-5.3",
    "glm-5.3-flash",
    "kimi-k2.8-preview",
    "fast-model",
    "balanced-model",
    "deep-model",
)

_HELP_TIMEOUT_S = 8
# 惰性匹配到第一个 Currently supported，避免把后面别的开关也吞进来
_MODEL_LIST_RE = re.compile(
    r"--model\s+<model>[\s\S]*?Currently supported:\s*\(([^)]*)\)"
)


@lru_cache(maxsize=8)
def _from_cli(binary: str) -> tuple[str, ...]:
    """跑一次 `codebuddy --help` 抠出模型列表；任何失败都返回空元组。

    进程内只解析一次 —— CLI 不会在服务运行途中自己升级，重启服务即可刷新。
    用 capture_output 而不是管道串命令，避免 help 输出把事件循环堵住。
    """
    try:
        exe = resolve_executable(binary)
        proc = subprocess.run(
            [exe, "--help"],
            capture_output=True,
            timeout=_HELP_TIMEOUT_S,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return ()
    text = proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")
    match = _MODEL_LIST_RE.search(text)
    if not match:
        return ()
    names = (part.strip() for part in match.group(1).split(","))
    # 去重且保序；过滤掉空项，别让下拉里多一个空选项
    return tuple(dict.fromkeys(n for n in names if n))


def available_models(config: Config) -> tuple[list[str], str]:
    """返回 `(候选模型列表, 来源)`，来源是 config / cli / builtin。

    同步函数：里面会起子进程，调用方要丢到线程里（见 api.runtime_models）。
    """
    configured = [m.strip() for m in config.codebuddy.models if m.strip()]
    if configured:
        return list(dict.fromkeys(configured)), "config"
    cli = list(_from_cli(config.codebuddy.binary))
    if cli:
        return cli, "cli"
    return list(FALLBACK_MODELS), "builtin"
