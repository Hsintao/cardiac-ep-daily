"""心脏电生理每日论文检索 —— 采集层。

模块划分：
  - ``schema``   Paper 记录契约（采集/AI/渲染三层共用）
  - ``config``   检索配置（期刊白名单、主题词、亚专业标签）
  - ``http``     共享 HTTP 客户端（限速 + 重试）
  - ``sources``  各数据源（pubmed / europepmc）
  - ``dedup``    跨源去重
  - ``collect``  CLI 入口
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
