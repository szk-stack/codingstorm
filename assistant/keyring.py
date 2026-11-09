"""
keyring.py —— 在界面上配置的大模型 API Key，单独存一份

为什么不直接改 config.py：
  · config.py 是代码，用正则去替换引号里的值很容易误伤（注释里也有引号），
    而且它已经被 .gitignore 排除，本机那份还带着用户自己填的 Key，不该被程序改写。
  · 界面保存的值写进 api_key.json（同样进 .gitignore），和代码彻底分开。

为什么「保存后立即生效」：
  llm.py / cli.py 都是运行时读 `config.API_KEY`（模块属性，不是 from-import 的副本），
  所以保存时直接给 `config.API_KEY` 赋值即可，不用重启、不用 reload。

安全：
  status() 只回「有没有配」+ 一串纯星号，**绝不把明文 Key 发回浏览器**。
  前端拿到的是掩码，想改 Key 只能重新输入一份新的 POST 过来。
"""

import json
from pathlib import Path

import config
import envconfig

# 界面保存的 Key 单独放这里（已加入 .gitignore）
# ★ 跟数据目录走：容器里挂在宿主机上，重建容器不会把界面配的 Key 弄丢。
KEY_FILE = envconfig.data_dir() / "api_key.json"

# 固定长度的纯星号：连头尾字符都不泄露，长度也不随真实 Key 变化
MASK = "•" * 16


def load_into_config():
    """服务启动时调用：把上次在界面里保存的 Key 加载成当前生效值。

    文件不存在 / 内容坏了就安静跳过——这时用 config.py 里原本的值。
    优先级：MYWHEEL_API_KEY（部署时显式注入）> api_key.json > config.py。
    """
    if envconfig.is_set("API_KEY"):
        return   # 环境变量是最高优先级，别被数据卷里的旧 Key 覆盖回去
    if not KEY_FILE.exists():
        return
    try:
        saved = json.loads(KEY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    key = (saved.get("api_key") or "").strip()
    if key:
        config.API_KEY = key


def _source():
    """这个 Key 是从哪儿来的（只用于界面上一句提示，不含敏感信息）。"""
    return "saved" if KEY_FILE.exists() else "config"


def status():
    """给前端的状态：只有「配没配」+ 星号掩码，永远不含明文。"""
    key = (getattr(config, "API_KEY", "") or "").strip()
    if not key:
        return {"configured": False, "masked": "", "source": "none"}
    return {"configured": True, "masked": MASK, "source": _source()}


def save(raw):
    """保存新 Key：写文件 + 让当前进程立刻生效。空值 / 掩码串直接拒绝。"""
    key = (raw or "").strip()
    if not key:
        raise ValueError("API Key 不能为空")
    # 防御：万一前端把显示用的星号掩码当新 Key 提交回来，不能把真 Key 覆盖掉
    if set(key) <= {"•", "*", "·"}:
        raise ValueError("这是一串掩码，不是 API Key；请点开输入框重新填写")

    KEY_FILE.write_text(
        json.dumps({"api_key": key}, ensure_ascii=False), encoding="utf-8"
    )
    try:
        KEY_FILE.chmod(0o600)   # 能生效的平台就收紧权限，Windows 上失败也无所谓
    except OSError:
        pass

    config.API_KEY = key        # ★ 立刻生效：llm.py 每次请求都读的是这个属性
    return status()
