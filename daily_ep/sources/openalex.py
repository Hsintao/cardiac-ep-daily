"""OpenAlex 数据源（补充源）。

定位：覆盖面最广的免费文献库（含未被 PubMed 收录的期刊与预印本），
做 PubMed/Europe PMC/Crossref 之外的兜底。

查询拆两路（OpenAlex 的 filter 语法不支持跨字段 OR，只能分开请求再合并）：

  1. **主题词路**：``title_and_abstract.search``（等价 PubMed 的 ``[tiab]``）。
     每个主题词单独一个请求 —— ``.search`` 类 filter 的 ``|`` 语义文档没有
     明确保证，逐词请求最稳，量级也小（2 天窗口每词通常只有一页）；
  2. **期刊路**：``primary_location.source.issn:ISSN1|ISSN2|...``（同字段
     ``|`` 是明确的 OR），一次请求覆盖全部白名单期刊。

两路都带 ``is_paratext:false``：服务端就排掉社论/勘误/信件这类 paratext，
省得下游 ``pubtypes.py`` 再猜（OpenAlex 不给 PubMed 那样的出版类型细分）。

字段约定：
  - 摘要从 ``abstract_inverted_index`` 还原（词 → 位置列表）；
  - ``journal`` 命中白名单 ISSN 时强制写成白名单刊名（与 Crossref 同理由：
    保持刊名一致、让 ``relevance.py`` 白名单直通生效），否则用
    ``primary_location.source.display_name``；
  - ``type`` 映射成 PubMed 风格 pub_types（article→Journal Article，
    review→Review，preprint→Preprint），让 ``pubtypes.py`` 的规则能接上。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Dict, List, Optional, Sequence

from ..http import HttpClient, RateLimiter, build_client
from ..schema import DEFAULT_TOPIC, Paper
from .pubmed import date_range

logger = logging.getLogger(__name__)

API_URL = "https://api.openalex.org/works"
SOURCE_NAME = "openalex"
PAGE_SIZE = 200
MAX_PAGES = 3

SELECT_FIELDS = (
    "id,doi,display_name,authorships,publication_date,primary_location,"
    "abstract_inverted_index,type,open_access,best_oa_location"
)

#: OpenAlex type → PubMed 风格 pub_types
TYPE_MAP = {
    "article": ["Journal Article"],
    "review": ["Review"],
    "preprint": ["Preprint"],
}


def openalex_rate_limiter() -> RateLimiter:
    """polite pool（带 mailto）官方限 10/s，留余量取 5/s。"""
    return RateLimiter(max_calls=5, period=1.0)


def _quote(term: str) -> str:
    """多词主题加引号按短语搜（OpenAlex search 支持引号短语）。"""
    term = (term or "").strip()
    if not term:
        return ""
    if term.startswith('"') and term.endswith('"'):
        return term
    return f'"{term}"' if " " in term else term


def reconstruct_abstract(inverted_index: Optional[Dict[str, List[int]]]) -> str:
    """``{"word": [pos, ...]}`` 倒排索引 → 正常文本。"""
    if not inverted_index:
        return ""
    positions: List[str] = []
    size = 0
    for positions_list in inverted_index.values():
        if positions_list:
            size = max(size, max(positions_list) + 1)
    positions = [""] * size
    for word, indices in inverted_index.items():
        for index in indices or []:
            if 0 <= index < size:
                positions[index] = word
    return " ".join(w for w in positions if w)


def _strip_doi(doi: str) -> str:
    """OpenAlex 的 doi 是完整 URL（``https://doi.org/10.xxx``），存裸 DOI。"""
    doi = (doi or "").strip()
    for prefix in ("https://doi.org/", "http://doi.org/"):
        if doi.lower().startswith(prefix):
            return doi[len(prefix):]
    return doi


def _parse_authors(item: Dict) -> List[str]:
    authors: List[str] = []
    for authorship in item.get("authorships") or []:
        if not isinstance(authorship, dict):
            continue
        name = ((authorship.get("author") or {}).get("display_name") or "").strip()
        if name:
            authors.append(name)
    return authors


def _source_issns(item: Dict) -> List[str]:
    source = ((item.get("primary_location") or {}).get("source") or {})
    issns = source.get("issn") or []
    if isinstance(issns, str):
        issns = [issns]
    issn_l = (source.get("issn_l") or "").strip()
    result = [str(i).strip() for i in issns if str(i).strip()]
    if issn_l and issn_l not in result:
        result.append(issn_l)
    return result


def _journal_name(item: Dict, issn_to_journal: Dict[str, str]) -> str:
    """命中白名单 ISSN 时用白名单刊名，否则用 OpenAlex 的 source 名。"""
    for issn in _source_issns(item):
        if issn in issn_to_journal:
            return issn_to_journal[issn]
    source = ((item.get("primary_location") or {}).get("source") or {})
    return (source.get("display_name") or "").strip()


