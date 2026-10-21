"""Crossref 数据源（补充源）。

定位：抢「出版社已上线、PubMed 还没入库」的 online-first 论文。Crossref 的
元数据由出版商在上线当天注册，通常比 PubMed ``[edat]`` 早几天到几周 ——
README 里为入库延迟专门把定时调到 UTC 22:00，本源从结构上补上这个窗口。

策略：**只按期刊白名单查，不做主题词检索**。
Crossref 的 ``query.bibliographic`` 是相关性排序的全元数据模糊搜索，不是
标题/摘要的短语匹配，噪声无法控制；主题词维度的补充交给 OpenAlex。
逐期刊走 ``/journals/{issn}/works`` + ``from-pub-date/until-pub-date``，
ISSN 来自 config.yaml 的 ``journal_issn`` 映射（刊名检索歧义大，不猜）。

字段约定：
  - ``journal`` **强制写成白名单里的刊名**（即 config 的 key），而不是
    Crossref 的 ``container-title`` —— 后者是全称（如 "JACC: Clinical
    Electrophysiology"），既与 PubMed 的 ISO 缩写不一致，也会让
    ``relevance.py`` 的白名单直通（精确匹配）失效；
  - ``pub_types`` 留空：Crossref 的 ``type`` 粒度只有 journal-article，
    分不出社论/勘误，写 "Journal Article" 反而给 ``pubtypes.py`` 错误暗示；
  - ``pdf`` 留空：日报不依赖 PDF（Europe PMC 会补 OA 链接）。
"""

from __future__ import annotations

import html
import logging
import re
from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple

from ..http import HttpClient, RateLimiter, build_client
from ..schema import DEFAULT_TOPIC, Paper
from .pubmed import date_range

logger = logging.getLogger(__name__)

API_BASE = "https://api.crossref.org"
SOURCE_NAME = "crossref"
PAGE_SIZE = 200
MAX_PAGES = 3

#: select 只取需要的字段，单条响应从 ~4KB 压到 ~1KB
SELECT_FIELDS = (
    "DOI,title,author,published,published-online,published-print,issued,"
    "container-title,abstract,type,URL"
)

_TAG_RE = re.compile(r"<[^>]+>")


def crossref_rate_limiter() -> RateLimiter:
    """polite pool（带 mailto）官方期望 ≤50/s，日报量级远用不到，取 5/s。"""
    return RateLimiter(max_calls=5, period=1.0)


def _first(value) -> str:
    if isinstance(value, (list, tuple)):
        for item in value:
            text = _first(item)
            if text:
                return text
        return ""
    if value is None:
        return ""
    return str(value).strip()


def _parse_date(item: Dict) -> str:
    """online 优先于 print/issued：日报关心的是「哪天能读到」。

    Crossref 的 date-parts 是 ``[[2026, 10, 20]]``，月/日可缺省。
    """
    for field in ("published-online", "published", "published-print", "issued"):
        node = item.get(field) or {}
        parts = node.get("date-parts") or []
        if not parts or not parts[0]:
            continue
        numbers = parts[0]
        year = numbers[0]
        month = numbers[1] if len(numbers) > 1 else None
        day = numbers[2] if len(numbers) > 2 else None
        if month and day:
            return f"{year:04d}-{month:02d}-{day:02d}"
        if month:
            return f"{year:04d}-{month:02d}"
        return f"{year:04d}"
    return ""


def _parse_abstract(item: Dict) -> str:
    """Crossref 摘要是 JATS XML（``<jats:p>`` 等），去标签 + 反转义。"""
    raw = _first(item.get("abstract"))
    if not raw:
        return ""
    text = _TAG_RE.sub(" ", raw)
    return " ".join(html.unescape(text).split())


def _parse_authors(item: Dict) -> List[str]:
    authors: List[str] = []
    for author in item.get("author") or []:
        if not isinstance(author, dict):
            continue
        name = _first(author.get("name"))  # 集体署名
        if not name:
            family = _first(author.get("family"))
            given = _first(author.get("given"))
            name = f"{given} {family}".strip()
        if name:
            authors.append(name)
    return authors


