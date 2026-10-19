"""PubMed 数据源（主源）。

检索策略（每条都由实测决定，改动前请先读 config.yaml 顶部的注释）：

1. **日期字段用 ``[edat]``（Entrez 入库日），不用 ``[dp]``。**
   实测：EP 期刊近 2 天 ``[dp]`` 命中 0 篇、``[edat]`` 命中 16 篇。
   ``[dp]`` 是印刷/期号日期，在版文章（ahead-of-print）常被标成未来月份，
   用它做日报会把当天全部内容漏掉。入库日才是「今天 PubMed 多了哪些文章」的答案。

2. **期刊白名单 ``[jour]`` + 主题词 ``[tiab]``，不用 ``[MeSH]``。**
   实测 ``"cardiac electrophysiology"[MeSH]`` 近 30 天 0 篇、两年仅 46 篇：
   新文章在 PubMed 里常处于 Publisher 状态、尚未标引 MeSH，用它等于只看到旧文献。

3. 查询模板::

       ("A"[jour] OR "B"[jour] OR ... OR "T1"[tiab] OR "T2"[tiab] ...)
       AND ("YYYY/MM/DD"[edat] OR "YYYY/MM/DD"[edat] ...)

   期刊与主题词之间是 **OR**：白名单期刊里的文章本身就是目标领域，
   不必再额外命中主题词（否则会滤掉大量纯 EP 期刊的影像/基础研究）。
   日期之间也是 OR：日报要覆盖「最近 N 天」。

4. 速率限制：无 API key 3 请求/秒、有 key 10 请求/秒；必须带 ``tool=`` 与 ``email=``。
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional, Sequence

from ..http import HttpClient, build_client, ncbi_rate_limiter
from ..schema import DEFAULT_TOPIC, Paper

logger = logging.getLogger(__name__)

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

SOURCE_NAME = "pubmed"
TOOL_NAME = "daily-ep-paper-fetcher"

# efetch 一次请求的 PMID 上限。官方建议单次不要超过 200 条，
# 分批也能让某批失败时只损失这一批。
EFETCH_BATCH_SIZE = 150

# 单次 esearch 最多取回的 PMID 数；超过 max_papers 的部分本来也会被截断
ESEARCH_RETMAX = 500


# ---------------------------------------------------------------------------
# 检索式构造
# ---------------------------------------------------------------------------

def date_range(days: int, today: Optional[date] = None) -> List[date]:
    """返回从 ``today - (days-1)`` 到 ``today`` 的日期列表（升序）。

    ``days`` 为回溯窗口长度：``--days 2`` = 今天 + 昨天。
    """
    today = today or date.today()
    span = max(1, int(days))
    return [today - timedelta(days=offset) for offset in range(span - 1, -1, -1)]


def _quote(term: str) -> str:
    """把带空格的短语包成 PubMed 的精确短语。

    不加引号时 ``atrial fibrillation[tiab]`` 会被自动拆词（AF AND ... AND ...），
    容易带入不相关命中；加引号后按短语匹配。
    """
    term = (term or "").strip()
    if not term:
        return ""
    if term.startswith('"') and term.endswith('"'):
        return term
    return f'"{term}"' if " " in term else term


def build_clause(terms: Sequence[str], field: str) -> str:
    """把一组词拼成 ``(a[field] OR b[field])``。空集合返回空串。"""
    parts = [f"{_quote(t)}[{field}]" for t in terms if (t or "").strip()]
    if not parts:
        return ""
    if len(parts) == 1:
        return f"({parts[0]})"
    return "(" + " OR ".join(parts) + ")"


def build_date_clause(days: Iterable[date], date_field: str = "edat") -> str:
    """``("2026/10/16"[edat] OR "2026/10/17"[edat])``"""
    rendered = [f'"{d.strftime("%Y/%m/%d")}"[{date_field}]' for d in days]
    if not rendered:
        return ""
    if len(rendered) == 1:
        return f"({rendered[0]})"
    return "(" + " OR ".join(rendered) + ")"


def build_query(
    journals: Sequence[str],
    topics: Sequence[str],
    days: Iterable[date],
    date_field: str = "edat",
) -> str:
    """按模板拼出完整检索式。

    期刊与主题词同为 OR 组；两组都没有时只按日期检索（用于排查）。
    """
    journal_clause = build_clause(journals, "jour")
    topic_clause = build_clause(topics, "tiab")
    scope_parts = [c for c in (journal_clause, topic_clause) if c]
    scope = " OR ".join(scope_parts)
    if len(scope_parts) > 1:
        scope = f"({scope})"

    date_clause = build_date_clause(days, date_field)
    if scope and date_clause:
        return f"{scope} AND {date_clause}"
    return scope or date_clause


def esearch_params(query: str, cfg, retmax: int = ESEARCH_RETMAX) -> Dict[str, object]:
    """esearch 查询参数。``tool`` 与 ``email`` 是 NCBI 的硬性要求。"""
    params: Dict[str, object] = {
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retmax": retmax,
        "retstart": 0,
        "sort": "pub_date",
        "tool": TOOL_NAME,
        "email": getattr(cfg, "ncbi_email", "") or "anonymous@example.com",
    }
    api_key = getattr(cfg, "ncbi_api_key", "")
    if api_key:
        params["api_key"] = api_key
    return params


def efetch_params(pmids: Sequence[str], cfg) -> Dict[str, object]:
    """efetch 查询参数（retmode=xml，标准库解析）。"""
    params: Dict[str, object] = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
        "tool": TOOL_NAME,
        "email": getattr(cfg, "ncbi_email", "") or "anonymous@example.com",
    }
    api_key = getattr(cfg, "ncbi_api_key", "")
    if api_key:
        params["api_key"] = api_key
    return params


def search_pmids(client: HttpClient, query: str, cfg) -> List[str]:
    """esearch → PMID 列表。"""
    payload = client.get_json(ESEARCH_URL, esearch_params(query, cfg))
    result = payload.get("esearchresult") or {}
    if result.get("error"):
        raise RuntimeError(f"PubMed esearch 返回错误: {result['error']}")
    pmids = [str(p) for p in (result.get("idlist") or []) if str(p).strip()]
    logger.info("PubMed esearch 命中 %s 篇（查询串 %d 字符）", result.get("count", len(pmids)), len(query))
    return pmids


def fetch_xml(client: HttpClient, pmids: Sequence[str], cfg) -> str:
    """efetch → 原始 XML 文本。"""
    return client.get(EFETCH_URL, efetch_params(pmids, cfg))


# ---------------------------------------------------------------------------
# XML 解析
# ---------------------------------------------------------------------------

def _text(node: Optional[ET.Element]) -> str:
    """取节点全部文本（含子节点，如 <i>/<sub>），并压缩空白。

    标题里的斜体、上下标会被解析成子元素，``node.text`` 只给第一段；
    用 ``itertext()`` 才能拼回完整标题。
    """
    if node is None:
        return ""
    return " ".join("".join(node.itertext()).split())


def _join_abstract(article: ET.Element) -> str:
    """拼接摘要的所有段落。

    结构化摘要有多个 ``<AbstractText Label="BACKGROUND">`` 段落，
    标签（BACKGROUND/METHODS/...）要保留——临床读者靠它快速定位结果。
    """
    chunks: List[str] = []
    for node in article.findall(".//Abstract/AbstractText"):
        body = _text(node)
        if not body:
            continue
        label = (node.get("Label") or "").strip()
        if label and label.upper() not in {"UNLABELLED", "UNASSIGNED"}:
            chunks.append(f"{label.upper()}: {body}")
        else:
            chunks.append(body)
    return "\n\n".join(chunks)


def _parse_pub_date(article: ET.Element) -> str:
    """解析出版日期（``<PubDate>``，可能是 MedlineDate 这种自由文本）。"""
    node = article.find(".//Journal/JournalIssue/PubDate")
    if node is None:
        node = article.find(".//ArticleDate")
    if node is None:
        return ""
    year = _text(node.find("Year"))
    month = _text(node.find("Month"))
    day = _text(node.find("Day"))
    medline = _text(node.find("MedlineDate"))
    if not year and medline:
        # 形如 "2026 Jan-Feb" / "2026 Winter"
        year = medline.split()[0] if medline.split() else ""
        month = medline.split()[1] if len(medline.split()) > 1 else ""
    month = _month_number(month)
    if not year:
        return ""
    if month and day:
        return f"{year}-{month}-{day.zfill(2)}"
    if month:
        return f"{year}-{month}"
    return year


_MONTHS = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
    "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
    "january": "01", "february": "02", "march": "03", "april": "04", "june": "06",
    "july": "07", "august": "08", "september": "09", "october": "10",
    "november": "11", "december": "12",
}


def _month_number(month: str) -> str:
    """把 ``Oct`` / ``10`` 统一成两位数字。"""
    month = (month or "").strip()
    if not month:
        return ""
    if month.isdigit():
        return month.zfill(2)
    return _MONTHS.get(month[:3].lower(), "")


def _entry_date(article: ET.Element) -> str:
    """取「入库日」：``PubMedPubDate PubStatus="entrez"`` 优先，依次回退。

    与检索用的 ``[edat]`` 对齐，保证日报显示的日期就是筛选依据；
    ``[edat]`` 缺失时回退到 ``pubmed`` 状态，最后才用出版日期。
    """
    history = article.find(".//PubmedData/History")
    if history is not None:
        for status in ("entrez", "pubmed", "medline"):
            for node in history.findall("PubMedPubDate"):
                if (node.get("PubStatus") or "").lower() != status:
                    continue
                rendered = _render_pubmed_date(node)
                if rendered:
                    return rendered
    pub_date = _parse_pub_date(article)
    if pub_date:
        return pub_date
    # MedlineCitation/DateCompleted 兜底
    node = article.find(".//DateCompleted")
    if node is not None:
        rendered = _render_pubmed_date(node)
        if rendered:
            return rendered
    return ""


def _render_pubmed_date(node: ET.Element) -> str:
    """``<Year>/<Month>/<Day>`` → ``YYYY-MM-DD``。"""
    year = _text(node.find("Year"))
    month = _month_number(_text(node.find("Month")))
    day = _text(node.find("Day"))
    if not year:
        return ""
    if month and day:
        return f"{year}-{month}-{day.zfill(2)}"
    if month:
        return f"{year}-{month}"
    return year


def _parse_authors(article: ET.Element) -> List[str]:
    """作者列表，格式 ``Given Family``；集体署名（CollectiveName）原样保留。"""
    authors: List[str] = []
    for node in article.findall(".//AuthorList/Author"):
        collective = _text(node.find("CollectiveName"))
        if collective:
            authors.append(collective)
            continue
        last = _text(node.find("LastName"))
        fore = _text(node.find("ForeName")) or _text(node.find("Initials"))
        if last and fore:
            authors.append(f"{fore} {last}")
        elif last:
            authors.append(last)
    return authors


def _parse_doi(article: ET.Element) -> str:
    """DOI：``PubmedData/ArticleIdList`` 优先，回退 ``ELocationID``。"""
    for node in article.findall(".//PubmedData/ArticleIdList/ArticleId"):
        if (node.get("IdType") or "").lower() == "doi":
            value = _text(node)
            if value:
                return value
    for node in article.findall(".//ELocationID"):
        if (node.get("EIdType") or "").lower() == "doi":
            value = _text(node)
            if value:
                return value
    return ""


def _parse_pub_types(article: ET.Element) -> List[str]:
    """出版类型（Review / Practice Guideline / Randomized Controlled Trial ...）。"""
    seen: List[str] = []
    for node in article.findall(".//PublicationTypeList/PublicationType"):
        value = _text(node)
        if value and value not in seen:
            seen.append(value)
    return seen


def parse_article(article: ET.Element, cfg=None) -> Optional[Paper]:
    """单个 ``<PubmedArticle>`` → :class:`Paper`。缺关键字段返回 None。"""
    pmid = _text(article.find(".//MedlineCitation/PMID"))
    title = _text(article.find(".//Article/ArticleTitle"))
    if not pmid or not title:
        return None

    abstract = _join_abstract(article)
    journal = _text(article.find(".//Journal/ISOAbbreviation")) or _text(
        article.find(".//Journal/Title")
    )
    doi = _parse_doi(article)
    pub_types = _parse_pub_types(article)
    entry_date = _entry_date(article)

    paper = Paper(
        id=pmid,
        title=title,
        authors=_parse_authors(article),
        summary=abstract,
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        abs=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        pdf="",  # PubMed 不直接给 OA PDF，由 Europe PMC 补充
        date=entry_date,
        source=SOURCE_NAME,
        journal=journal,
        doi=doi,
        pmid=pmid,
        pub_types=pub_types,
        is_open_access=False,
        topic=DEFAULT_TOPIC,
        # 社论/评论类常常一行摘要都没有：显式标记，AI 层据此跳过
        no_abstract=not abstract.strip(),
    )
    if cfg is not None:
        topic = cfg.classify(paper.title, paper.summary, paper.journal)
        paper.topic = topic
        paper.categories = [topic]
        paper.category = topic
    return paper


def parse_articles(xml_text: str, cfg=None) -> List[Paper]:
    """整份 efetch XML → Paper 列表。

    标准库 ``xml.etree``（本机 conda 环境没有 lxml）。
    DTD 声明不会触发外部实体下载（ElementTree 不解析 DTD），所以离线可测。
    """
    if not (xml_text or "").strip():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.error("PubMed XML 解析失败: %s", exc)
        raise
    papers: List[Paper] = []
    for article in root.iter("PubmedArticle"):
        paper = parse_article(article, cfg)
        if paper is not None:
            papers.append(paper)
    return papers


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def fetch(
    cfg,
    days: int = 1,
    today: Optional[date] = None,
    client: Optional[HttpClient] = None,
) -> List[Paper]:
    """采集最近 ``days`` 天入库的论文。

    ``client`` 可注入（测试用），不传则按配置构造（含 NCBI 限速）。
    """
    window = date_range(days, today)
    query = build_query(cfg.journals, cfg.topics, window, cfg.date_field)
    logger.info("PubMed 检索式: %s", query)

    client = client or build_client(cfg, SOURCE_NAME, ncbi_rate_limiter(cfg.ncbi_api_key))
    pmids = search_pmids(client, query, cfg)
    if not pmids:
        return []
    if getattr(cfg, "max_papers", 0):
        pmids = pmids[: cfg.max_papers]

    papers: List[Paper] = []
    for start in range(0, len(pmids), EFETCH_BATCH_SIZE):
        batch = pmids[start : start + EFETCH_BATCH_SIZE]
        xml_text = fetch_xml(client, batch, cfg)
        papers.extend(parse_articles(xml_text, cfg))
        logger.info("PubMed efetch 第 %d 批：%d 条", start // EFETCH_BATCH_SIZE + 1, len(batch))
    return papers
