"""AI 输出模型：五段式中文解读。

字段名与 daily-arXiv-ai-enhanced 的 ``Structure`` 完全一致
（tldr / motivation / method / result / conclusion），这样前端 js/app.js
的 ``item.AI.tldr`` 等取法可以原样复用，不需要改渲染层。

与参考项目不同的一点：这里**没有默认占位文本**。
参考实现把 "Summary generation failed" 之类当作兜底值写进 JSONL，
但渲染层只检查「键是否存在」，占位符会被当成真的摘要显示在页面上。
所以本任务的契约是：字段要么是模型给出的真实内容，要么整个 AI 字段不写。
"""

from __future__ import annotations

from typing import Dict

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 五段式字段的顺序即契约顺序，渲染层与测试都依赖它。
AI_FIELDS = ("tldr", "motivation", "method", "result", "conclusion")

# 单字段最小可接受长度：短于此值几乎一定是「无内容」而非有效解读
# （例如 "" / "." / "无"）。用于判定一条响应是否值得发布。
MIN_FIELD_LENGTH = 2


class Structure(BaseModel):
    """模型的输出结构（中文临床解读）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    tldr: str = Field(
        description="一句话核心结论：研究对象 + 主要发现（中文，40-90 字）",
    )
    motivation: str = Field(
        description="研究背景与动机：临床问题、既往证据缺口（中文）",
    )
    method: str = Field(
        description="研究方法：研究类型、研究对象、样本量、干预/暴露、终点与统计方法（中文）",
    )
    result: str = Field(
        description="主要结果：关键数值，含效应量与 95%CI / P 值（中文）",
    )
    conclusion: str = Field(
        description="结论与临床意义：对电生理实践的影响与局限性（中文）",
    )

    @field_validator(*AI_FIELDS, mode="before")
    @classmethod
    def _coerce_to_text(cls, value):
        """容忍模型把某个字段写成数字 / 列表 / None。

        模型偶尔会把 ``tldr`` 写成数字，或把 ``result`` 写成一个要点列表。
        直接抛 ValidationError 会丢掉整条响应（很贵），所以这里统一转成文本；
        真正的「空内容」由 :func:`is_meaningful` 判定，而不是靠类型报错。
        """
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple)):
            parts = []
            for item in value:
                if item is None:
                    continue
                if isinstance(item, dict):
                    parts.append("；".join(str(v) for v in item.values() if v is not None))
                else:
                    parts.append(str(item))
            return "；".join(p for p in parts if p)
        if isinstance(value, dict):
            return "；".join(str(v) for v in value.values() if v is not None)
        return str(value)

    def to_ai_dict(self) -> Dict[str, str]:
        """导出为写入 JSONL 的 ``AI`` 字典（只含五个契约字段）。"""
        data = self.model_dump()
        return {name: data.get(name, "") for name in AI_FIELDS}


def is_meaningful(values: Dict[str, object], min_length: int = MIN_FIELD_LENGTH) -> bool:
    """判断一份 AI 数据是否值得发布。

    这是「禁止占位符」契约的执行点：只要求 **至少一个** 字段有实质内容，
    而不是五个字段全部非空。原因是真实模型确实会出现「摘要里没有报告
    统计结果」这类情况，硬性要求五段全非空会让整篇论文被丢弃；
    而一个字段都没内容时，写出去的必然是占位文本，必须拒绝。

    纯函数：不读网络、不读磁盘，可单测。
    """
    if not isinstance(values, dict):
        return False
    for name in AI_FIELDS:
        value = values.get(name)
        if isinstance(value, str) and len(value.strip()) >= min_length:
            return True
    return False


def normalized_ai_dict(values: Dict[str, object]) -> Dict[str, str]:
    """补齐缺失字段为真正的空字符串（而不是占位文本）。

    渲染层用 ``item.AI.tldr`` 直接取值，缺键会渲染出 ``undefined``，
    所以五个键必须都在；但缺失的值必须是空串，不能是
    "Summary generation failed" 这类看起来像摘要的占位符。
    """
    source = values if isinstance(values, dict) else {}
    result: Dict[str, str] = {}
    for name in AI_FIELDS:
        value = source.get(name, "")
        result[name] = value if isinstance(value, str) else str(value)
    return result


__all__ = [
    "AI_FIELDS",
    "MIN_FIELD_LENGTH",
    "Structure",
    "is_meaningful",
    "normalized_ai_dict",
]
