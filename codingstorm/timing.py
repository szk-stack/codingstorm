"""执行窗口与定时规则的时间计算。

全是纯函数 —— 不碰数据库、不读配置对象。时间逻辑最容易出隐蔽错误
（时区差 8 小时、跨天窗口差一天、边界闭开），必须能单独测。

**时间一律显式带时区**，绝不用 `datetime.now()` 取本地时间。执行机的系统时区
可能是 UTC，而用户说的「每天 9 点」指的是自己所在时区的 9 点。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

# 星期别名。用户写 mon/wed 比写 0/2 直观，存进库里也一眼能读懂。
WEEKDAYS = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}
WEEKDAY_LABEL = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 一次性任务在库里就是这个形状；只到分钟，秒对定时任务没有意义
_ISO_MINUTE = "%Y-%m-%dT%H:%M"


class TimingError(ValueError):
    """规则或窗口写错了。调用方应当把它变成 400，而不是让它冒到调度循环里。"""


# ---------- UTC 时间串 ----------


def iso_utc(dt: datetime) -> str:
    """转成库里统一的 UTC 时间串。

    **格式必须和 `store.utcnow()` 完全一致** —— 到期判断是拿字符串比大小的，
    格式一旦不同（比如 `+00:00` 和 `Z`），比较结果就是错的。
    """
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_utc(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def load_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception as exc:  # ZoneInfoNotFoundError 及其它
        raise TimingError(f"认不出时区 {name!r}：{exc}") from exc


# ---------- 执行窗口 ----------


@dataclass(frozen=True, slots=True)
class Window:
    """一段允许执行的时间。`start > end` 表示跨天（如 22:00–06:00）。"""

    start: time
    end: time

    @property
    def crosses_midnight(self) -> bool:
        return self.start > self.end

    def contains(self, moment: time) -> bool:
        """闭开区间：`[start, end)`。08:30:00 整不在 00:30–08:30 里。"""
        if self.start == self.end:
            return False
        if self.crosses_midnight:
            return moment >= self.start or moment < self.end
        return self.start <= moment < self.end

    def as_pair(self) -> list[str]:
        return [self.start.strftime("%H:%M"), self.end.strftime("%H:%M")]


def parse_time(raw: str) -> time:
    text = str(raw).strip()
    try:
        hour, minute = text.split(":")
        return time(int(hour), int(minute))
    except Exception as exc:
        raise TimingError(f"时间要写成 HH:MM，收到的是 {raw!r}") from exc


def parse_windows(raw: Any) -> list[Window]:
    """把 [[\"00:30\", \"08:30\"]] 这种配置解析成 Window 列表。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise TimingError("windows 要写成列表，例如 [[\"00:30\", \"08:30\"]]")
    windows = []
    for item in raw:
        if isinstance(item, dict):  # TOML 里也可以写 [[scheduler.window.windows]]
            item = [item.get("start"), item.get("end")]
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise TimingError(f"每段窗口要有起止两个时刻，收到的是 {item!r}")
        start, end = parse_time(item[0]), parse_time(item[1])
        if start == end:
            raise TimingError(f"窗口的起止时刻相同（{start:%H:%M}）—— 那就等于不限制，删掉这段即可")
        windows.append(Window(start, end))
    return windows


def is_open(now: datetime, windows: list[Window]) -> bool:
    """没有窗口配置 = 不限制（返回 True），这样默认配置就是不改变原有行为。"""
    if not windows:
        return True
    clock = now.time()
    return any(w.contains(clock) for w in windows)


def next_open(now: datetime, windows: list[Window]) -> datetime | None:
    """下一次窗口开启的时刻；已经开着则返回 None。"""
    if not windows or is_open(now, windows):
        return None
    closes = [
        datetime.combine(now.date() + timedelta(days=offset), w.start, now.tzinfo)
        for w in windows
        for offset in (0, 1)
    ]
    future = [c for c in closes if c > now]
    return min(future) if future else None


def next_close(now: datetime, windows: list[Window]) -> datetime | None:
    """当前这段窗口什么时候关；现在不在窗口内则返回 None。

    跨天窗口要分清落在哪一段：22:00–06:00，23 点看到的是「明天 6 点关」，
    凌晨 2 点看到的是「今天 6 点关」。
    """
    if not windows:
        return None
    clock = now.time()
    moments = []
    for w in windows:
        if not w.contains(clock):
            continue
        if w.crosses_midnight and clock >= w.start:
            moments.append(datetime.combine(now.date() + timedelta(days=1), w.end, now.tzinfo))
        else:
            moments.append(datetime.combine(now.date(), w.end, now.tzinfo))
    return min(moments) if moments else None


# ---------- 项目级覆盖 ----------


@dataclass(frozen=True, slots=True)
class WindowOverride:
    """项目对全局窗口的覆盖。None 就是完全继承全局。"""

    mode: Literal["always", "custom"]
    windows: tuple[Window, ...] = ()

    def as_json(self) -> str:
        payload: dict[str, Any] = {"mode": self.mode}
        if self.mode == "custom":
            payload["windows"] = [w.as_pair() for w in self.windows]
        return json.dumps(payload, ensure_ascii=False)


def parse_override(raw: str | None) -> WindowOverride | None:
    """解析 projects.window_override。空值 / 坏值一律当作「继承全局」。

    这里**故意不抛异常**：一列坏数据不应该让整个调度器停摆，降级成继承是安全的
    失败方向（窗口照旧生效，不会变成全天放开一直烧钱）。
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    mode = data.get("mode")
    if mode == "always":
        return WindowOverride("always")
    if mode == "custom":
        try:
            windows = parse_windows(data.get("windows"))
        except TimingError:
            return None
        return WindowOverride("custom", tuple(windows))
    return None


def is_open_for(now: datetime, global_windows: list[Window], override: str | None) -> bool:
    parsed = parse_override(override)
    if parsed is None:
        return is_open(now, global_windows)
    if parsed.mode == "always":
        return True
    return is_open(now, list(parsed.windows))


# ---------- 定时规则 ----------


@dataclass(frozen=True, slots=True)
class Rule:
    """三种规则：一次性 / 每天 / 每周几。

    `once` 的 `at` 是**用户时区的本地时刻**，不带偏移量 —— 存绝对值的话，
    改时区配置就等于把已排好的任务全挪了位置。
    """

    type: Literal["once", "daily", "weekly"]
    at: datetime | None = None
    clock: time | None = None
    days: tuple[int, ...] = ()

    def as_json(self) -> str:
        if self.type == "once":
            assert self.at is not None
            payload: dict[str, Any] = {"type": "once", "at": self.at.strftime(_ISO_MINUTE)}
        elif self.type == "daily":
            assert self.clock is not None
            payload = {"type": "daily", "time": self.clock.strftime("%H:%M")}
        else:
            assert self.clock is not None
            names = [name for name, idx in WEEKDAYS.items() if idx in self.days]
            payload = {"type": "weekly", "days": names, "time": self.clock.strftime("%H:%M")}
        return json.dumps(payload, ensure_ascii=False)

    def describe(self) -> str:
        if self.type == "once":
            assert self.at is not None
            return f"一次性 · {self.at.strftime('%Y-%m-%d %H:%M')}"
        if self.type == "daily":
            assert self.clock is not None
            return f"每天 {self.clock.strftime('%H:%M')}"
        assert self.clock is not None
        labels = "、".join(WEEKDAY_LABEL[d] for d in sorted(self.days))
        return f"{labels} {self.clock.strftime('%H:%M')}"


def parse_rule(raw: str | dict[str, Any]) -> Rule:
    """坏输入一律变成 `TimingError`。

    `json.JSONDecodeError` 虽然也是 `ValueError`，但它不是 `TimingError` ——
    调度器只接 `TimingError`，漏出去就会被当成「未知异常」反复重试同一条坏规则。
    """
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TimingError(f"规则不是合法的 JSON：{raw!r}") from exc
    else:
        data = raw
    if not isinstance(data, dict):
        raise TimingError("规则要是一个对象")
    kind = data.get("type")
    if kind == "once":
        at_raw = str(data.get("at") or "").strip()
        try:
            at = datetime.strptime(at_raw, _ISO_MINUTE)
        except ValueError as exc:
            raise TimingError(f"一次性任务的时间要写成 2026-09-23T09:00，收到的是 {at_raw!r}") from exc
        return Rule("once", at=at)
    if kind == "daily":
        return Rule("daily", clock=parse_time(data.get("time")))
    if kind == "weekly":
        days = data.get("days")
        if isinstance(days, str):
            days = [d.strip() for d in days.split(",") if d.strip()]
        if not days:
            raise TimingError("每周规则要指定星期几，比如 mon,wed")
        indices = []
        for day in days:
            key = str(day).strip().lower()[:3]
            if key not in WEEKDAYS:
                raise TimingError(f"认不出星期 {day!r}，可用 mon/tue/wed/thu/fri/sat/sun")
            indices.append(WEEKDAYS[key])
        return Rule("weekly", clock=parse_time(data.get("time")), days=tuple(sorted(set(indices))))
    raise TimingError(f"不支持的规则类型 {kind!r}，只有 once / daily / weekly")


def next_occurrence(rule: Rule, after: datetime, tz: ZoneInfo) -> datetime | None:
    """`after` 之后的下一次触发时刻。一次性任务过期后返回 None。

    **总是从 `after` 往后算，不做 `上次 + 间隔` 的追赶** —— 否则平台停机三天，
    开机时会一口气补跑三天的任务。用户要的是错过就跳过。
    """
    local = after.astimezone(tz)

    if rule.type == "once":
        assert rule.at is not None
        target = rule.at.replace(tzinfo=tz)
        return target if target > local else None

    assert rule.clock is not None
    if rule.type == "daily":
        candidate = datetime.combine(local.date(), rule.clock, tz)
        if candidate <= local:
            candidate += timedelta(days=1)
        return candidate

    # weekly：今天可能已经过点，那就往后找下一个匹配的星期，最多看 8 天
    for offset in range(8):
        day = local.date() + timedelta(days=offset)
        if day.weekday() not in rule.days:
            continue
        candidate = datetime.combine(day, rule.clock, tz)
        if candidate > local:
            return candidate
    return None


def first_occurrence(rule: Rule, now: datetime, tz: ZoneInfo) -> datetime | None:
    """新建定时任务时的首次触发时刻。

    和 `next_occurrence` 的区别只在一处：**一次性任务如果时间已经过去，
    明确报错而不是静默变成永不触发** —— 用户刚打完命令，值得立刻被告知。
    """
    local = now.astimezone(tz)
    if rule.type == "once":
        assert rule.at is not None
        target = rule.at.replace(tzinfo=tz)
        if target <= local:
            raise TimingError(
                f"这个时刻已经过去了：{rule.at.strftime('%Y-%m-%d %H:%M')}"
                f"（当前 {local.strftime('%Y-%m-%d %H:%M')}）"
            )
        return target
    # 周期任务「今天这个点已经过了」是正常的，顺延到下一次即可
    return next_occurrence(rule, now, tz)
