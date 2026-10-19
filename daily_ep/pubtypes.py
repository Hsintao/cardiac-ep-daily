"""非研究性文献过滤：勘误、撤稿、纯指针类条目。

**为什么放在采集层而不是 relevance 层**：
``relevance`` 判断的是「这篇文献与电生理论不相关」；而这里判断的是
「这根本不是一篇有内容的论文」。两者是正交的，混在一起会让阈值调参变得难以理解 ——
一篇勘误通知即使主题完全对口（标题里全是 ablation/AF），也毫无阅读价值。

实测案例（曾漏进日报）::

    PMID 42890521 | Front Physiol
    title:   Correction: Machine learning for enhancing the ablation outcomes ...
    summary: [This corrects the article DOI: 10.3389/fphys.2026.1831826.].
    pub_types: ['Published Erratum']

它靠原标题里的 "ablation"/"AF" 拿到 0.54 分，读者点开只有一个 DOI 指针。

**两档规则**（对应 lead 的判定要求）：

1. ``HARD_DROP_TYPES`` —— 勘误/撤稿类，**无条件丢弃**（白名单期刊也丢）。
   这类条目永远没有独立内容。
2. ``SOFT_TYPES`` —— 社论/评论/通讯等，**有真实摘要才保留**。
   因为部分社论有独立观点，不能一刀切；但摘要为空或只是
   "[This corrects the article DOI: ...]" 这种指针的，等于没有内容，丢弃。

⚠️ 关键细节：空摘要只对 ``SOFT_TYPES`` 生效。**不能对所有论文生效** ——
实测 ``Journal Article`` 里有大量正常论文没有摘要（PubMed 未标引摘要），
其中包含必须保留的电生理论文，例如

    - "Optogenetic cardioversion: To depolarize or to hyperpolarize?"      (J Physiol)
    - "Comparison of AI-enabled cardiac chambers volumetry ..."            (Am J Prev Cardiol)
    - "A misleading wide-complex rhythm in an ICD recipient"               (Neth Heart J)

若对全类型套用「空摘要即丢」，上述三篇会被误杀。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Pattern, Sequence, Tuple

from .schema import Paper

logger = logging.getLogger(__name__)

#: 勘误/撤稿类：无条件丢弃。PubMed 的 PublicationType 写法见下方覆盖测试。
HARD_DROP_TYPES = frozenset(
    {
        "published erratum",
        "erratum",
        "retraction of publication",
        "retracted publication",
        "corrected and republished article",
        "retraction",
        "expression of concern",
    }
)

#: 社论/评论/通讯等：有真实摘要才保留（见模块 docstring 的判定说明）
SOFT_TYPES = frozenset(
    {
        "comment",
        "editorial",
        "news",
        "newspaper article",
        "letter",
        "congress",
        "overall",
        "preprint",
        "published erratum",  # 双保险：即便在硬过滤名单里也认识
    }
)

#: 「纯指针」摘要：只是指向另一篇文章，本身没有内容
POINTER_PATTERNS: Tuple[Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^\s*\[\s*this\s+(?:corrects|retracts|replaces|has\s+been\s+retracted)",
        r"^\s*this\s+(?:corrects|retracts|replaces)\s+the\s+article",
        r"^\s*\[\s*this\s+corrects",
        r"\bdoi:\s*10\.",
    )
)

#: 长度阈值：短于这个且含指针词的摘要视为纯指针
_MAX_POINTER_LEN = 160


@dataclass
class NonResearchStats:
    """过滤统计，用于日志与验收报告。"""

    before: int = 0
    after: int = 0
    dropped: List[Tuple[str, str, str]] = field(default_factory=list)  # (title, reason, pub_types)

    @property
    def dropped_count(self) -> int:
        return len(self.dropped)

    def summary(self) -> str:
        return f"过滤非研究性文献 {self.dropped_count} 篇（{self.before} 篇 → {self.after} 篇）"


def _norm_types(paper: Paper) -> List[str]:
    return [(t or "").strip().lower() for t in (paper.pub_types or []) if (t or "").strip()]


def is_pointer_only(summary: str) -> bool:
    """摘要是否只是指向另一篇文章的指针（没有实质内容）。"""
    text = (summary or "").strip()
    if not text:
        return True
    if len(text) > _MAX_POINTER_LEN:
        return False
    return any(p.search(text) for p in POINTER_PATTERNS)


def drop_reason(paper: Paper) -> Optional[str]:
    """返回丢弃原因；保留则返回 None。"""
    types = _norm_types(paper)

    # 1. 勘误/撤稿：无条件丢弃
    hit = [t for t in types if t in HARD_DROP_TYPES]
    if hit:
        return f"勘误/撤稿类({hit[0]})"

    # 2. 社论/评论等：必须有真实摘要
    soft = [t for t in types if t in SOFT_TYPES]
    if soft:
        if not (paper.summary or "").strip():
            return f"{soft[0]}且无摘要"
        if is_pointer_only(paper.summary):
            return f"{soft[0]}且摘要仅为指针"

    return None


def filter_non_research(papers: Iterable[Paper]) -> Tuple[List[Paper], NonResearchStats]:
    """过滤掉非研究性文献。返回 ``(保留的论文, 统计)``。

    ⚠️ 本过滤**不受期刊白名单豁免**：白名单期刊也会出勘误，
    而勘误对读者没有任何信息量。
    """
    papers = list(papers)
    stats = NonResearchStats(before=len(papers))
    kept: List[Paper] = []

    for paper in papers:
        reason = drop_reason(paper)
        if reason is None:
            kept.append(paper)
        else:
            stats.dropped.append((paper.title, reason, ",".join(paper.pub_types or [])))

    stats.after = len(kept)
    for title, reason, types in stats.dropped:
        logger.info("过滤非研究性文献: %s ← %s [%s]", title[:80], reason, types)
    if stats.dropped:
        logger.info("%s", stats.summary())
    return kept, stats
