"""跨源去重。

同一篇文章常同时出现在 PubMed 与 Europe PMC（后者本身就是 PubMed 的镜像 + 扩展），
不去重的话日报里会出现两条一模一样的标题。

去重键的顺序由 ``schema.dedup_keys`` 定义：**DOI → PMID → 标题指纹**。
本模块负责把这个「一个论文多个键」的关系落成一张倒排索引：

    key -> 已收录论文在结果列表里的下标

收录一篇论文时，只要它的**任意一个**键已经出现过，就判定为重复。
新论文收录后，它的所有键都要登记，否则后续重复项会漏判。

「保留哪一条」：先看源的 ``priority``（配置里 pubmed=10 < europepmc=20，
即 PubMed 优先），同源内先到先得。**但有一条硬规则**：
如果重复的后来者带了摘要而先到者没有，则用后来者补齐元数据
（Europe PMC 常在 PubMed 还没标引时就给了摘要与 OA PDF）。
"""

from __future__ import annotations

import logging
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .schema import Paper, dedup_keys

logger = logging.getLogger(__name__)

#: 未知源的默认优先级（数值越大越不优先）
DEFAULT_PRIORITY = 50


def source_priority(cfg, source: str) -> int:
    """查配置里某个源的优先级；未声明则用默认值。"""
    sources = getattr(cfg, "sources", None) or {}
    entry = sources.get(source)
    if entry is None:
        return DEFAULT_PRIORITY
    return int(getattr(entry, "priority", DEFAULT_PRIORITY))


def _merge_metadata(target: Paper, incoming: Paper) -> Paper:
    """用 ``incoming`` 补齐 ``target`` 缺失的字段，不覆盖已有值。

    只补「空值」字段 —— 保留下来的那条（更高优先级）在标题/期刊上更权威，
    但在摘要、DOI、OA PDF 这些附属信息上，后来者可能更全。
    """
    if not target.summary and incoming.summary:
        target.summary = incoming.summary
        target.no_abstract = False
    if not target.doi and incoming.doi:
        target.doi = incoming.doi
    if not target.pmid and incoming.pmid:
        target.pmid = incoming.pmid
    if not target.pdf and incoming.pdf:
        target.pdf = incoming.pdf
    if not target.journal and incoming.journal:
        target.journal = incoming.journal
    if not target.date and incoming.date:
        target.date = incoming.date
    if not target.authors and incoming.authors:
        target.authors = incoming.authors
    if incoming.is_open_access:
        target.is_open_access = True
    for pub_type in incoming.pub_types:
        if pub_type not in target.pub_types:
            target.pub_types.append(pub_type)
    return target


def deduplicate(
    papers: Iterable[Paper],
    cfg=None,
    priorities: Optional[Dict[str, int]] = None,
) -> List[Paper]:
    """按 DOI → PMID → 标题指纹跨源去重，返回保序的结果列表。

    ``priorities`` 可显式指定源优先级（测试用）；不传则读 ``cfg``。
    传入顺序即「同优先级时的先到先得顺序」，所以调用方应先按优先级排好源。
    """

    def priority_of(source: str) -> int:
        if priorities is not None and source in priorities:
            return int(priorities[source])
        if cfg is not None:
            return source_priority(cfg, source)
        return DEFAULT_PRIORITY

    index: Dict[str, int] = {}
    kept: List[Paper] = []
    duplicates = 0
    merged = 0

    for paper in papers:
        keys = dedup_keys(paper)
        hit_at: Optional[int] = None
        for key in keys:
            if key in index:
                hit_at = index[key]
                break

        if hit_at is None:
            index.update({key: len(kept) for key in keys})
            kept.append(paper)
            continue

        duplicates += 1
        existing = kept[hit_at]
        # 只有严格更高优先级（数值更小）的后来者才替换
        if priority_of(paper.source) < priority_of(existing.source):
            paper = _merge_metadata(paper, existing)
            kept[hit_at] = paper
            # 重新登记键：替换后键集合可能变大（例如新增了 DOI）
            index.update({key: hit_at for key in dedup_keys(paper)})
        else:
            before = (existing.summary, existing.doi, existing.pdf)
            _merge_metadata(existing, paper)
            after = (existing.summary, existing.doi, existing.pdf)
            if before != after:
                merged += 1
            # 补齐后可能产生新键，同样要登记，避免第三条副本漏判
            index.update({key: hit_at for key in dedup_keys(existing)})

    if duplicates:
        logger.info("去重：跳过 %d 条重复，其中 %d 条补齐了元数据", duplicates, merged)
    return kept


def dedup_stats(papers: Sequence[Paper], cfg=None) -> Dict[str, int]:
    """按源统计篇数，便于在报告里核对去重效果。"""
    stats: Dict[str, int] = {}
    for paper in papers:
        stats[paper.source] = stats.get(paper.source, 0) + 1
    del cfg
    return stats


def sort_papers(papers: Sequence[Paper], cfg=None) -> List[Paper]:
    """排序：亚专业标签顺序 → 日期倒序 → 期刊名（见 ``schema.sort_key``）。"""
    from .schema import sort_key

    del cfg
    return sorted(papers, key=sort_key)


def top_n(papers: Sequence[Paper], limit: int) -> List[Paper]:
    """截断到 ``limit`` 篇（``config.max_papers``）。"""
    if limit and limit > 0:
        return list(papers)[:limit]
    return list(papers)


def summarize_sources(items: Sequence[Tuple[str, int, Optional[str]]]) -> str:
    """把 ``[(源名, 篇数, 错误)]`` 渲染成一行日志，便于 GitHub Actions 里看结果。"""
    chunks = []
    for name, count, error in items:
        chunks.append(f"{name}={count}" + (f"(失败: {error})" if error else ""))
    return " | ".join(chunks)
