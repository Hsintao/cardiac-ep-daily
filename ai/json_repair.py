"""JSON 提取与修复：全部是纯函数，不碰网络、不碰磁盘。

为什么需要这一层：即使开了 DeepSeek 的 JSON 模式，实测仍会遇到
  - 响应前后带散文（"好的，以下是解读：{...}"）
  - 摘要里的 LaTeX 单反斜杠（``\\mathcal``）—— 这是**非法 JSON 转义**
  - 尾随逗号（``{"a": 1,}``）
  - 响应被 max_tokens 截断（花括号不闭合）

这些都不该让整篇论文失败，所以这里做「尽力恢复」：
能解析出 dict 就返回它，解析不出来就返回 ``{}``，**绝不抛异常**。
调用方（enhance.py）拿到 ``{}`` 后走重试/丢弃逻辑，
而不是写占位符文本。

设计上刻意与参考实现 daily-arXiv-ai-enhanced/ai/enhance.py 保持一致的三级
尝试顺序（原样 → 转义修复 → 转义修复 + 尾随逗号），因为那套顺序是在真实
数据上试出来的：先修转义再删逗号，否则 ``\\`` 与 ``,}`` 会互相干扰。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

# JSON 字符串里，反斜杠后面只允许跟这些字符。其余（如 LaTeX 的 \m \a \c）
# 都是非法转义，必须把那个反斜杠本身转义掉。
_VALID_JSON_ESCAPES = frozenset('"\\/bfnrtu')

# 模型常见的「用单引号当 JSON 引号」，以及裸键名 {tldr: "..."}
_SINGLE_QUOTE_KEY_RE = re.compile(r"(?<=[{,\s])'([^'\\\n]{1,40})'(\s*:)")
_SINGLE_QUOTE_VALUE_RE = re.compile(r":\s*'([^'\\\n]*)'(\s*[,}\]])")
_BARE_KEY_RE = re.compile(r"(?<=[{,\s])([A-Za-z_][A-Za-z0-9_]{0,40})(\s*:)")

# 中文全角标点常被模型混进 JSON 结构里
_FULLWIDTH_MAP = {
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
    "：": ":",
    "，": ",",
    "｛": "{",
    "｝": "}",
}

# 被截断的响应里，字符串没有闭合就结束了；补一个引号往往就能救回来
_TRAILING_PARTIAL_STRING_RE = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*$')


def extract_json_object_text(text: str) -> str:
    """返回最外层 ``{...}`` 片段，容忍其前后的散文。

    用花括号平衡扫描（并正确跳过字符串内的花括号）而不是
    ``text[text.find('{'):text.rfind('}')+1]``：后者在模型把嵌套对象
    或含 ``}`` 的字符串写进 value 时会被截断，或把后文散文里的 ``}`` 也算进来。

    响应被截断（花括号不闭合）时返回剩余部分，交给修复层尝试补齐，
    而不是直接放弃整条响应。
    """
    if not isinstance(text, str) or not text:
        return ""
    start = text.find("{")
    if start == -1:
        return ""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    # 不闭合：返回剩余内容，让 repair 有机会补括号
    return text[start:]


def repair_backslash_escapes(text: str) -> str:
    """只把**非法**的反斜杠转义加上转义，不动已经正确的转义。

    摘要里的 LaTeX 如 ``\\mathcal``、``\\alpha`` 用的是单个反斜杠，
    这在 JSON 里非法；但一刀切的 ``replace('\\\\', '\\\\\\\\')`` 会把
    本来正确的 ``\\\\``、``\\"``、``\\n`` 也弄坏 —— 这正是参考实现早期
    在真实摘要上恢复失败的原因。

    逐字符扫描：遇到 ``\\x`` 时，``x`` 是合法转义字符就原样保留
    （``\\uXXXX`` 还要连它的四位十六进制一起吃掉），否则把反斜杠自己转义。
    """
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else ""
    out: List[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        following = text[index + 1] if index + 1 < length else ""
        # \u 只有在后面确实跟着四位十六进制时才是合法转义；否则 ``\u``
        # 本身（如 LaTeX 的 \underline）就是非法的，必须转义掉反斜杠。
        if following == "u":
            digits = text[index + 2 : index + 6]
            if len(digits) == 4 and all(c in "0123456789abcdefABCDEF" for c in digits):
                out.append("\\u")
                out.append(digits)
                index += 6
                continue
        elif following in _VALID_JSON_ESCAPES:
            out.append(char)
            out.append(following)
            index += 2
            continue
        # 非法转义（含字符串末尾孤立的反斜杠）：把反斜杠本身转义掉
        out.append("\\\\")
        index += 1
    return "".join(out)


def repair_trailing_commas(text: str) -> str:
    """删掉对象/数组末尾多余的逗号（``{"a": 1,}`` → ``{"a": 1}``）。

    逐字符扫描、**跳过字符串内部**：正则版会把正文里的
    ``"1, }"`` 也当成尾随逗号删掉（真实摘要里确实会出现这种片段）。
    """
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else ""
    out: List[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == '"':
            # 整段字符串原样搬运（含转义），内部内容一概不动
            out.append(char)
            index += 1
            while index < length:
                current = text[index]
                out.append(current)
                index += 1
                if current == "\\" and index < length:
                    out.append(text[index])
                    index += 1
                    continue
                if current == '"':
                    break
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < length and text[lookahead] in " \t\r\n":
                lookahead += 1
            if lookahead < length and text[lookahead] in "}]":
                # 尾随逗号：连同其后的空白一起丢弃，保留收尾括号
                index = lookahead
                continue
        out.append(char)
        index += 1
    return "".join(out)


def repair_fullwidth_punctuation(text: str) -> str:
    """把混进 JSON 结构里的全角标点换成半角。

    只替换结构字符（引号/冒号/逗号/花括号），不碰中文正文里的句号顿号。
    """
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else ""
    for source, target in _FULLWIDTH_MAP.items():
        text = text.replace(source, target)
    return text


def repair_single_quotes(text: str) -> str:
    """把 ``{tldr: '...'}`` 这类单引号写法转成合法 JSON。

    只处理「键」和「紧跟冒号的值」，避免把中文正文里的英文撇号
    （``patient's``）也改掉。
    """
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else ""
    text = _SINGLE_QUOTE_KEY_RE.sub(r'"\1"\2', text)
    text = _SINGLE_QUOTE_VALUE_RE.sub(r': "\1"\2', text)
    return text


def repair_bare_keys(text: str) -> str:
    """给裸键名补上引号：``{tldr: "x"}`` → ``{"tldr": "x"}``。"""
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else ""
    return _BARE_KEY_RE.sub(r'"\1"\2', text)


def _close_truncated_json(text: str) -> str:
    """尽力闭合被截断的 JSON。

    先给未闭合的字符串补引号，再按扫描到的栈补上缺失的 ``}`` / ``]``，
    最后删掉可能因此产生的尾随逗号甚至孤立的 ``"key":`` 片段。
    """
    if not isinstance(text, str) or not text:
        return ""
    candidate = text.rstrip()
    if not candidate:
        return ""

    # 未闭合的字符串（奇数个未转义引号）→ 补一个引号
    if _has_open_string(candidate):
        candidate += '"'

    stack: List[str] = []
    in_string = False
    escaped = False
    for char in candidate:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]" and stack:
            stack.pop()

    # 截断点常见形态："result": "D", "conclusion":   ← 键写了但值没写
    # 必须在补括号**之前**处理：补完 `}` 之后尾部就变成 `:}`，
    # 正则再也匹配不到「以冒号结尾」，会产出一个非法的 `"key":}`。
    candidate = _repair_dangling_key(candidate)

    closers = "".join("}" if opener == "{" else "]" for opener in reversed(stack))
    return candidate + closers


# 截断后残留的「只有键名、没有值」尾部，例如：
#   ..., "conclusion":      ← 末尾停在冒号上
#   ..., "conclusion": }    ← 已经补过括号，变成 `:}` 或 `:,}` 或 `:""`
_DANGLING_KEY_TAIL_RE = re.compile(
    r',?\s*"[A-Za-z_][A-Za-z0-9_]*"\s*:\s*[}\],\s]*$'
)


def _repair_dangling_key(candidate: str) -> str:
    """修掉截断点残留在尾部的「有键无值」片段。

    为什么单独写一个函数而不是一条正则：这段修复要在**补括号前后**都能用。
    补括号前尾部是 `"conclusion":`，补括号后同一处会变成 `"conclusion":}`
    甚至 `"conclusion":""`。与其写三条互相干扰的正则，不如在补括号前先把
    残留键删掉，然后再统一补值/补括号，逻辑只有一个出口。
    """
    if not candidate:
        return candidate
    # 只在字符串外部匹配：先把尾部已有内容切成「结构尾部」，正文不动
    stripped = candidate.rstrip()
    if stripped.endswith(":"):
        # 最简单也最常见：以冒号结尾 → 删掉整个残留键，稍后统一补 ""
        match = _DANGLING_KEY_TAIL_RE.search(stripped)
        if match:
            stripped = stripped[: match.start()].rstrip().rstrip(",")
        if re.search(r":\s*$", stripped):
            stripped += '""'
        return stripped
    return candidate


def _has_open_string(text: str) -> bool:
    """判断文本是否停在一个未闭合的字符串里。"""
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
    return in_string


def _parse_candidates(candidate: str) -> List[str]:
    """按「修复强度递增」的顺序生成候选文本。

    顺序即参考实现验证过的顺序：先只修转义，再修逗号，再上更强的手段，
    每一步都可能解析成功，越往后代价越高（也可能引入新问题），所以
    前面的能成功就不往后走。
    """
    candidates: List[str] = []
    seen = set()

    def add(text: str) -> None:
        if text and text not in seen:
            seen.add(text)
            candidates.append(text)

    add(candidate)
    escaped = repair_backslash_escapes(candidate)
    add(escaped)
    escaped_no_comma = repair_trailing_commas(escaped)
    add(escaped_no_comma)

    fullwidth = repair_fullwidth_punctuation(candidate)
    add(fullwidth)
    fullwidth_escaped = repair_trailing_commas(repair_backslash_escapes(fullwidth))
    add(fullwidth_escaped)

    quotes = repair_single_quotes(fullwidth_escaped)
    add(quotes)
    add(repair_trailing_commas(repair_bare_keys(quotes)))

    # 最后才尝试「截断修复」：它会主动补字符，风险最高。
    for base in (escaped_no_comma, fullwidth_escaped, quotes):
        closed = repair_trailing_commas(_close_truncated_json(base))
        add(closed)
        add(repair_trailing_commas(repair_backslash_escapes(closed)))
    return candidates


def parse_json_object(text: Any) -> Optional[Dict[str, Any]]:
    """从模型响应里解析出一个 dict；失败返回 ``None``（不抛异常）。

    这是本模块的主入口。返回 ``None`` 与返回 ``{}`` 语义不同：
      - ``None``  无法解析出任何对象 → 调用方应重试
      - ``{}``    解析成功但对象是空的 → 同样视为无效内容
    调用方统一用 ``is_meaningful()`` 判定，这里只负责「能不能解析」。
    """
    if isinstance(text, dict):
        return text
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped:
        return None  # 文档明确警告：JSON 模式可能返回空 content

    # 1) 整体就是 JSON（最常见，也最便宜）
    for candidate in _parse_candidates(stripped):
        parsed = _try_load(candidate)
        if parsed is not None:
            return parsed

    # 2) 带散文：先抠出最外层对象，再重复同一套修复
    extracted = extract_json_object_text(stripped)
    if extracted and extracted != stripped:
        for candidate in _parse_candidates(extracted):
            parsed = _try_load(candidate)
            if parsed is not None:
                return parsed
    return None


def _try_load(text: str) -> Optional[Dict[str, Any]]:
    """``json.loads`` 的安全包装：只接受 dict，永不抛异常。"""
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def looks_like_empty_content(text: Any) -> bool:
    """True 表示响应没有可用内容（空串 / 纯空白 / 解析不出对象）。

    单独抽出来是因为「空 content」是 DeepSeek JSON 模式文档明确警告的
    已知行为，调用方需要把它与「解析失败」区分开来打日志和决定重试。
    """
    if text is None:
        return True
    if isinstance(text, str) and not text.strip():
        return True
    if isinstance(text, dict) and not text:
        return True
    return False


__all__ = [
    "extract_json_object_text",
    "looks_like_empty_content",
    "parse_json_object",
    "repair_backslash_escapes",
    "repair_bare_keys",
    "repair_fullwidth_punctuation",
    "repair_single_quotes",
    "repair_trailing_commas",
]
