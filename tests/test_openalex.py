"""OpenAlex 数据源测试。全部离线，响应为内联 JSON fixture。"""

from __future__ import annotations

from datetime import date

from daily_ep.sources import openalex

# 真实结构裁剪：abstract 是倒排索引，doi 是完整 URL，source 带 issn 列表
WORK_ITEM = {
    "id": "https://openalex.org/W123456",
    "doi": "https://doi.org/10.1093/europace/euaf999",
    "display_name": "Catheter ablation of atrial fibrillation in octogenarians.",
    "authorships": [
        {"author": {"display_name": "John Smith"}},
        {"author": {"display_name": "Jane Doe"}},
    ],
    "publication_date": "2026-10-19",
    "type": "article",
    "open_access": {"is_oa": True},
    "best_oa_location": {"pdf_url": "https://example.org/a.pdf"},
    "primary_location": {
        "source": {
            "display_name": "Europace",
            "issn": ["1099-5129", "1532-2092"],
            "issn_l": "1099-5129",
        }
    },
    "abstract_inverted_index": {
        "Catheter": [0],
        "ablation": [1],
        "is": [2],
        "safe": [3],
        "in": [4],
        "octogenarians.": [5],
    },
}

ISSN_TO_JOURNAL = {"1099-5129": "Europace", "1532-2092": "Europace"}


def payload(items, next_cursor=None):
    return {"results": items, "meta": {"next_cursor": next_cursor}}


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def test_abstract_reconstructed_from_inverted_index():
    abstract = openalex.reconstruct_abstract(WORK_ITEM["abstract_inverted_index"])
    assert abstract == "Catheter ablation is safe in octogenarians."
    assert openalex.reconstruct_abstract(None) == ""
    assert openalex.reconstruct_abstract({}) == ""


def test_doi_url_stripped_to_bare_doi():
    assert openalex._strip_doi("https://doi.org/10.1093/europace/euaf999") == (
        "10.1093/europace/euaf999"
    )
    assert openalex._strip_doi("10.1/x") == "10.1/x"
    assert openalex._strip_doi("") == ""


def test_work_parsed(cfg):
    paper = openalex.parse_work(WORK_ITEM, ISSN_TO_JOURNAL, cfg)
    assert paper is not None
    assert paper.source == "openalex"
    assert paper.doi == "10.1093/europace/euaf999"
    assert paper.id == paper.doi
    assert paper.journal == "Europace"          # ISSN 命中白名单 → 白名单刊名
    assert paper.authors == ["John Smith", "Jane Doe"]
    assert paper.summary.startswith("Catheter ablation")
    assert paper.pub_types == ["Journal Article"]
    assert paper.is_open_access is True
    assert paper.pdf == "https://example.org/a.pdf"
    assert paper.date == "2026-10-19"
    assert paper.url == "https://doi.org/10.1093/europace/euaf999"


def test_journal_falls_back_to_source_display_name():
    item = {
        "primary_location": {"source": {"display_name": "Some Journal", "issn": ["0000-0000"]}}
    }
    assert openalex._journal_name(item, ISSN_TO_JOURNAL) == "Some Journal"
    assert openalex._journal_name({"primary_location": {}}, ISSN_TO_JOURNAL) == ""


def test_journal_matches_on_secondary_issn():
    """source.issn 是列表，电子刊号也要能映射回白名单刊名。"""
    item = {"primary_location": {"source": {"display_name": "EP Europace", "issn": ["1532-2092"]}}}
    assert openalex._journal_name(item, ISSN_TO_JOURNAL) == "Europace"


def test_type_mapped_to_pubmed_style_pub_types(cfg):
    assert openalex.parse_work(dict(WORK_ITEM, type="review"), ISSN_TO_JOURNAL, cfg).pub_types == [
        "Review"
    ]
    assert openalex.parse_work(dict(WORK_ITEM, type="preprint"), ISSN_TO_JOURNAL, cfg).pub_types == [
        "Preprint"
    ]
    assert openalex.parse_work(dict(WORK_ITEM, type="book-chapter"), ISSN_TO_JOURNAL, cfg).pub_types == []