def parse_work(item: Dict, journal: str, cfg=None) -> Optional[Paper]:
    """单条 Crossref work → :class:`Paper`。缺 DOI/标题返回 None。

    ``journal`` 用白名单刊名覆盖 ``container-title``（见模块 docstring）。
    """
    doi = _first(item.get("DOI"))
    title = _first(item.get("title")).rstrip(".")
    if not doi or not title:
        return None

    abstract = _parse_abstract(item)
    paper = Paper(
        id=doi,
        title=title,
        authors=_parse_authors(item),
        summary=abstract,
        url=_first(item.get("URL")) or f"https://doi.org/{doi}",
        abs=f"https://doi.org/{doi}",
        pdf="",
        date=_parse_date(item),
        source=SOURCE_NAME,
        journal=journal,
        doi=doi,
        pmid="",
        pub_types=[],
        is_open_access=False,
        topic=DEFAULT_TOPIC,
        no_abstract=not abstract,
    )
    if cfg is not None:
        topic = cfg.classify(paper.title, paper.summary, paper.journal)
        paper.topic = topic
        paper.categories = [topic]
        paper.category = topic
    return paper


def works_params(start: date, end: date, cursor: str = "*", mailto: str = "") -> Dict[str, object]:
    """``/journals/{issn}/works`` 查询参数。``mailto`` 进 polite pool。"""
    params: Dict[str, object] = {
        "filter": f"from-pub-date:{start.isoformat()},until-pub-date:{end.isoformat()},type:journal-article",
        "rows": PAGE_SIZE,
        "cursor": cursor,
        "select": SELECT_FIELDS,
    }
    if mailto:
        params["mailto"] = mailto
    return params


def fetch_journal(
    client: HttpClient,
    issn: str,
    journal: str,
    window: Sequence[date],
    cfg,
    mailto: str = "",
) -> List[Paper]:
    """单期刊采集：按 ISSN 拉取窗口内的 works（cursor 翻页）。"""
    start, end = min(window), max(window)
    url = f"{API_BASE}/journals/{issn}/works"
    max_papers = getattr(cfg, "max_papers", 0) or 0
    papers: List[Paper] = []

    cursor = "*"
    for page in range(MAX_PAGES):
        payload = client.get_json(url, works_params(start, end, cursor, mailto))
        message = payload.get("message") or {}
        items = message.get("items") or []
        for item in items:
            if not isinstance(item, dict):
                continue
            paper = parse_work(item, journal, cfg)
            if paper is not None:
                papers.append(paper)
        logger.info("Crossref %s 第 %d 页：%d 条", journal, page + 1, len(items))
        if max_papers and len(papers) >= max_papers:
            return papers[:max_papers]
        next_cursor = _first(message.get("next-cursor"))
        if not items or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return papers


def fetch(
    cfg,
    days: int = 1,
    today: Optional[date] = None,
    client: Optional[HttpClient] = None,
) -> List[Paper]:
    """采集最近 ``days`` 天白名单期刊在 Crossref 注册的论文。"""
    window = date_range(days, today)
    journal_issn: Dict[str, str] = getattr(cfg, "journal_issn", {}) or {}

    targets: List[Tuple[str, str]] = []  # (白名单刊名, ISSN)
    for journal in getattr(cfg, "journals", []) or []:
        issn = (journal_issn.get(journal) or "").strip()
        if issn:
            targets.append((journal, issn))
        else:
            logger.warning("Crossref 跳过 %s：config.yaml 的 journal_issn 里没有它的 ISSN", journal)

    client = client or build_client(cfg, SOURCE_NAME, crossref_rate_limiter())
    mailto = (getattr(cfg, "ncbi_email", "") or "").strip()
    papers: List[Paper] = []
    seen_dois: set = set()
    for journal, issn in targets:
        try:
            batch = fetch_journal(client, issn, journal, window, cfg, mailto)
        except Exception as exc:  # noqa: BLE001 —— 单期刊失败隔离，与 collect 层同哲学
            logger.warning("Crossref 期刊 %s (%s) 采集失败，跳过: %s", journal, issn, exc)
            continue
        for paper in batch:
            # 同一 DOI 可能被相邻窗口/重复 ISSN 命中两次，源内先去一道
            if paper.doi.lower() in seen_dois:
                continue
            seen_dois.add(paper.doi.lower())
            papers.append(paper)
    return papers
