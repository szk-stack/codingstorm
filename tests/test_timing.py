"""执行窗口与定时规则的时间计算。

这里全是纯函数，所以能穷举边界 —— 窗口判断的坑（跨天、闭开区间、差一天）
在集成测试里很难重现，在这儿是几行断言。
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from codingstorm.timing import (
    TimingError,
    first_occurrence,
    is_open,
    is_open_for,
    iso_utc,
    load_zone,
    next_close,
    next_occurrence,
    next_open,
    parse_override,
    parse_rule,
    parse_utc,
    parse_windows,
)

TZ = ZoneInfo("Asia/Shanghai")


def at(hour: int, minute: int = 0, day: int = 22) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=TZ)


# ---------- 窗口 ----------


def test_没有窗口配置就是完全不限制():
    assert is_open(at(3), []) is True
    assert is_open(at(15), []) is True


def test_简单窗口的闭开区间():
    w = parse_windows([["00:30", "08:30"]])
    assert is_open(at(0, 29), w) is False
    assert is_open(at(0, 30), w) is True  # 起点算在内
    assert is_open(at(8, 29), w) is True
    assert is_open(at(8, 30), w) is False  # 终点不算 —— 否则两段相邻的窗口会重叠一秒


def test_跨天窗口():
    w = parse_windows([["22:00", "06:00"]])
    assert is_open(at(23), w) is True
    assert is_open(at(2), w) is True
    assert is_open(at(6), w) is False
    assert is_open(at(12), w) is False
    assert is_open(at(21, 59), w) is False


def test_多段窗口取并集():
    w = parse_windows([["00:30", "08:30"], ["13:00", "14:00"]])
    assert is_open(at(2), w) is True
    assert is_open(at(13, 30), w) is True
    assert is_open(at(10), w) is False
    assert is_open(at(14), w) is False


def test_下次开启时刻():
    w = parse_windows([["00:30", "08:30"]])
    assert next_open(at(9), w) == at(0, 30, day=23)  # 今天已过，顺延到明天
    assert next_open(at(0), w) == at(0, 30, day=22)
    assert next_open(at(2), w) is None  # 已经开着


def test_关闭时刻要分清跨天窗口的哪一段():
    w = parse_windows([["22:00", "06:00"]])
    # 23 点处在 [22:00, 24:00) 那一段 → 明天 6 点关
    assert next_close(at(23), w) == at(6, day=23)
    # 凌晨 2 点处在 [00:00, 06:00) 那一段 → 今天 6 点关
    assert next_close(at(2), w) == at(6, day=22)
    assert next_close(at(12), w) is None  # 没开


def test_多段窗口的关闭时刻取最近的():
    w = parse_windows([["00:30", "08:30"], ["22:00", "06:00"]])
    # 凌晨 2 点两段都开着，先关的是 06:00
    assert next_close(at(2), w) == at(6, day=22)


def test_坏窗口配置要报错而不是静默忽略():
    assert parse_windows(None) == []
    assert parse_windows([]) == []
    with pytest.raises(TimingError):
        parse_windows([["00:30"]])
    with pytest.raises(TimingError):
        parse_windows([["8点半", "09:00"]])
    with pytest.raises(TimingError):
        parse_windows([["09:00", "09:00"]])  # 起止相同 = 永不开启，多半是写错了
    with pytest.raises(TimingError):
        parse_windows("00:30-08:30")


# ---------- 项目级覆盖 ----------


def test_项目覆盖_always_不受窗口限制():
    w = parse_windows([["00:30", "08:30"]])
    assert is_open_for(at(10), w, '{"mode":"always"}') is True
    assert is_open_for(at(10), w, None) is False  # 没覆盖就继承全局


def test_项目覆盖_custom_用自己的时段():
    w = parse_windows([["00:30", "08:30"]])
    override = '{"mode":"custom","windows":[["09:00","18:00"]]}'
    assert is_open_for(at(10), w, override) is True
    assert is_open_for(at(2), w, override) is False  # 全局开着，但项目自己的时段没开


def test_坏的项目覆盖降级成继承全局():
    """一列坏数据不该让调度器停摆，也不该变成全天放开一直烧钱。"""

    w = parse_windows([["00:30", "08:30"]])
    for bad in ("{oops", '{"mode":"nope"}', '{"mode":"custom","windows":"中午"}', "[]", "null"):
        assert parse_override(bad) is None
        assert is_open_for(at(10), w, bad) is False


def test_窗口覆盖的序列化能往返():
    override = parse_override('{"mode":"custom","windows":[["09:00","18:00"]]}')
    assert override is not None
    assert parse_override(override.as_json()) == override


# ---------- 规则 ----------


def test_每天规则顺延到明天():
    rule = parse_rule({"type": "daily", "time": "09:00"})
    assert next_occurrence(rule, at(8), TZ) == at(9, day=22)
    assert next_occurrence(rule, at(9), TZ) == at(9, day=23)  # 正好到点算「已过」
    assert next_occurrence(rule, at(23), TZ) == at(9, day=23)


def test_每周规则只落在指定的星期():
    # 2026-09-22 是周二，23 是周三，28 是下周一
    rule = parse_rule({"type": "weekly", "days": ["mon", "wed"], "time": "09:00"})
    assert next_occurrence(rule, at(12, day=22), TZ) == at(9, day=23)  # 周三
    assert next_occurrence(rule, at(10, day=23), TZ) == at(9, day=28)  # 周三已过 → 下周一


def test_每周规则支持大小写和逗号串():
    a = parse_rule({"type": "weekly", "days": ["MON", "wed"], "time": "09:00"})
    b = parse_rule({"type": "weekly", "days": "mon,wed", "time": "09:00"})
    assert a.days == b.days == (0, 2)
    assert "周一" in a.describe() and "周三" in a.describe()


def test_一次性规则过期返回_None():
    rule = parse_rule({"type": "once", "at": "2026-09-01T09:00"})
    assert next_occurrence(rule, at(8), TZ) is None


def test_一次性规则没到点就正常返回():
    rule = parse_rule({"type": "once", "at": "2026-09-23T09:00"})
    assert next_occurrence(rule, at(8), TZ) == at(9, day=23)


def test_新建时一次性规则过期要当场报错():
    """刚打完命令，值得立刻被告知时间写错了，而不是静默变成永不触发。"""

    rule = parse_rule({"type": "once", "at": "2026-09-01T09:00"})
    with pytest.raises(TimingError, match="已经过去"):
        first_occurrence(rule, at(8), TZ)


def test_新建时周期规则的点已过是正常的():
    day = parse_rule({"type": "daily", "time": "09:00"})
    assert first_occurrence(day, at(12), TZ) == at(9, day=23)


def test_不补跑_停机三天后只算下一次():
    """从当前时刻往后算，不做「上次 + 间隔」的追赶。"""

    rule = parse_rule({"type": "daily", "time": "09:00"})
    three_days_later = at(12, day=25)
    got = next_occurrence(rule, three_days_later, TZ)
    assert got == at(9, day=26)  # 不是 23 号，也不是连着补三条


def test_坏规则要报错():
    with pytest.raises(TimingError):
        parse_rule({"type": "hourly"})
    with pytest.raises(TimingError):
        parse_rule({"type": "once", "at": "明天早上"})
    with pytest.raises(TimingError):
        parse_rule({"type": "weekly", "time": "09:00"})  # 没写星期几
    with pytest.raises(TimingError):
        parse_rule({"type": "weekly", "days": ["星期八"], "time": "09:00"})


def test_规则的描述和序列化能往返():
    for spec in (
        {"type": "once", "at": "2026-09-23T09:00"},
        {"type": "daily", "time": "00:30"},
        {"type": "weekly", "days": ["mon", "fri"], "time": "22:00"},
    ):
        rule = parse_rule(spec)
        assert parse_rule(rule.as_json()).as_json() == rule.as_json()
        assert rule.describe()


# ---------- 时区与时间串 ----------


def test_utc_时间串的格式和_store_完全一致():
    """到期判断是拿字符串比大小，格式一变比较结果就是错的。"""

    from codingstorm.store import utcnow

    assert iso_utc(at(9)) == "2026-09-22T01:00:00.000Z"
    assert len(iso_utc(at(9))) == len(utcnow())
    assert iso_utc(at(9))[10] == "T" and iso_utc(at(9)).endswith("Z")


def test_时间串能往返():
    moment = at(9, 30)
    assert parse_utc(iso_utc(moment)) == moment


def test_时区必须显式给():
    with pytest.raises(TimingError):
        load_zone("Mars/Olympus")


def test_钟点按给进来的时区算():
    """同一个 UTC 瞬间，北京是 08:00、东京是 09:00 —— 这就是为什么时区必须显式配。"""

    instant = datetime(2026, 9, 22, 0, 0, tzinfo=ZoneInfo("UTC"))
    w = parse_windows([["00:30", "08:30"]])
    assert is_open(instant.astimezone(TZ), w) is True  # 北京 08:00 → 还开着
    assert is_open(instant.astimezone(ZoneInfo("Asia/Tokyo")), w) is False  # 东京 09:00 → 关了


def test_窗口判断不受秒的影响():
    w = parse_windows([["00:30", "08:30"]])
    assert is_open(at(0, 30) + timedelta(seconds=59), w) is True