def test_id_falls_back_to_openalex_url(cfg):
    item = dict(WORK_ITEM)
    item["doi"] = None
    paper = openalex.parse_work(item, ISSN_TO_JOURNAL, cfg)
    assert paper.id == "https://openalex.org/W123456"
    assert paper.doi == ""


def test_missing_title_rejected(cfg):
    assert openalex.parse_work(dict(WORK_ITEM, display_name=""), ISSN_TO_JOURNAL, cfg) is None


# ---------------------------------------------------------------------------
# 查询构造与 fetch 编排
# ---------------------------------------------------------------------------

def test_date_filter_includes_paratext_exclusion():
    clause = openalex._date_filter([date(2026, 10, 19), date(2026, 10, 20)])
    assert "from_publication_date:2026-10-19" in clause
    assert "to_publication_date:2026-10-20" in clause
    assert "is_paratext:false" in clause


def test_works_params_mailto_optional():
    assert "mailto" not in openalex.works_params("f")
    assert openalex.works_params("f", mailto="a@b.c")["mailto"] == "a@b.c"
    assert "api_key" not in openalex.works_params("f")
    assert openalex.works_params("f", api_key="k123")["api_key"] == "k123"


def test_multiword_topics_quoted():
    assert openalex._quote("atrial fibrillation") == '"atrial fibrillation"'
    assert openalex._quote("defibrillator") == "defibrillator"


class FakeClient:
    """按 filter 内容分发响应：主题词路与期刊路各给一份。"""

    def __init__(self, by_keyword):
        self.by_keyword = by_keyword  # [(filter中的关键字, payload)]
        self.calls = []

    def get_json(self, url, params=None):
        filter_clause = (params or {}).get("filter", "")
        self.calls.append(filter_clause)
        for keyword, payload_ in self.by_keyword:
            if keyword in filter_clause:
                return payload_
        return payload([])


def test_fetch_queries_topics_and_journals(cfg):
    journal_batch = payload([WORK_ITEM])
    client = FakeClient([("primary_location.source.issn", journal_batch)])
    papers = openalex.fetch(cfg, days=2, today=date(2026, 10, 20), client=client)

    # 每个主题词一路 + 期刊一路
    topic_calls = [c for c in client.calls if "title_and_abstract.search" in c]
    journal_calls = [c for c in client.calls if "primary_location.source.issn" in c]
    assert len(topic_calls) == len([t for t in cfg.topics if t.strip()])
    assert len(journal_calls) == 1
    # 期刊路的 ISSN 来自 journal_issn 映射，用 | 做 OR
    assert "1099-5129" in journal_calls[0]
    assert "|" in journal_calls[0]
    assert papers and papers[0].journal == "Europace"


def test_fetch_dedups_across_topic_and_journal_queries(cfg):
    """同一篇同时命中主题词和期刊路时只保留一条。"""
    both = payload([WORK_ITEM])
    client = FakeClient([("", both)])  # 所有查询都返回同一篇
    papers = openalex.fetch(cfg, days=1, today=date(2026, 10, 20), client=client)
    assert len(papers) == 1


def test_fetch_query_paginates(cfg):
    pages = {
        "*": payload([WORK_ITEM], next_cursor="PAGE2"),
        "PAGE2": payload(
            [dict(WORK_ITEM, doi="https://doi.org/10.1093/europace/euaf1000")]
        ),
    }

    class CursorClient:
        def get_json(self, url, params=None):
            return pages[(params or {}).get("cursor", "*")]

    papers = openalex.fetch_query(CursorClient(), "f", ISSN_TO_JOURNAL, cfg, set())
    assert len(papers) == 2
