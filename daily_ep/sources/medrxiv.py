"""medRxiv 预印本数据源（补充源）。

定位：抢「尚未通过同行评审、更未进 PubMed」的电生理预印本。medRxiv 的
Cardiovascular Medicine 分区是 EP 预印本的主要集散地。

API：`api.biorxiv.org <https://api.biorxiv.org/details/medrxiv/{from}/{to}/{cursor}>`_
按**投稿日**闭区间返回该窗口内全部帖子，每页 100 条，cursor 按 0/100/200 翻页。

收录策略（与 PubMed 侧「期刊 OR 主题词」对齐）：

  1. **分区限定** ``category == "cardiovascular medicine"`` —— medRxiv 全站
     主题词匹配噪声极大（``ablation`` 会命中蛋白模型优化论文，见
     ``europepmc.is_research_record`` 的实测记录），必须先锁分区；
  2. **主题词预过滤** —— 分区仍含心衰/影像等非 EP 内容，标题+摘要做小写
     子串匹配（等价于 PubMed 的 ``[tiab]``），漏网噪声交给下游
     ``relevance.py`` 打分。

字段约定：
  - ``journal`` 固定为 ``medRxiv``（预印本无期刊归属，日报按期刊分组展示，
    用服务器名占位，避免 Europe PMC 当年「空期刊无处归类」的问题）；
  - ``pub_types`` 固定 ``["Preprint"]`` —— 走 ``pubtypes.SOFT_TYPES`` 规则，
    无摘要/纯指针摘要的会被丢（medRxiv 强制要求摘要，正常不会触发）；
  - ``id``/``doi`` 用 medRxiv 自己的 ``10.1101/...``。预印本正式发表后
    DOI 换成期刊的，与 PubMed 记录去重靠**标题指纹**（``schema.dedup_keys``
    的兜底键），所以标题必须原样保留，不能做改写。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Dict, List, Optional, Sequence

from ..http import HttpClient, RateLimiter, build_client
from ..schema import DEFAULT_TOPIC, Paper
from .pubmed import date_range

logger = logging.getLogger(__name__)

API_BASE = "https://api.biorxiv.org/details/medrxiv"
SOURCE_NAME = "medrxiv"
PAGE_SIZE = 100
MAX_PAGES = 5

#: 只收这个分区（API 返回值是小写）
CATEGORY = "cardiovascular medicine"


def medrxiv_rate_limiter() -> RateLimiter:
    """api.biorxiv.org 无公开硬性速率，保守取 2 请求/秒。"""
    return RateLimiter(max_calls=2, period=1.0)


def matches_topics(title: str, abstract: str, topics: Sequence[str]) -> bool:
    """标题+摘要是否命中任一主题词（小写子串，等价 [tiab] 语义）。"""
    haystack = f"{title} {abstract}".lower()
    return any((t or "").strip().lower() in haystack for t in topics if (t or "").strip())


def _parse_authors(raw: str) -> List[str]:
    """``"Last, First; Last2, First2."`` → ``["First Last", "First2 Last2"]``。

    API 给的是分号分隔的「姓, 名」串，翻成与 PubMed 侧一致的「名 姓」。
    末尾句号是整个串的结束符，先整体去掉再切分 —— 在 chunk 上去会把
    名的首字母缩写（"John A."）的句号也吃掉。
    """
    raw = (raw or "").strip().rstrip(".")
    authors: List[str] = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "," in chunk:
            family, given = chunk.split(",", 1)
            name = f"{given.strip()} {family.strip()}".strip()
        else:
            name = chunk
        if name:
            authors.append(name)
    return authors


def parse_record(record: Dict, cfg) -> Optional[Paper]:
    """单条 medRxiv JSON → :class:`Paper`。

    返回 None 的情况：非心血管分区、缺标题/DOI、主题词预过滤未命中。
    """
    if (record.get("category") or "").strip().lower() != CATEGORY:
        return None
    title = (record.get("title") or "").strip().rstrip(".")
    doi = (record.get("doi") or "").strip()
    abstract = (record.get("abstract") or "").strip()
    if not title or not doi:
        return None
    if not matches_topics(title, abstract, getattr(cfg, "topics", []) or []):
        return None

    version = str(record.get("version") or "1").strip()
    url = f"https://www.medrxiv.org/content/{doi}"
    paper = Paper(
        id=doi,
        title=title,
        authors=_parse_authors(record.get("authors") or ""),
        summary=abstract,
        url=url,
        abs=url,
        pdf=f"https://www.medrxiv.org/content/{doi}v{version}.full.pdf",
        date=(record.get("date") or "").strip(),
        source=SOURCE_NAME,
        journal="medRxiv",
        doi=doi,
        pmid="",
        pub_types=["Preprint"],
        is_open_access=True,
        topic=DEFAULT_TOPIC,
        no_abstract=not abstract,
    )
    if cfg is not None:
        topic = cfg.classify(paper.title, paper.summary, paper.journal)
        paper.topic = topic
        paper.categories = [topic]
        paper.category = topic
    return paper


def parse_response(payload: Dict, cfg) -> List[Paper]:
    """整份 details 响应 → Paper 列表。无帖子/结构异常都返回空列表。"""
    collection = (payload or {}).get("collection") or []
    papers: List[Paper] = []
    skipped_category = 0
    for record in collection:
        if not isinstance(record, dict):
            continue
        if (record.get("category") or "").strip().lower() != CATEGORY:
            skipped_category += 1
            continue
        paper = parse_record(record, cfg)
        if paper is not None:
            papers.append(paper)
    if skipped_category:
        logger.debug("medRxiv 非心血管分区跳过 %d 条", skipped_category)
    return papers


def _total(payload: Dict) -> int:
    """messages[0].total 可能是 int 也可能是 str。"""
    messages = (payload or {}).get("messages") or []
    if not messages:
        return 0
    try:
        return int(messages[0].get("total") or 0)
    except (TypeError, ValueError):
        return 0


def fetch(
    cfg,
    days: int = 1,
    today: Optional[date] = None,
    client: Optional[HttpClient] = None,
) -> List[Paper]:
    """采集最近 ``days`` 天投到 medRxiv 心血管分区的 EP 预印本。"""
    window = date_range(days, today)
    start, end = min(window).isoformat(), max(window).isoformat()

    client = client or build_client(cfg, SOURCE_NAME, medrxiv_rate_limiter())
    max_papers = getattr(cfg, "max_papers", 0) or 0
    papers: List[Paper] = []

    cursor = 0
    for page in range(MAX_PAGES):
        payload = client.get_json(f"{API_BASE}/{start}/{end}/{cursor}")
        batch = parse_response(payload, cfg)
        papers.extend(batch)
        logger.info("medRxiv 第 %d 页：%d 条（cursor=%d）", page + 1, len(batch), cursor)
        if max_papers and len(papers) >= max_papers:
            papers = papers[:max_papers]
            break
        total = _total(payload)
        cursor += PAGE_SIZE
        if cursor >= total or not (payload.get("collection") or []):
            break
    return papers