def parse_work(item: Dict, issn_to_journal: Dict[str, str], cfg=None) -> Optional[Paper]:
    """单条 OpenAlex work → :class:`Paper`。缺标题返回 None。"""
    title = (item.get("display_name") or "").strip().rstrip(".")
    if not title:
        return None

    openalex_id = (item.get("id") or "").strip()
    doi = _strip_doi(item.get("doi") or "")
    abstract = reconstruct_abstract(item.get("abstract_inverted_index"))
    journal = _journal_name(item, issn_to_journal)
    best_oa = item.get("best_oa_location") or {}

    paper = Paper(
        id=doi or openalex_id,
        title=title,
        authors=_parse_authors(item),
        summary=abstract,
        url=f"https://doi.org/{doi}" if doi else openalex_id,
        abs=f"https://doi.org/{doi}" if doi else openalex_id,
        pdf=(best_oa.get("pdf_url") or "").strip(),
        date=(item.get("publication_date") or "").strip(),
        source=SOURCE_NAME,
        journal=journal,
        doi=doi,
        pmid="",
        pub_types=list(TYPE_MAP.get((item.get("type") or "").strip(), [])),
        is_open_access=bool((item.get("open_access") or {}).get("is_oa")),
        topic=DEFAULT_TOPIC,
        no_abstract=not abstract,
    )
    if cfg is not None:
        topic = cfg.classify(paper.title, paper.summary, paper.journal)
        paper.topic = topic
        paper.categories = [topic]
        paper.category = topic
    return paper


def _date_filter(window: Sequence[date]) -> str:
    return (
        f"from_publication_date:{min(window).isoformat()},"
        f"to_publication_date:{max(window).isoformat()},"
        "is_paratext:false"
    )


def works_params(
    filter_clause: str, cursor: str = "*", mailto: str = "", api_key: str = ""
) -> Dict[str, object]:
    """查询参数。

    ⚠️ OpenAlex 已改为预算制（2026 年实测）：无 key 时与**同 IP 所有人**
    共享 $0.10/天匿名额度（一次 search 查询约 $0.001，~100 次就耗尽）；
    免费 key 有独立 $1/天额度。生产使用必须通过 ``OPENALEX_API_KEY``
    环境变量带 key，否则日报跑到月中就可能整天 429。
    ``mailto`` 作为补充礼貌标识保留。
    """
    params: Dict[str, object] = {
        "filter": filter_clause,
        "per-page": PAGE_SIZE,
        "cursor": cursor,
        "select": SELECT_FIELDS,
    }
    if mailto:
        params["mailto"] = mailto
    if api_key:
        params["api_key"] = api_key
    return params


def fetch_query(
    client: HttpClient,
    filter_clause: str,
    issn_to_journal: Dict[str, str],
    cfg,
    seen: set,
    mailto: str = "",
    api_key: str = "",
) -> List[Paper]:
    """单路查询（cursor 翻页）。``seen`` 跨路去重（一篇可命中多个主题词）。"""
    papers: List[Paper] = []
    cursor = "*"
    for page in range(MAX_PAGES):
        payload = client.get_json(API_URL, works_params(filter_clause, cursor, mailto, api_key))
        results = payload.get("results") or []
        for item in results:
            if not isinstance(item, dict):
                continue
            paper = parse_work(item, issn_to_journal, cfg)
            if paper is None or not paper.id or paper.id in seen:
                continue
            seen.add(paper.id)
            papers.append(paper)
        meta = payload.get("meta") or {}
        next_cursor = meta.get("next_cursor")
        if not results or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return papers


def fetch(
    cfg,
    days: int = 1,
    today: Optional[date] = None,
    client: Optional[HttpClient] = None,
) -> List[Paper]:
    """采集最近 ``days`` 天 OpenAlex 收录的 EP 论文（主题词路 + 期刊路）。"""
    window = date_range(days, today)
    base_filter = _date_filter(window)

    journal_issn: Dict[str, str] = getattr(cfg, "journal_issn", {}) or {}
    issn_to_journal = {v.strip(): k for k, v in journal_issn.items() if v.strip()}

    client = client or build_client(cfg, SOURCE_NAME, openalex_rate_limiter())
    mailto = (getattr(cfg, "ncbi_email", "") or "").strip()
    api_key = (getattr(cfg, "openalex_api_key", "") or "").strip()
    if not api_key:
        logger.warning(
            "OpenAlex 未配 API key（OPENALEX_API_KEY），与整个 IP 共享 $0.10/天匿名额度，"
            "耗尽后整天 429；免费 key 申请见 https://openalex.org/settings/api"
        )
    max_papers = getattr(cfg, "max_papers", 0) or 0
    papers: List[Paper] = []
    seen: set = set()

    for topic in getattr(cfg, "topics", []) or []:
        topic = _quote(topic)
        if not topic:
            continue
        try:
            batch = fetch_query(
                client,
                f"{base_filter},title_and_abstract.search:{topic}",
                issn_to_journal,
                cfg,
                seen,
                mailto,
                api_key,
            )
        except Exception as exc:  # noqa: BLE001 —— 单路失败隔离，其余主题词继续
            logger.warning("OpenAlex 主题 %s 查询失败，跳过: %s", topic, exc)
            continue
        papers.extend(batch)
        logger.info("OpenAlex 主题 %s：%d 条", topic, len(batch))

    if issn_to_journal:
        issn_clause = "|".join(sorted(issn_to_journal))
        try:
            batch = fetch_query(
                client,
                f"{base_filter},primary_location.source.issn:{issn_clause}",
                issn_to_journal,
                cfg,
                seen,
                mailto,
                api_key,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("OpenAlex 期刊路查询失败，跳过: %s", exc)
            batch = []
        papers.extend(batch)
        logger.info("OpenAlex 期刊路：%d 条", len(batch))

    if max_papers and len(papers) > max_papers:
        papers = papers[:max_papers]
    return papers
