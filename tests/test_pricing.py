"""成本计算测试。

核心断言：**价目表里没有的模型，成本必须是 None，不能瞎猜** ——
第三方中转下 `total_cost_usd` 跟实际付费对不上，宁可留空。
"""

from pathlib import Path

from codingstorm.pricing import MILLION, PriceTable, Usage


def test_empty_table_has_no_cost():
    table = PriceTable.empty()
    assert not table.configured
    assert table.cost("anything", Usage(input_tokens=1000)) is None


def test_load_from_toml(tmp_path: Path):
    p = tmp_path / "prices.toml"
    p.write_text(
        'version = "2026-09-18"\n'
        '[models."deepseek-v4-flash"]\n'
        "input = 0.14\n"
        "output = 0.28\n"
        "cache_read = 0.014\n",
        encoding="utf-8",
    )
    table = PriceTable.load(p)
    assert table.configured
    assert table.version == "2026-09-18"


def test_cost_computation(tmp_path: Path):
    p = tmp_path / "prices.toml"
    p.write_text(
        'version = "v1"\n'
        '[models."m"]\ninput = 1.0\noutput = 2.0\ncache_read = 0.1\ncache_creation = 1.0\n',
        encoding="utf-8",
    )
    table = PriceTable.load(p)

    cost = table.cost("m", Usage(
        input_tokens=MILLION, output_tokens=MILLION,
        cache_read_tokens=MILLION, cache_creation_tokens=MILLION,
    ))
    assert cost == 1.0 + 2.0 + 0.1 + 1.0


def test_unknown_model_returns_none(tmp_path: Path):
    p = tmp_path / "prices.toml"
    p.write_text('version = "v1"\n[models."known"]\ninput = 1\noutput = 1\n', encoding="utf-8")
    table = PriceTable.load(p)
    assert table.cost("unknown", Usage(input_tokens=1000)) is None
    assert table.cost(None, Usage(input_tokens=1000)) is None


def test_cache_defaults_to_input_price(tmp_path: Path):
    """没写 cache 单价时退化成 input 单价，而不是 0 —— 0 会低报成本。"""
    p = tmp_path / "prices.toml"
    p.write_text('version = "v1"\n[models."m"]\ninput = 2.0\noutput = 1.0\n', encoding="utf-8")
    table = PriceTable.load(p)
    assert table.cost("m", Usage(cache_read_tokens=MILLION)) == 2.0


def test_missing_file_is_not_an_error(tmp_path: Path):
    table = PriceTable.load(tmp_path / "nope.toml")
    assert not table.configured
    assert PriceTable.load(None).configured is False


def test_malformed_file_degrades_gracefully(tmp_path: Path):
    p = tmp_path / "prices.toml"
    p.write_text("这不是合法 TOML {{{", encoding="utf-8")
    table = PriceTable.load(p)
    assert not table.configured  # 不抛异常，只是没有成本


def test_bad_model_entry_is_skipped(tmp_path: Path):
    p = tmp_path / "prices.toml"
    p.write_text(
        'version = "v1"\n'
        '[models."good"]\ninput = 1\noutput = 1\n'
        '[models."bad"]\ninput = "不是数字"\n',
        encoding="utf-8",
    )
    table = PriceTable.load(p)
    assert table.cost("good", Usage(input_tokens=MILLION)) == 1.0
    assert table.cost("bad", Usage(input_tokens=MILLION)) is None
