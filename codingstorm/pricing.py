"""成本计算。

**为什么不能直接用 `result.total_cost_usd`**：那是 Claude Code 按 Claude 的价目算的。
我们走第三方中转（服务器上是 DeepSeek 端点），实际计费口径跟它不一样 ——
实测一句「Reply with exactly: OK」它报 0.26507 美元，而真实花费是另一个量级。

所以价目表由用户提供，并带一个 `version` 一起落库：**改了价目表不能改写历史成本**。
没配价目表时成本留空，界面上如实显示「未配置」，而不是拿一个假数字糊弄。

价目表格式（TOML，默认在 `{root}/prices.toml`）：

    version = "2026-09-18"

    [models."deepseek-v4-flash"]
    input = 0.14           # 每百万 token 的价格
    output = 0.28
    cache_read = 0.014
    cache_creation = 0.14
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("codingstorm.pricing")

MILLION = 1_000_000.0


@dataclass(frozen=True)
class ModelPrice:
    input: float
    output: float
    cache_read: float = 0.0
    cache_creation: float = 0.0


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


class PriceTable:
    def __init__(self, version: str, models: dict[str, ModelPrice]):
        self.version = version
        self.models = models

    @classmethod
    def empty(cls) -> "PriceTable":
        return cls(version="", models={})

    @classmethod
    def load(cls, path: Path | None) -> "PriceTable":
        if path is None or not Path(path).exists():
            return cls.empty()
        try:
            with Path(path).open("rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            log.warning("价目表读取失败（成本将留空）: %s", exc)
            return cls.empty()

        models: dict[str, ModelPrice] = {}
        for name, spec in (data.get("models") or {}).items():
            try:
                models[name] = ModelPrice(
                    input=float(spec["input"]),
                    output=float(spec["output"]),
                    cache_read=float(spec.get("cache_read", spec["input"])),
                    cache_creation=float(spec.get("cache_creation", spec["input"])),
                )
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("价目表里 %s 的配置无效，跳过: %s", name, exc)
        return cls(version=str(data.get("version") or "unknown"), models=models)

    @property
    def configured(self) -> bool:
        return bool(self.models)

    def cost(self, model: str | None, usage: Usage) -> float | None:
        """返回美元成本；价目表里没有这个模型则返回 None（**不要瞎猜**）。"""
        if model is None:
            return None
        price = self.models.get(model)
        if price is None:
            return None
        return (
            usage.input_tokens * price.input
            + usage.output_tokens * price.output
            + usage.cache_read_tokens * price.cache_read
            + usage.cache_creation_tokens * price.cache_creation
        ) / MILLION
