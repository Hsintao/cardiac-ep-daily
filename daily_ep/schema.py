"""统一的论文记录定义（采集层 → AI 层 → 渲染层的唯一契约）。

字段刻意与 daily-arXiv-ai-enhanced 的 JSONL 保持一致，这样前端 js/app.js
可以原样复用：

    id, title, authors[], summary, url, abs, pdf, date,
    categories[], category, AI:{tldr, motivation, method, result, conclusion}

医疗领域的适配点：
  - ``source``    记录来源库（pubmed / europepmc / medrxiv ...），便于溯源与调试
  - ``journal``   期刊名（arXiv 没有这个概念，日报里很重要）
  - ``pub_types`` PubMed 出版类型（Review / Practice Guideline / RCT ...），用于分级
  - ``topic``     亚专业标签（房颤消融 / 器械治疗 ...），替代 arXiv 的 cs.CV 分类

``categories`` 是与前端约定的主分类字段，这里统一填充为亚专业标签，
既满足前端 ``item["categories"][0]`` 的取值方式，又让日报按临床主题分组。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

# 亚专业标签的展示顺序，同时决定 HTML 分组顺序（越靠前越靠上）。
TOPIC_ORDER: List[str] = [
    "房颤与消融",
    "室性心律失常",
    "器械治疗",
    "心力衰竭与代谢",
    "影像与标测",
    "遗传与基础",
    "其他",
]

DEFAULT_TOPIC = "其他"


@dataclass
class Paper:
    """一篇论文。字段与前端契约一一对应。"""

    id: str
    title: str
    authors: List[str] = field(default_factory=list)
    summary: str = ""
    url: str = ""          # 详情页链接（前端主链接）
    abs: str = ""          # 与 url 相同，兼容参考项目字段名
    pdf: str = ""          # 开放获取时的 PDF 链接，没有则留空
    date: str = ""         # 首次公开/入库日期 YYYY-MM-DD
    categories: List[str] = field(default_factory=lambda: [DEFAULT_TOPIC])
    category: str = DEFAULT_TOPIC

    # ---- 医疗领域扩展字段 ----
    source: str = ""                      # pubmed / europepmc / medrxiv / biorxiv
    journal: str = ""
    doi: str = ""
    pmid: str = ""
    pub_types: List[str] = field(default_factory=list)
    is_open_access: bool = False
    topic: str = DEFAULT_TOPIC
    no_abstract: bool = False             # 社论/评论类常常没有摘要

    AI: Optional[Dict[str, str]] = None   # 由 ai/enhance.py 填充

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        # 主分类字段与亚专业标签保持同步，避免两处不一致。
        data["category"] = self.topic
        data["categories"] = [self.topic]
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Paper":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        payload = {k: v for k, v in data.items() if k in known}
        # categories 在旧数据里可能是列表，转成 topic 单一来源。
        if "topic" not in payload and payload.get("categories"):
            payload["topic"] = payload["categories"][0]
        return cls(**payload)


# --------------------------------------------------------------------------
# 去重指纹
# --------------------------------------------------------------------------

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    """标题归一化，用于跨库去重。

    大小写、标点、多余空白在不同数据库间差异极大，直接比较会漏判。
    统一转小写、去掉标点、压缩空白，并做 Unicode 规范化（NFKC）以兼容
    全角字符与连字符变体（如 "P‐wave" 用的非 ASCII 连字符）。
    """
    if not title:
        return ""
    text = unicodedata.normalize("NFKC", title)
    text = text.lower()
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text)
    return text.strip()


def dedup_keys(paper: Paper) -> List[str]:
    """返回该论文可用于去重的键，按优先级排列。

    DOI 最可靠；PMID 次之；标题指纹兜底。调用方按顺序查表，
    命中任意一个就视为重复。
    """
    keys: List[str] = []
    if paper.doi:
        keys.append("doi:" + paper.doi.strip().lower())
    if paper.pmid:
        keys.append("pmid:" + paper.pmid.strip())
    normalized = normalize_title(paper.title)
    if normalized:
        keys.append("title:" + normalized)
    if not keys:
        keys.append("id:" + paper.id)
    return keys


def sort_key(paper: Paper) -> tuple:
    """排序：先按亚专业标签的约定顺序，再按日期倒序、期刊名。"""
    topic = paper.topic if paper.topic in TOPIC_ORDER else DEFAULT_TOPIC
    return (TOPIC_ORDER.index(topic), _neg_date(paper.date), paper.journal or "")


def _neg_date(date: str) -> str:
    """把日期转成可升序排序的倒序键（新日期排前面）。

    日期缺失时给一个极大值，保证排在同期有日期的条目之后。
    """
    return "0" if not date else "".join(chr(ord("9") - int(c)) if c.isdigit() else c for c in date)
