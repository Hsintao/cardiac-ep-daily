"""ai/json_repair.py 的纯函数测试。

全部离线：不打网络、不打真实 API、不 mock SDK。
这些用例覆盖的都是**真实模型响应里出现过**的坏格式
（带散文、LaTeX 反斜杠、尾随逗号、被 max_tokens 截断）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import json_repair  # noqa: E402


# --------------------------------------------------------------------------
# 花括号平衡扫描
# --------------------------------------------------------------------------


def test_extract_plain_object():
    assert json_repair.extract_json_object_text('{"a": 1}') == '{"a": 1}'


def test_extract_tolerates_prose_around_json():
    text = '好的，以下是解读：\n{"tldr": "房颤消融"}\n以上。'
    assert json_repair.extract_json_object_text(text) == '{"tldr": "房颤消融"}'


def test_extract_skips_braces_inside_strings():
    text = '{"note": "使用 {公式} 处理", "x": 1}'
    assert json_repair.extract_json_object_text(text) == text


def test_extract_handles_nested_objects():
    text = '前言 {"a": {"b": {"c": 1}}} 结语'
    assert json_repair.extract_json_object_text(text) == '{"a": {"b": {"c": 1}}}'


def test_extract_handles_escaped_quote_inside_string():
    text = r'{"q": "他说 \"你好\"", "n": 1}'
    assert json_repair.extract_json_object_text(text) == text


def test_extract_returns_remainder_when_unbalanced():
    """截断响应：返回剩余内容，而不是空串，让修复层还有机会。"""
    text = '{"tldr": "被截断'
    assert json_repair.extract_json_object_text(text) == text


def test_extract_empty_and_garbage():
    assert json_repair.extract_json_object_text("") == ""
    assert json_repair.extract_json_object_text("没有对象") == ""
    assert json_repair.extract_json_object_text(None) == ""


# --------------------------------------------------------------------------
# 反斜杠转义修复
# --------------------------------------------------------------------------


def test_latex_backslash_is_repaired():
    """LaTeX 的 \\mathcal 是非法 JSON 转义 —— 这是最常见的失败原因。"""
    broken = r'{"method": "\mathcal{R} 分析"}'
    fixed = json_repair.repair_backslash_escapes(broken)
    assert json.loads(fixed)["method"] == r"\mathcal{R} 分析"


def test_valid_escapes_are_preserved():
    """已经正确的转义不能被二次转义（参考实现早期就栽在这里）。"""
    valid = r'{"a": "换行\n引号\"反斜杠\\制表\t"}'
    repaired = json_repair.repair_backslash_escapes(valid)
    assert repaired == valid
    assert json.loads(repaired)["a"] == '换行\n引号"反斜杠\\制表\t'


def test_unicode_escape_digits_are_consumed():
    text = r'{"a": "\u4e2d\u6587"}'
    assert json_repair.repair_backslash_escapes(text) == text
    assert json.loads(json_repair.repair_backslash_escapes(text))["a"] == "中文"


def test_invalid_unicode_escape_is_neutralised():
    """\\u 后面不是四位十六进制时，和普通非法转义一样处理。"""
    text = r'{"a": "\uzz"}'
    repaired = json_repair.repair_backslash_escapes(text)
    assert json.loads(repaired)["a"] == r"\uzz"


def test_trailing_backslash_is_neutralised():
    repaired = json_repair.repair_backslash_escapes('{"a": "x\\')
    assert repaired.endswith("\\\\")


def test_mixed_valid_and_invalid_escapes():
    broken = r'{"a": "第一行\n\mathcal{X} 与 \alpha"}'
    fixed = json_repair.repair_backslash_escapes(broken)
    parsed = json.loads(fixed)
    assert parsed["a"] == "第一行\n" + r"\mathcal{X} 与 \alpha"


# --------------------------------------------------------------------------
# 尾随逗号
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "broken,expected",
    [
        ('{"a": 1,}', '{"a": 1}'),
        ('{"a": 1 , }', '{"a": 1 }'),
        ('[1, 2,]', '[1, 2]'),
        ('{"a": {"b": 1,},}', '{"a": {"b": 1}}'),
    ],
)
def test_repair_trailing_commas(broken, expected):
    assert json_repair.repair_trailing_commas(broken) == expected


def test_trailing_comma_inside_string_untouched():
    text = '{"a": "1, }"}'
    assert json_repair.repair_trailing_commas(text) == text


# --------------------------------------------------------------------------
# 全角标点 / 单引号 / 裸键
# --------------------------------------------------------------------------


def test_fullwidth_structural_punctuation():
    broken = "｛“a”： 1， “b”： 2｝"
    assert json.loads(json_repair.repair_fullwidth_punctuation(broken)) == {"a": 1, "b": 2}


def test_single_quoted_object():
    broken = "{'tldr': '房颤消融优于药物', 'method': 'RCT'}"
    parsed = json_repair.parse_json_object(broken)
    assert parsed == {"tldr": "房颤消融优于药物", "method": "RCT"}


def test_bare_keys():
    broken = '{tldr: "房颤消融", method: "RCT"}'
    parsed = json_repair.parse_json_object(broken)
    assert parsed == {"tldr": "房颤消融", "method": "RCT"}


def test_apostrophe_in_value_is_not_mangled():
    """值里出现英文撇号时不能把正文改坏。"""
    text = '{"conclusion": "patient\'s outcome improved"}'
    assert json_repair.parse_json_object(text) == {
        "conclusion": "patient's outcome improved"
    }


# --------------------------------------------------------------------------
# 主入口 parse_json_object
# --------------------------------------------------------------------------


def test_parse_clean_json():
    text = json.dumps({"tldr": "x", "motivation": "y"}, ensure_ascii=False)
    assert json_repair.parse_json_object(text) == {"tldr": "x", "motivation": "y"}


def test_parse_with_prose_prefix_and_code_fence():
    text = '```json\n{"tldr": "房颤", "method": "RCT"}\n```\n希望有帮助！'
    assert json_repair.parse_json_object(text) == {"tldr": "房颤", "method": "RCT"}


def test_parse_realistic_latex_abstract_response():
    text = (
        "以下是解读：\n"
        r'{"tldr": "PVI 优于药物", "motivation": "既往证据不足", '
        r'"method": "纳入 120 例，\mathcal{AF} 患者", '
        r'"result": "HR 0.62 (95%CI 0.45-0.85), P=0.003", '
        r'"conclusion": "支持早期消融"}'
    )
    parsed = json_repair.parse_json_object(text)
    assert parsed is not None
    assert parsed["tldr"] == "PVI 优于药物"
    assert r"\mathcal{AF}" in parsed["method"]
    assert "0.003" in parsed["result"]


def test_parse_trailing_comma_response():
    text = '{"tldr": "A", "method": "B",}'
    assert json_repair.parse_json_object(text) == {"tldr": "A", "method": "B"}


def test_parse_truncated_response_recovers_available_fields():
    """被 max_tokens 截断：能救回的字段要救回，不能整条丢掉。"""
    text = '{"tldr": "房颤消融", "motivation": "背景", "method": "RCT 纳入 120 例'
    parsed = json_repair.parse_json_object(text)
    assert parsed is not None
    assert parsed["tldr"] == "房颤消融"
    assert parsed["method"].startswith("RCT 纳入 120 例")


def test_parse_truncated_with_dangling_key():
    """截断点停在键名上（"conclusion": ）也要能补出合法 JSON。"""
    text = '{"tldr": "A", "motivation": "B", "method": "C", "result": "D", "conclusion":'
    parsed = json_repair.parse_json_object(text)
    assert parsed is not None
    assert parsed["result"] == "D"


def test_parse_empty_returns_none():
    """空 content 是文档明确的已知行为，必须返回 None（触发重试）。"""
    for empty in ("", "   ", "\n", None, 12345):
        assert json_repair.parse_json_object(empty) is None


def test_parse_non_object_json_returns_none():
    assert json_repair.parse_json_object("[1, 2, 3]") is None
    assert json_repair.parse_json_object("真的没有 json") is None
    assert json_repair.parse_json_object('"just a string"') is None


def test_parse_never_raises_on_hostile_input():
    """纯函数的契约：任何输入都不抛异常。"""
    hostile = [
        "{" * 500,
        "}" * 500,
        '{"a": ' + '"' * 99,
        '{"a": "\\',
        "\x00\x01{}",
        '{"a": 1}{"b": 2}',
        "{{{{",
        '{"a": "' + "x" * 10000,
    ]
    for text in hostile:
        result = json_repair.parse_json_object(text)
        assert result is None or isinstance(result, dict)


def test_parse_first_object_wins_when_two_present():
    parsed = json_repair.parse_json_object('{"a": 1} 然后是 {"b": 2}')
    assert parsed == {"a": 1}


def test_parse_dict_passthrough():
    data = {"tldr": "x"}
    assert json_repair.parse_json_object(data) is data


# --------------------------------------------------------------------------
# looks_like_empty_content
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["", "   ", "\n\t", None, {}])
def test_looks_like_empty_content_true(value):
    assert json_repair.looks_like_empty_content(value) is True


@pytest.mark.parametrize("value", ['{"a": 1}', "x", {"a": 1}, 0])
def test_looks_like_empty_content_false(value):
    assert json_repair.looks_like_empty_content(value) is False


def test_repair_functions_are_pure_and_tolerant_of_non_str():
    assert json_repair.repair_backslash_escapes(None) == ""
    assert json_repair.repair_trailing_commas(None) == ""
    assert json_repair.extract_json_object_text(None) == ""
