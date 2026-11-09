"""
envconfig.py —— 启动时把环境变量灌进 config，并统一「数据目录」

为什么需要它（容器化部署时踩到的三件事）：

  1. config.py 被 .gitignore 排除，容器里 clone 下来根本没有这个文件，
     一 import 就 ModuleNotFoundError。所以缺文件时，拿 config.example.py
     当默认值加载，登记成 sys.modules["config"]，后面所有模块照常 import。
  2. 换 API Key / 端口 / 开关不该重建镜像。用 MYWHEEL_ 前缀的环境变量
     覆盖白名单里的配置项；只放开白名单，避免拼错名字悄悄生效。
  3. data.jsonl / traces.jsonl / api_key.json / uploads / bili_cookie.json
     原本全写项目根，容器一重建就没了。统一走 data_dir()，
     用 MYWHEEL_DATA_DIR 指到宿主机挂进来的目录。

优先级：环境变量 > 界面保存的 api_key.json > config.py > config.example.py
本机（有 config.py、没设任何 MYWHEEL_*）行为和加这个文件之前完全一致。

★ 每个入口（server.py / cli.py）必须在「任何 import config 之前」调用 apply()，
  否则别的模块先 import config 就绕过兜底了。
"""

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PREFIX = "MYWHEEL_"
DATA_DIR_ENV = "MYWHEEL_DATA_DIR"

# 老本机 config.py 里没有的新配置项：先补默认值，环境变量才覆盖得到
_DEFAULTS = {"HOST": "127.0.0.1"}

# 只允许覆盖这些名字。类型按 config 里原本的值推断（bool/int/float/str），
# 不维护第二张类型表；列表类配置（KNOWLEDGE_SOURCES 等）暂不开放注入。
ALLOWED = frozenset({
    "HOST", "PORT", "API_KEY", "API_URL", "MODEL", "VISION_MODEL",
    "SYSTEM_PROMPT", "SUBAGENT_PROMPT",
    "ENABLE_TOOLS", "MAX_TOOL_ROUNDS", "ENABLE_EXEC",
    "ENABLE_VISION", "MAX_IMAGE_MB",
    "ENABLE_COMPACT", "CONTEXT_WINDOW", "COMPACT_THRESHOLD_RATIO",
    "KEEP_RECENT_MESSAGES", "ENABLE_USAGE_STATS", "ENABLE_SUBAGENT",
    "ENABLE_KB", "KB_CHUNK_MAX", "KB_CHUNK_OVERLAP", "KB_TOP_K",
    "KB_TITLE_WEIGHT", "KB_MAX_OUTPUT",
    "DASHSCOPE_API_KEY", "EMBED_MODEL", "EMBED_DIM", "EMBED_BATCH",
    "EMBED_URL", "RECALL_TOP",
    "ENABLE_VIDEO", "VIDEO_BILI_COOKIE", "VIDEO_MAX_MINUTES",
    "ASR_BACKEND", "VOICE2TEXT_URL", "VOICE2TEXT_EXE", "VOICE2TEXT_AUTOSTART",
    "VOICE2TEXT_MODEL", "ASR_MODEL_SIZE", "ASR_MODEL_DIR", "ASR_DEVICE",
    "ASR_COMPUTE_TYPE", "ASR_HOTWORDS", "ASR_FIXES", "HF_ENDPOINT",
})

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _ensure_config():
    """保证 config 模块可用：本机读 config.py，容器里拿 config.example.py 兜底。"""
    if "config" in sys.modules:
        return sys.modules["config"]
    try:
        import config  # noqa: F401
        return sys.modules["config"]
    except ModuleNotFoundError:
        pass

    spec = importlib.util.spec_from_file_location("config", ROOT / "config.example.py")
    if spec is None or spec.loader is None:
        raise ImportError("找不到 config.py，也无法从 config.example.py 加载默认配置")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["config"] = mod     # 先登记，防止加载过程中绕回来
    spec.loader.exec_module(mod)
    return mod


def _coerce(sample, raw):
    """按配置项原本的类型，把环境变量字符串转回去。"""
    if isinstance(sample, bool):
        v = raw.strip().lower()
        if v in _TRUE:
            return True
        if v in _FALSE:
            return False
        raise ValueError(f"布尔值只能是 1/0/true/false/yes/no/on/off，收到：{raw!r}")
    if isinstance(sample, int):
        return int(raw)
    if isinstance(sample, float):
        return float(raw)
    return raw


def is_set(name):
    """这个配置项是否被环境变量显式指定过（keyring 用来判优先级）。"""
    return (PREFIX + name) in os.environ


def apply():
    """入口文件在最顶部调用一次：补齐 config + 灌环境变量 + 备好数据目录。"""
    cfg = _ensure_config()
    for name, default in _DEFAULTS.items():
        if not hasattr(cfg, name):
            setattr(cfg, name, default)

    for name in ALLOWED:
        raw = os.environ.get(PREFIX + name)
        if raw is None or not hasattr(cfg, name):
            continue
        setattr(cfg, name, _coerce(getattr(cfg, name), raw))

    try:
        data_dir().mkdir(parents=True, exist_ok=True)
    except OSError:
        pass       # 目录不可写就让后续真正写文件的地方报错，别在 import 阶段炸
    return cfg


def data_dir():
    """所有「会变的数据」都落这里；默认仍是项目根，本机零变化。"""
    raw = os.environ.get(DATA_DIR_ENV, "").strip()
    return Path(raw).expanduser().resolve() if raw else ROOT


def uploads_dir():
    return data_dir() / "uploads"
