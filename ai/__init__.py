"""AI 增强层：把采集到的论文摘要转成中文五段式临床解读。

模块划分：
  - ``ai.structure``      pydantic 输出模型（五段式契约）
  - ``ai.json_repair``    纯函数的 JSON 提取 / 修复（无网络、无副作用、可单测）
  - ``ai.runtime``        DeepSeek 客户端构造 + 重试策略
  - ``ai.enhance``        主逻辑与 CLI

本包不依赖 langchain：直接用 openai SDK，减少一个中间层的解析不确定性
（langchain 的 OutputParserException 会把原始响应包进异常文本，反而更难修）。
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
