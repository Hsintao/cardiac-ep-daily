"""Europe PMC 数据源（补充源）。

.. warning::

   **本源不是 PubMed 的冗余镜像，不要因为「结果大量重叠」而删掉它。**
   两者的日期维度不同：

   ====  ==================  ==========================================
   源     日期字段             含义
   ====  ==================  ==========================================
   PubMed  ``[edat]``           Entrez **入库日**（今天进了哪些文章）
   EPMC    ``FIRST_PDATE``      **首次出版日**（文章本身标称哪天发表）
   ====  ==================  ==========================================

   实测 7 天窗口：Europe PMC 命中 34 条（28 条非预印本），其中若干条
   （如 *Global Advances in Ventricular Tachycardia*、
   *Atrial fibroblast metabolic reprogramming*）是 PubMed 按 ``edat``
   **抓不到的时间窗**——它们出版日和入库日不在同一天。
   日报要的是「最近发表的电生理文献」，两个维度都需要，所以本源必须保留。

   ⚠️ 由此带来的后果：这类「PubMed 还没入库」的记录**常常没有 pmid**
   （``journalInfo`` 有、``pmid`` 为 None）。因此跨源去重**不能只依赖 pmid**，
   必须依靠 DOI 或标题指纹（见 ``daily_ep/dedup.py``）。

本源的定位与边界：
  - 只收 MEDLINE/PMC/AGR 记录（``SRC:`` 服务端过滤 + 客户端 ``is_research_record`` 双保险）；
  - **不收预印本（PPR）**：无期刊归属、无 pmid，且主题词匹配噪声极大；
  - 补充开放获取 PDF 链接（``isOpenAccess=Y`` + ``fullTextUrlList``）。

⚠️ 必须用 ``TITLE_ABS:`` 限定字段。实测：不加字段限定时搜
``"cardiac electrophysiology"`` 会命中 Cell Reports 的脑瘤论文 ——
Europe PMC 默认在全文/所有字段里匹配，假阳性严重。

日期过滤写法是 ``FIRST_PDATE:[YYYY-MM-DD TO YYYY-MM-DD]``（与 PubMed 的 [edat] 不同，
它是闭区间，且日期用短横线）。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Dict, List, Optional, Sequence

from ..http import HttpClient, RateLimiter, build_client
from ..schema import DEFAULT_TOPIC, Paper
from .pubmed import date_range

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
SOURCE_NAME = "europepmc"
PAGE_SIZE = 100
MAX_PAGES = 3

#: 允许收录的 Europe PMC source 值：MED=MEDLINE、PMC=PubMed Central、AGR=AGRICOLA。
#: PPR（预印本）被排除，见 ``is_research_record``。
ALLOWED_SOURCES = {"MED", "PMC", "AGR"}

#: 可信的 OA PDF 站点（Europe PMC 的 fullTextUrlList 里混有落地页）
TRUSTED_PDF_SITES = {"europepmc", "pubmedcentral", "pmc", "arxiv"}

#: 期刊名兜底值。日报按期刊展示/筛选，journal 留空会让条目无处归类，
#: 所以宁可显式标注「未知期刊」也不留空字符串。
UNKNOWN_JOURNAL = "未知期刊"


def europepmc_rate_limiter() -> RateLimiter:
    """Europe PMC 没有像 NCBI 那样公开的硬性速率，保守取 5 请求/秒。"""
    return RateLimiter(max_calls=5, period=1.0)


def _quote(term: str) -> str:
    term = (term or "").strip()
    if not term:
        return ""
    if term.startswith('"') and term.endswith('"'):
        return term
    return f'"{term}"' if " " in term else term


def build_query(journals: Sequence[str], topics: Sequence[str], window: Sequence[date]) -> str:
    """构造 Europe PMC 检索式。

    与 PubMed 同样采用「期刊白名单 OR 主题词」的范围组，但有两个差异：

    - 主题词强制加 ``TITLE_ABS:`` 字段限定（否则会命中全文里的偶然提及）；
    - 期刊名用 ``JOURNAL:"X"`` **或** ``TITLE_ABS:"X"``。
      少了后者会漏掉 ``source == PMC`` 的记录（实测 PMC 记录的检索索引覆盖
      不如 MEDLINE，只写 JOURNAL: 时同一窗口 7 条只回 4 条）。
      PMC 记录常常没有 pmid、只有 pmcid，只靠 PubMed 会整条丢掉。
    """
    parts: List[str] = []
    for journal in journals:
        journal = (journal or "").strip()
        if journal:
            quoted = _quote(journal)
            # 期刊名本身也常出现在标题里（子刊/系列），一并匹配后由 is_research_record 兜底
            parts.append(f"JOURNAL:{quoted}")
            parts.append(f"TITLE_ABS:{quoted}")
    for topic in topics:
        if (topic or "").strip():
            parts.append(f"TITLE_ABS:{_quote(topic)}")
    scope = " OR ".join(parts)
    if len(parts) > 1:
        scope = f"({scope})"

    # 服务端就排掉预印本，少传无用数据；客户端 is_research_record 再兜一层。
    # 实测这个条件不会额外收紧结果（7 条里筛掉 3 条 PPR，保留全部 4 条 PMC）。
    source_clause = " OR ".join(f"SRC:{s}" for s in sorted(ALLOWED_SOURCES))
    scope = f"({source_clause}) AND ({scope})" if scope else f"({source_clause})"

    if window:
        start = min(window).strftime("%Y-%m-%d")
        end = max(window).strftime("%Y-%m-%d")
        date_clause = f"FIRST_PDATE:[{start} TO {end}]"
    else:
        date_clause = ""

    if scope and date_clause:
        return f"{scope} AND {date_clause}"
    return scope or date_clause


def search_params(query: str, page_size: int = PAGE_SIZE, cursor: str = "*") -> Dict[str, object]:
    """search 查询参数。``resultType=core`` 才带摘要与作者全名。"""
    return {
        "query": query,
        "format": "json",
        "resultType": "core",
        "pageSize": page_size,
        "cursorMark": cursor,
        "synonym": "false",
    }


def _first(value) -> str:
    """字段可能是 str 也可能是 list，统一取第一个。"""
    if isinstance(value, (list, tuple)):
        for item in value:
            text = _first(item)
            if text:
                return text
        return ""
    if value is None:
        return ""
    return str(value).strip()


def _parse_date(record: Dict) -> str:
    """日期：优先 ``firstPublicationDate``（与检索用的 FIRST_PDATE 对齐）。"""
    raw = _first(record.get("firstPublicationDate")) or _first(
        record.get("electronicPublicationDate")
    )
    if not raw:
        # dateOfPublication 实测是 " " / "" 这种空壳，只有年份时才退回 yearOfPublication
        journal_info = record.get("journalInfo") or {}
        year = journal_info.get("yearOfPublication")
        if year:
            return str(year)
    if not raw:
        return ""
    raw = raw.strip()
    # 可能带时间戳，如 "2026-10-16T00:00:00Z"
    return raw.split("T")[0]


def _journal_name(record: Dict) -> str:
    """期刊缩写。

    ⚠️ 真实结构的坑：``journalInfo.journal`` 是**对象**，不是字符串。
    ``_first()`` 对 dict 会 ``str()`` 成 JSON 文本，所以**不能**直接
    ``_first(journal_info.get("journal"))`` —— 那样会把整段 JSON 当期刊名写进日报。

    取不到时返回空串，由 :func:`parse_record` 统一兜底成 ``UNKNOWN_JOURNAL`` ——
    **绝不允许 journal 留空**：日报按期刊筛选，空期刊的条目在页面上无处归类。
    """
    journal_info = record.get("journalInfo")
    if not isinstance(journal_info, dict):
        return ""
    journal = journal_info.get("journal")
    if isinstance(journal, dict):
        return (
            _first(journal.get("medlineAbbreviation"))
            or _first(journal.get("isoabbreviation"))
            or _first(journal.get("title"))
        )
    # 少数版本 journal 是纯字符串
    return _first(journal)


def is_research_record(record: Dict) -> bool:
    """判断这条记录值不值得进日报。

    只保留已被 MEDLINE/PMC 收录、有期刊归属的记录（``source`` ∈ MED/PMC/AGR）。
    预印本（``source == "PPR"``）一律排除，原因有二：

      1. 预印本没有 ``journalInfo``，``journal`` 只能是空字符串，而期刊是
         EP 日报里读者筛选内容的第一依据；
      2. 预印本的 ``id`` 形如 ``PPR1344466``、没有 pmid，无法回链到 PubMed，
         而且预印本库的主题词匹配噪声极大 —— 实测 ``ablation`` 会命中
         "CAPO: Compute-Aware Automated Protein-Model Optimization"
         （蛋白质模型优化）和 LLM 方法学论文。

    预印本若要收录，应当是单独的源（config.yaml 里已有 medrxiv/biorxiv 开关），
    而不是让 Europe PMC 这条「补充源」把它夹带进来。
    """
    source = _first(record.get("source")).upper()
    if source not in ALLOWED_SOURCES:
        return False
    # 双保险：即便 source 正常，没有期刊信息也对日报没有价值
    return bool(_journal_name(record))


def _parse_pdf_url(record: Dict) -> str:
    """开放获取 PDF 链接。

    只认 ``documentStyle == pdf`` 且站点可信的条目：Europe PMC 的
    fullTextUrlList 里混有 doi.org / PubMed 等**落地页**（不是 PDF），
    直接塞进 pdf 字段会让前端「PDF」按钮打开网页。
    """
    for item in (record.get("fullTextUrlList") or {}).get("fullTextUrl") or []:
        if not isinstance(item, dict):
            continue
        if (item.get("documentStyle") or "").lower() != "pdf":
            continue
        url = _first(item.get("url"))
        if not url:
            continue
        if _first(item.get("site")).lower() in TRUSTED_PDF_SITES or url.lower().endswith(".pdf"):
            return url
    return ""


def _parse_authors(record: Dict) -> List[str]:
    """作者：``authorList.author`` 的 fullName / ``collectiveName``。"""
    authors: List[str] = []
    for item in (record.get("authorList") or {}).get("author") or []:
        if not isinstance(item, dict):
            continue
        name = _first(item.get("fullName")) or _first(item.get("collectiveName"))
        if not name:
            last = _first(item.get("lastName"))
            fore = _first(item.get("firstName")) or _first(item.get("initials"))
            name = f"{fore} {last}".strip()
        if name:
            authors.append(name)
    return authors


def _parse_abstract(record: Dict) -> str:
    """摘要。Europe PMC 会带 ``<h4>Background</h4>`` 之类的 HTML 标签，去掉。"""
    raw = _first(record.get("abstractText"))
    if not raw:
        return ""
    # 极简去标签：Europe PMC 的结构标签只有 h4/br/p 等，不需要完整 HTML 解析
    text = raw.replace("<h4>", "").replace("</h4>", " ").replace("<br/>", "\n").replace(
        "<br />", "\n"
    )
    for tag in ("<p>", "</p>", "<i>", "</i>", "<b>", "</b>", "<sub>", "</sub>", "<sup>", "</sup>"):
        text = text.replace(tag, "")
    return " ".join(text.split())


def parse_record(record: Dict, cfg=None, window: Optional[Sequence[date]] = None) -> Optional[Paper]:
    """单条 Europe PMC JSON → :class:`Paper`。

    返回 None 的情况（都会被静默跳过）：
      - 预印本或没有期刊归属的记录（见 :func:`is_research_record`）；
      - 没有标题；
      - 拿不到任何稳定标识（pmid / doi / pmcid / 内部 id 全缺）。

    ``id`` 取值规则：**优先 pmid，其次 doi，再退到 pmcid / 内部 id**。
    绝不能让预印本的 ``PPR1344466`` 之类编号冒充 pmid —— 但反过来，
    PMC 记录实测**经常既无 pmid 也无 doi**（只有 pmcid），这类是正经期刊论文，
    直接丢掉会漏内容，所以退到 pmcid 作为标识，同时把 doi 留空。
    """
    if not is_research_record(record):
        return None

    pmid = _first(record.get("pmid"))
    doi = _first(record.get("doi"))
    pmcid = _first(record.get("pmcid")) or _first(record.get("fullTextIdList", {}).get("fullTextId"))
    internal_id = _first(record.get("id"))
    title = _first(record.get("title")).rstrip(".")
    if not title:
        return None

    paper_id = pmid or doi or pmcid or internal_id
    if not paper_id:
        logger.debug("跳过无任何标识的 Europe PMC 记录")
        return None
    if not pmid and not doi:
        logger.debug("记录无 pmid/doi，退用 %s: %s", paper_id, title[:60])

    abstract = _parse_abstract(record)
    journal = _journal_name(record) or UNKNOWN_JOURNAL
    pub_types = [_first(t) for t in (record.get("pubTypeList") or {}).get("pubType") or []]
    pub_types = [p for p in pub_types if p]
    is_oa = _first(record.get("isOpenAccess")).upper() == "Y"

    if pmid:
        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    elif doi:
        # 没有 pmid 时用 DOI 直达出版商页面，比 europepmc.org 内部 id 更稳
        url = f"https://doi.org/{doi}"
    elif pmcid:
        url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/"
    else:
        source = _first(record.get("source")) or "MED"
        url = f"https://europepmc.org/article/{source}/{internal_id}"

    paper = Paper(
        id=paper_id,
        title=title,
        authors=_parse_authors(record),
        summary=abstract,
        url=url,
        abs=url,
        pdf=_parse_pdf_url(record),
        date=_parse_date(record),
        source=SOURCE_NAME,
        journal=journal,
        doi=doi,
        pmid=pmid,
        pub_types=pub_types,
        is_open_access=is_oa,
        topic=DEFAULT_TOPIC,
        no_abstract=not abstract.strip(),
    )
    if cfg is not None:
        topic = cfg.classify(paper.title, paper.summary, paper.journal)
        paper.topic = topic
        paper.categories = [topic]
        paper.category = topic
    del window  # 日期过滤已交给 FIRST_PDATE，不需要客户端再筛
    return paper


def parse_response(payload: Dict, cfg=None) -> List[Paper]:
    """整份 search 响应 → Paper 列表。

    被丢弃的记录类型会记进日志 —— 静默丢数据是最难排查的问题。
    """
    results = ((payload or {}).get("resultList") or {}).get("result") or []
    papers: List[Paper] = []
    skipped_preprint = 0
    skipped_other = 0
    for record in results:
        if not isinstance(record, dict):
            continue
        if _first(record.get("source")).upper() == "PPR":
            skipped_preprint += 1
            continue
        paper = parse_record(record, cfg)
        if paper is None:
            skipped_other += 1
            continue
        papers.append(paper)
    if skipped_preprint or skipped_other:
        logger.info(
            "Europe PMC 过滤：预印本 %d 条、无期刊/无 pmid+doi %d 条",
            skipped_preprint,
            skipped_other,
        )
    return papers


def fetch(
    cfg,
    days: int = 1,
    today: Optional[date] = None,
    client: Optional[HttpClient] = None,
) -> List[Paper]:
    """采集最近 ``days`` 天首次发表的论文（Europe PMC 视角）。"""
    window = date_range(days, today)
    query = build_query(cfg.journals, cfg.topics, window)
    logger.info("Europe PMC 检索式: %s", query)

    client = client or build_client(cfg, SOURCE_NAME, europepmc_rate_limiter())
    max_papers = getattr(cfg, "max_papers", 0) or 0
    papers: List[Paper] = []
    cursor = "*"

    for page in range(MAX_PAGES):
        payload = client.get_json(SEARCH_URL, search_params(query, cursor=cursor))
        batch = parse_response(payload, cfg)
        papers.extend(batch)
        logger.info("Europe PMC 第 %d 页：%d 条", page + 1, len(batch))
        if max_papers and len(papers) >= max_papers:
            papers = papers[:max_papers]
            break
        next_cursor = _first(payload.get("nextCursorMark"))
        if not batch or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return papers
