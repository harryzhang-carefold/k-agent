"""TASK-041 / BUG-008 回归单测：`hermes profile list` 表格解析（长名不再静默丢弃）。

BUG-008：原 `list_profiles()` 用 `^\\s{2,}(\\S+)\\s{2,}` 匹配数据行，依赖
"name 之后至少 2 个分隔空格"。`profile list` 是固定/自适应列宽表格，name ≥15
字符时 name 与 model 之间只剩 1 个分隔空格 → 匹配失败 → 该 profile 被静默
丢弃（API 不可见但数据真实落地）。另 active 行带 `◆` 前缀使行前导只剩 1
空格，原正则同样丢它。

本文件单测**纯函数** `core.hermes_cli._parse_profile_list`（不依赖 hermes CLI、
不起子进程、不连服务）：
  - 回归护栏：构造 len=14/15/16/20 的假表格行，断言均被正确解析（BUG-008 阈值
    正好跨 14→15）；
  - 单空格分隔（len≥15 的真实形态）也能解析；
  - 固定宽 2 空格分隔（len≤14 的既有形态）不回归；
  - active（`◆`）行能解析（原正则的另一处静默丢弃）；
  - em-dash（—）alias/distribution → None；
  - 表头/分隔线/空行被跳过；
  - 疑似数据行缺列 → logging.warning（杜绝再次静默丢弃）。

与既有 pytest 一致：src 加入 sys.path，纯单元测试，不依赖 8099 服务。
"""
import logging
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from core import hermes_cli  # noqa: E402

E = "\u2014"  # em-dash "—"（profile list 的空占位符，非 "-"）
MODEL = "vllm-qwen3.8-27b"


def _row(name: str, model: str = MODEL, gateway: str = "stopped",
         alias: str = E, dist: str = E) -> str:
    """构造一条与真实 `hermes profile list` 布局一致的数据行。

    真实 CLI 的 name 列：前导 2 空格 + name 左对齐 16 列宽（止于第 18 列），
    长 name（>16）时列宽自适应撑开、name 与 model 间至少 1 个分隔空格。
    故 name→model 分隔 = max(1, 16 - len(name))：
      - len ≤14 → ≥2 空格（旧正则安全区）
      - len ≥15 → 恰好 1 空格（BUG-008 触发形态）
    其余列间距固定 2 空格（解析按空白切词，精确列宽不影响解析正确性）。
    """
    sep = max(1, 16 - len(name))
    rest = f"{model}  {gateway}  {alias}  {dist}"
    return "  " + name + " " * sep + rest


@pytest.mark.parametrize("L", [14, 15, 16, 20])
def test_longname_rows_parsed(L, caplog):
    """回归护栏：len=14/15/16/20 的行都被解析成 profile 记录（BUG-008 阈值跨 14→15）。"""
    name = "t041" + "x" * (L - 4)
    assert len(name) == L
    out = _row(name)
    with caplog.at_level(logging.DEBUG):
        rows = hermes_cli._parse_profile_list(out)
    assert len(rows) == 1, f"len={L} 行未被解析: {out!r}"
    assert rows[0]["name"] == name
    assert rows[0]["model"] == MODEL
    assert rows[0]["gateway"] == "stopped"
    assert rows[0]["alias"] is None  # em-dash → None
    assert rows[0]["distribution"] is None
    # 解析成功不应产生告警
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_single_space_separator_len15():
    """len=15 真实形态：name 与 model 之间只有 1 个分隔空格，必须解析。"""
    out = "  " + "t041xxxxxxxxxxx" + " " + MODEL + "  stopped  " + E + "  " + E
    rows = hermes_cli._parse_profile_list(out)
    assert [r["name"] for r in rows] == ["t041xxxxxxxxxxx"]


def test_fixed_width_two_spaces_len14_no_regression():
    """len=14（2 空格分隔，既有正常形态）不回归。"""
    out = _row("t041xxxxxxxxxx")  # len=14
    rows = hermes_cli._parse_profile_list(out)
    assert [r["name"] for r in rows] == ["t041xxxxxxxxxx"]


def test_active_profile_marker():
    """active（运行中）profile 行带 `◆` 前缀、行前导仅 1 空格，必须解析（原正则丢它）。"""
    out = " \u25c6zhangbeihai" + " " * 5 + MODEL + "  running" + "  " + E + "  " + E
    rows = hermes_cli._parse_profile_list(out)
    assert len(rows) == 1
    assert rows[0]["name"] == "zhangbeihai"  # `◆` 已被剥离
    assert rows[0]["gateway"] == "running"


def test_header_separator_blank_skipped():
    """表头 / ─ 分隔线 / 空行 被跳过，不误报为数据行。"""
    out = "\n".join([
        "",
        " Profile          Model                        Gateway      Alias        Distribution",
        " " + "─" * 15 + "    " + "─" * 23 + "    " + "─" * 11 + "    " + "─" * 11 + "    " + "─" * 18,
        _row("realprofile"),
        "",
    ])
    rows = hermes_cli._parse_profile_list(out)
    assert [r["name"] for r in rows] == ["realprofile"]


def test_em_dash_and_alias_values():
    """em-dash → None；有值的 alias 原样保留。"""
    r1 = hermes_cli._parse_profile_list(_row("p1", alias=E))[0]
    assert r1["alias"] is None and r1["distribution"] is None
    r2 = hermes_cli._parse_profile_list(_row("p1", alias="p1"))[0]
    assert r2["alias"] == "p1"


def test_missing_column_warns_not_silent(caplog):
    """首 token 是合法 profile 名却缺 model 列 → 必须 logging.warning（杜绝静默丢弃）。"""
    out = "  someprofile"  # 只有 name，没有 model
    with caplog.at_level(logging.WARNING):
        rows = hermes_cli._parse_profile_list(out)
    assert rows == []  # 缺列 → 不产出记录
    warns = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warns, "缺列数据行必须产生 warning 日志，不能静默丢弃"
    assert "someprofile" in warns[0].message


def test_old_regex_would_drop_len15_regression_evidence():
    """证明本缺陷真实：旧正则 `^\\s{2,}(\\S+)\\s{2,}` 对 len=15 行不匹配（被丢弃）。

    仅作回归证据——锁定旧实现行为，防止未来把宽松匹配又改回 ≥2 空格。
    """
    import re
    out = _row("t041xxxxxxxxxxx")  # len=15
    assert re.match(r"^\s{2,}(\S+)\s{2,}", out) is None  # 旧实现：匹配失败→丢弃
    # 而修复后的解析器能解析
    assert [r["name"] for r in hermes_cli._parse_profile_list(out)] == ["t041xxxxxxxxxxx"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
