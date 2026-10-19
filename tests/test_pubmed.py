"""PubMed XML 解析的测试。

全部使用**内联 XML fixture**（从真实 efetch 响应裁剪而成），不打网络。
fixture 保留了真实的坑：结构化摘要多段落、MedlineDate 自由文本、
PubmedData/History 入库日、ELocationID 的 doi、CollectiveName、无摘要社论。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from daily_ep.config import Config
from daily_ep.schema import Paper
from daily_ep.sources.pubmed import parse_article, parse_articles

# 从真实 efetch 响应裁剪：结构化摘要 + History 入库日 + 期刊缩写
STRUCTURED_XML = """<?xml version="1.0" ?>
<!DOCTYPE PubmedArticleSet PUBLIC "-//NLM//DTD PubMedArticle, 1st January 2025//EN"
 "https://dtd.nlm.nih.gov/ncbi/pubmed/out/pubmed_250101.dtd">
<PubmedArticleSet>
<PubmedArticle><MedlineCitation Status="Publisher" Owner="NLM">
<PMID Version="1">42893394</PMID>
<DateRevised><Year>2026</Year><Month>10</Month><Day>18</Day></DateRevised>
<Article PubModel="Print-Electronic"><Journal>
<ISSN IssnType="Electronic">2352-3883</ISSN>
<JournalIssue CitedMedium="Internet"><Volume>123</Volume>
<PubDate><Year>2027</Year><Month>Jan</Month></PubDate></JournalIssue>
<Title>Healthcare management forum</Title>
<ISOAbbreviation>Healthc Manage Forum</ISOAbbreviation></Journal>
<ArticleTitle>Cardiology Support Utilization Patterns Across Northwestern Ontario.</ArticleTitle>
<ELocationID EIdType="doi" ValidYN="Y">10.1177/08404704261492943</ELocationID>
<Abstract>
<AbstractText Label="BACKGROUND" NlmCategory="UNASSIGNED">We studied atrial fibrillation care.</AbstractText>
<AbstractText Label="METHODS" NlmCategory="UNASSIGNED">Retrospective cohort of 206 patients.</AbstractText>
<AbstractText Label="RESULTS" NlmCategory="UNASSIGNED">Catheter ablation was associated with better outcome.</AbstractText>
</Abstract>
<AuthorList CompleteYN="Y">
<Author ValidYN="Y"><LastName>Al-Atawneh</LastName><ForeName>Jamal</ForeName><Initials>J</Initials></Author>
<Author ValidYN="Y"><LastName>Stevenson</LastName><ForeName>Paige H</ForeName><Initials>PH</Initials></Author>
</AuthorList>
</Article></MedlineCitation>
<PubmedData><History>
<PubMedPubDate PubStatus="received"><Year>2026</Year><Month>1</Month><Day>5</Day></PubMedPubDate>
<PubMedPubDate PubStatus="pubmed"><Year>2026</Year><Month>10</Month><Day>18</Day><Hour>5</Hour><Minute>37</Minute></PubMedPubDate>
<PubMedPubDate PubStatus="entrez"><Year>2026</Year><Month>10</Month><Day>17</Day><Hour>1</Hour><Minute>23</Minute></PubMedPubDate>
</History>
<PublicationStatus>aheadofprint</PublicationStatus>
<ArticleIdList>
<ArticleId IdType="pubmed">42893394</ArticleId>
<ArticleId IdType="doi">10.1177/08404704261492943</ArticleId>
<ArticleId IdType="pii">S1748-6815(26)00561-9</ArticleId>
</ArticleIdList></PubmedData></PubmedArticle>
</PubmedArticleSet>
"""

# 无摘要社论：Abstract 缺失、PublicationType=Editorial → no_abstract 必须为 True
NO_ABSTRACT_XML = """<?xml version="1.0" ?>
<PubmedArticleSet>
<PubmedArticle><MedlineCitation Status="Publisher" Owner="NLM">
<PMID Version="1">42900001</PMID>
<Article PubModel="Electronic"><Journal>
<JournalIssue CitedMedium="Internet"><PubDate><Year>2026</Year><Month>Oct</Month><Day>18</Day></PubDate></JournalIssue>
<Title>Heart rhythm</Title><ISOAbbreviation>Heart Rhythm</ISOAbbreviation></Journal>
<ArticleTitle>Looking ahead in cardiac electrophysiology.</ArticleTitle>
<AuthorList CompleteYN="Y"><Author ValidYN="Y"><CollectiveName>EHRA Writing Group</CollectiveName></Author></AuthorList>
<PublicationTypeList><PublicationType>Editorial</PublicationType></PublicationTypeList>
</Article></MedlineCitation>
<PubmedData><ArticleIdList><ArticleId IdType="pubmed">42900001</ArticleId></ArticleIdList></PubmedData></PubmedArticle>
</PubmedArticleSet>
"""

# MedlineDate 自由文本 + 无 History（回退到出版物日期）+ 无 doi + 多出版类型
LEGACY_XML = """<?xml version="1.0" ?>
<PubmedArticleSet>
<PubmedArticle><MedlineCitation Status="MEDLINE" Owner="NLM">
<PMID Version="1">42880000</PMID>
<Article PubModel="Print"><Journal>
<JournalIssue CitedMedium="Print">
<PubDate><MedlineDate>2026 Oct-Nov</MedlineDate></PubDate></JournalIssue>
<Title>Pacing and clinical electrophysiology</Title>
<ISOAbbreviation>Pacing Clin Electrophysiol</ISOAbbreviation></Journal>
<ArticleTitle>Long-term outcomes of <i>leadless</i> pacing systems.</ArticleTitle>
<Abstract><AbstractText>Leadless pacing avoided lead-related complications in 412 patients.</AbstractText></Abstract>
<AuthorList CompleteYN="Y">
<Author ValidYN="Y"><LastName>Ibrahim</LastName><ForeName>Rania</ForeName></Author>
<Author ValidYN="Y"><LastName>Novak</LastName><Initials>P</Initials></Author>
</AuthorList>
<PublicationTypeList>
<PublicationType>Journal Article</PublicationType>
<PublicationType>Randomized Controlled Trial</PublicationType>
</PublicationTypeList>
</Article></MedlineCitation>
<PubmedData><ArticleIdList><ArticleId IdType="pubmed">42880000</ArticleId></ArticleIdList></PubmedData></PubmedArticle>
</PubmedArticleSet>
"""


@pytest.fixture()
def cfg() -> Config:
    return Config.load()


def _first_article(xml: str) -> ET.Element:
    return next(ET.fromstring(xml).iter("PubmedArticle"))


# ---------------------------------------------------------------------------
# 字段解析
# ---------------------------------------------------------------------------

def test_parses_core_fields(cfg):
    papers = parse_articles(STRUCTURED_XML, cfg)
    assert len(papers) == 1
    paper = papers[0]
    assert isinstance(paper, Paper)
    assert paper.pmid == "42893394"
    assert paper.id == "42893394"
    assert paper.journal == "Healthc Manage Forum"
    assert paper.doi == "10.1177/08404704261492943"
    assert paper.source == "pubmed"
    assert paper.url == "https://pubmed.ncbi.nlm.nih.gov/42893394/"


def test_title_strips_trailing_period(cfg):
    # PubMed 标题末尾带句号，日报里显示很难看
    paper = parse_articles(STRUCTURED_XML, cfg)[0]
    assert paper.title == "Cardiology Support Utilization Patterns Across Northwestern Ontario."


def test_title_keeps_inline_markup_text(cfg):
    """标题里的 <i>/<sub> 是子元素，必须用 itertext() 拼回完整标题。"""
    paper = parse_articles(LEGACY_XML, cfg)[0]
    assert "leadless" in paper.title
    assert paper.title.startswith("Long-term outcomes of leadless pacing")


def test_structured_abstract_paragraphs_are_joined(cfg):
    """多段结构化摘要要拼接，并保留 BACKGROUND/METHODS/RESULTS 标签。"""
    paper = parse_articles(STRUCTURED_XML, cfg)[0]
    assert "BACKGROUND:" in paper.summary
    assert "METHODS:" in paper.summary
    assert "RESULTS:" in paper.summary
    assert "Retrospective cohort of 206 patients." in paper.summary
    assert paper.no_abstract is False


def test_authors_formatted_given_family(cfg):
    paper = parse_articles(STRUCTURED_XML, cfg)[0]
    assert paper.authors == ["Jamal Al-Atawneh", "Paige H Stevenson"]


def test_collective_name_preserved(cfg):
    """集体署名（写作组）要原样保留，不能丢掉作者信息。"""
    paper = parse_articles(NO_ABSTRACT_XML, cfg)[0]
    assert paper.authors == ["EHRA Writing Group"]


def test_initials_fallback_when_forename_missing(cfg):
    paper = parse_articles(LEGACY_XML, cfg)[0]
    assert "Rania Ibrahim" in paper.authors
    assert "P Novak" in paper.authors


def test_doi_from_articleidlist(cfg):
    """doi 优先取 PubmedData/ArticleIdList，而不是 ELocationID。"""
    paper = parse_articles(STRUCTURED_XML, cfg)[0]
    assert paper.doi == "10.1177/08404704261492943"


def test_pub_types_parsed(cfg):
    paper = parse_articles(LEGACY_XML, cfg)[0]
    assert paper.pub_types == ["Journal Article", "Randomized Controlled Trial"]


def test_pub_types_empty_when_absent(cfg):
    # NO_ABSTRACT_XML 里 PublicationTypeList 在 Article 内，属正常路径
    paper = parse_articles(NO_ABSTRACT_XML, cfg)[0]
    assert paper.pub_types == ["Editorial"]


# ---------------------------------------------------------------------------
# 日期：必须用入库日（edat 语义），不是印刷日期
# ---------------------------------------------------------------------------

def test_date_uses_entrez_history_not_print_date(cfg):
    """入库日必须优先于印刷日期。

    fixture 里 PubDate 是 2027-Jan（印刷期号，未来月份），
    而 entrez 入库日是 2026-10-17 —— 检索用的 [edat] 就是后者，
    显示日期必须与筛选依据一致，否则用户会以为检索错了。
    """
    paper = parse_articles(STRUCTURED_XML, cfg)[0]
    assert paper.date == "2026-10-17"
    assert paper.date != "2027-01"


def test_date_falls_back_to_pubdate_without_history(cfg):
    paper = parse_articles(LEGACY_XML, cfg)[0]
    # MedlineDate 自由文本 "2026 Oct-Nov" → 年份-月份
    assert paper.date == "2026-10"


def test_date_falls_back_to_pubdate_day(cfg):
    paper = parse_articles(NO_ABSTRACT_XML, cfg)[0]
    assert paper.date == "2026-10-18"


# ---------------------------------------------------------------------------
# 无摘要：社论类必须标记 no_abstract，且 summary 允许为空
# ---------------------------------------------------------------------------

def test_editorial_without_abstract_is_flagged(cfg):
    paper = parse_articles(NO_ABSTRACT_XML, cfg)[0]
    assert paper.summary == ""
    assert paper.no_abstract is True


def test_paper_with_abstract_is_not_flagged(cfg):
    for xml in (STRUCTURED_XML, LEGACY_XML):
        assert parse_articles(xml, cfg)[0].no_abstract is False


# ---------------------------------------------------------------------------
# 亚专业标签：cfg 传入时必须调用 config.classify
# ---------------------------------------------------------------------------

def test_topic_assigned_via_classify(cfg):
    paper = parse_articles(STRUCTURED_XML, cfg)[0]
    assert paper.topic != ""
    assert paper.categories == [paper.topic]
    assert paper.category == paper.topic


def test_topic_is_always_in_topic_order(cfg):
    from daily_ep.schema import TOPIC_ORDER

    for xml in (STRUCTURED_XML, NO_ABSTRACT_XML, LEGACY_XML):
        paper = parse_articles(xml, cfg)[0]
        assert paper.topic in TOPIC_ORDER


def test_classify_not_called_without_cfg():
    """不传 cfg 时不应崩溃，主题保持默认。"""
    paper = parse_articles(STRUCTURED_XML)[0]
    assert paper.topic == "其他"


# ---------------------------------------------------------------------------
# 边界与健壮性
# ---------------------------------------------------------------------------

def test_empty_xml_returns_empty_list(cfg):
    assert parse_articles("", cfg) == []
    assert parse_articles("   ", cfg) == []


def test_malformed_xml_raises(cfg):
    with pytest.raises(ET.ParseError):
        parse_articles("<PubmedArticleSet><unclosed>", cfg)


def test_article_without_title_is_skipped(cfg):
    xml = """<PubmedArticleSet><PubmedArticle><MedlineCitation>
    <PMID>1</PMID><Article><Journal><ISOAbbreviation>X</ISOAbbreviation></Journal></Article>
    </MedlineCitation></PubmedArticle></PubmedArticleSet>"""
    assert parse_articles(xml, cfg) == []


def test_article_without_pmid_is_skipped(cfg):
    xml = """<PubmedArticleSet><PubmedArticle><MedlineCitation>
    <Article><ArticleTitle>No PMID here</ArticleTitle></Article>
    </MedlineCitation></PubmedArticle></PubmedArticleSet>"""
    assert parse_articles(xml, cfg) == []


def test_multiple_articles_all_parsed(cfg):
    """合并两份 fixture（剥掉声明/DOCTYPE 与根元素）验证多记录解析。"""
    import re

    body = ""
    for xml in (STRUCTURED_XML, NO_ABSTRACT_XML):
        stripped = re.sub(r"<\?xml[^>]*\?>", "", xml)
        stripped = re.sub(r"<!DOCTYPE[^>]*>", "", stripped)
        stripped = stripped.replace("<PubmedArticleSet>", "").replace("</PubmedArticleSet>", "")
        body += stripped
    papers = parse_articles("<PubmedArticleSet>" + body + "</PubmedArticleSet>", cfg)
    assert {p.pmid for p in papers} == {"42893394", "42900001"}


def test_parse_article_returns_none_for_empty_article(cfg):
    node = ET.fromstring("<PubmedArticle><MedlineCitation/></PubmedArticle>")
    assert parse_article(node, cfg) is None


def test_no_lxml_dependency():
    """契约要求用标准库 xml.etree（本机 conda 环境没有 lxml）。

    只看 import 语句，不看注释/文档字符串 —— 文档里可能提到 lxml 这个词。
    """
    import re

    import daily_ep.sources.pubmed as module

    source = open(module.__file__, encoding="utf-8").read()
    imports = [
        line.strip()
        for line in source.splitlines()
        if re.match(r"\s*(import|from)\s", line)
    ]
    assert not any("lxml" in line for line in imports)
    assert any("xml.etree.ElementTree" in line for line in imports)
