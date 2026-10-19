"""检索配置：期刊白名单、主题词、亚专业标签映射、数据源开关。

单独成文件是为了让「改检索范围」不需要动代码逻辑 —— 这是这个日报最常调的参数。

关键设计（均由实测决定，勿随意改回）：
  - 使用 ``date_field: edat``（PubMed 入库日）。
    实测：EP 期刊近 2 天 ``[dp]`` 命中 0 篇，``[edat]`` 命中 16 篇。
    ``[dp]`` 是印刷/期号日期，在版文章常为未来日期，做日报会漏掉当天内容。
  - 主检索走 ``[jour]`` 期刊白名单 + ``[tiab]`` 主题词，不使用 [MeSH]。
    实测：``"cardiac electrophysiology"[MeSH]`` 近 30 天 0 篇、两年仅 46 篇，
    因为新文章在 PubMed 里常处于未标引状态（Publisher 状态）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


@dataclass
class SourceConfig:
    """单个数据源的开关与优先级。"""

    enabled: bool = True
    # 数值越小越先被采集；也是去重时「保留哪一条」的优先依据
    priority: int = 10


@dataclass
class Config:
    # ---- 日期 ----
    date_field: str = "edat"          # edat | dp | pdat，默认 edat（见模块 docstring）
    lookback_days: int = 1            # 除了今天，再往前看几天（补时区/入库延迟）

    # ---- 检索 ----
    journals: List[str] = field(default_factory=list)
    topics: List[str] = field(default_factory=list)

    # ---- 亚专业标签：标签名 -> 命中该标签的主题词（小写子串匹配）----
    topic_rules: Dict[str, List[str]] = field(default_factory=dict)

    # ---- 数据源 ----
    sources: Dict[str, SourceConfig] = field(default_factory=dict)

    # ---- 网络 ----
    ncbi_email: str = ""
    ncbi_api_key: str = ""
    timeout: int = 30
    max_retries: int = 3

    # ---- 输出 ----
    max_papers: int = 200             # 单日上限，防极端日期页面过大

    # ---------------------------------------------------------------
    @classmethod
    def load(cls, path: Path | str | None = None) -> "Config":
        path = Path(path) if path else DEFAULT_CONFIG_PATH
        raw: dict = {}
        if path.exists():
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        sources = {
            name: SourceConfig(**value) if isinstance(value, dict) else SourceConfig(enabled=bool(value))
            for name, value in (raw.get("sources") or {}).items()
        }

        cfg = cls(
            date_field=raw.get("date_field", "edat"),
            lookback_days=int(raw.get("lookback_days", 1)),
            journals=list(raw.get("journals") or []),
            topics=list(raw.get("topics") or []),
            topic_rules={k: list(v) for k, v in (raw.get("topic_rules") or {}).items()},
            sources=sources,
            ncbi_email=raw.get("ncbi_email", "") or os.environ.get("NCBI_EMAIL", ""),
            ncbi_api_key=os.environ.get("NCBI_API_KEY", ""),
            timeout=int(raw.get("timeout", 30)),
            max_retries=int(raw.get("max_retries", 3)),
            max_papers=int(raw.get("max_papers", 200)),
        )

        # 环境变量覆盖，方便云端/本地不改文件就调整
        if os.environ.get("JOURNALS"):
            cfg.journals = [j.strip() for j in os.environ["JOURNALS"].split(",") if j.strip()]
        if os.environ.get("TOPICS"):
            cfg.topics = [t.strip() for t in os.environ["TOPICS"].split(",") if t.strip()]
        cfg.lookback_days = int(os.environ.get("LOOKBACK_DAYS", cfg.lookback_days))
        cfg.max_papers = int(os.environ.get("MAX_PAPERS", cfg.max_papers))
        return cfg

    def enabled_sources(self) -> List[str]:
        """按 priority 升序返回启用的源名。未在配置里声明的源默认启用。"""
        if not self.sources:
            return ["pubmed", "europepmc"]
        items = [(name, sc) for name, sc in self.sources.items() if sc.enabled]
        items.sort(key=lambda kv: kv[1].priority)
        return [name for name, _ in items]

    def classify(self, title: str, abstract: str, journal: str = "") -> str:
        """把论文归入一个亚专业标签。

        按 ``topic_rules`` 的声明顺序做子串匹配，先命中者优先；
        都没命中则归入「其他」。规则顺序即优先级，所以配置文件里
        更具体的标签要写在更宽泛的标签前面。
        """
        haystack = f"{title} {abstract} {journal}".lower()
        for topic, keywords in self.topic_rules.items():
            for keyword in keywords:
                if keyword.lower() in haystack:
                    return topic
        return "其他"
