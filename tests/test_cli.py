"""命令行解析。

存在的理由：`build_parser` 里每一段都复用短变量名，一不小心就把子解析器当成
顶层返回了 —— 那会让所有命令都变成「window 的参数错误」。这种错只有跑起来才看得见，
所以在这里钉死。
"""

from __future__ import annotations

import pytest

from codingstorm.cli import build_parser


def parse(argv: list[str]):
    return build_parser().parse_args(argv)


@pytest.mark.parametrize(
    "argv,command",
    [
        (["projects"], "projects"),
        (["submit", "demo", "标题"], "submit"),
        (["ls"], "ls"),
        (["show", "abc"], "show"),
        (["usage"], "usage"),
        (["schedule", "ls"], "schedule"),
        (["schedule", "add", "demo", "标题", "--daily", "09:00"], "schedule"),
        (["window"], "window"),
        (["window", "off"], "window"),
    ],
)
def test_顶层命令都认得出来(argv, command):
    assert parse(argv).command == command


def test_顶层解析器带_base_选项():
    """子解析器没有 --base —— 认出它就说明返回的确实是顶层。"""

    assert parse(["--base", "http://x", "ls"]).base == "http://x"


def test_没有子命令要报错():
    with pytest.raises(SystemExit):
        parse([])


# ---------- 定时规则 ----------


def _rule(argv: list[str]) -> dict:
    from codingstorm.cli import _rule_from_args

    return _rule_from_args(parse(["schedule", "add", "demo", "标题", *argv]))


def test_每日规则():
    assert _rule(["--daily", "09:00"]) == {"type": "daily", "time": "09:00"}


def test_每周规则():
    assert _rule(["--weekly", "03:00", "--days", "mon,wed"]) == {
        "type": "weekly",
        "time": "03:00",
        "days": ["mon", "wed"],
    }


def test_一次性规则接受空格分隔的写法():
    """命令行里打引号的「2026-09-23 09:00」要变成 ISO 的 T 分隔。"""

    assert _rule(["--at", "2026-09-23 09:00"]) == {"type": "once", "at": "2026-09-23T09:00"}


def test_三种规则互斥():
    with pytest.raises(SystemExit):
        parse(["schedule", "add", "demo", "标题", "--daily", "09:00", "--weekly", "10:00"])


def test_必须给一种规则():
    with pytest.raises(SystemExit):
        parse(["schedule", "add", "demo", "标题"])


def test_每周缺_days_要报错():
    with pytest.raises(SystemExit):
        _rule(["--weekly", "03:00"])


def test_一次性时间写错要报错():
    with pytest.raises(SystemExit):
        _rule(["--at", "明天早上"])
