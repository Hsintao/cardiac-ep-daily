"""Crossref 数据源测试。全部离线，响应为内联 JSON fixture。"""

from __future__ import annotations

from datetime import date

from daily_ep.sources import crossref

# 真实结构裁剪：title/author/container-title 都是 list，abstract 是 JATS XML
WORK_ITEM = {
    "DOI": "10.1016/j.hrthm.2026.10.001",
    "title": ["Pulsed Field Ablation Versus Cryoballoon Ablation."],
    "author": [
        {"given": "John A.", "family": "Smith", "sequence": "first"},
        {"name": "EHRA Writing Group"},
        {"family": "OnlyFamily"},
    ],
    "container-title": ["Heart Rhythm"],
    "abstract": (
        "<jats:sec><jats:title>Background</jats:title>"
        "<jats:p>Pulsed field ablation is noninferior to cryoballoon ablation.</jats:p></jats:sec>"
    ),
    "type": "journal-article",
    "URL": "https://doi.org/10.1016/j.hrthm.2026.10.001",
    "published-online": {"date-parts": [[2026, 10, 18]]},
    "published-print": {"date-parts": [[2027, 1]]},
    "issued": {"date-parts": [[2026, 10, 18]]},
}


def message(items, next_cursor=None):
    msg = {"items": items, "total-results": len(items)}
    if next_cursor:
        msg["next-cursor"] = next_cursor
    return {"message": msg}


# ---------------------------------------------------------------------------
# parse_work
# ---------------------------------------------------------------------------

def test_work_parsed_with_whitelist_journal_name(cfg):
    """journal 必须是白名单刊名（config key），不是 container-title。"""
    paper = crossref.parse_work(WORK_ITEM, "Heart Rhythm", cfg)
    assert paper is not None
    assert paper.source == "crossref"
    assert paper.journal == "Heart Rhythm"
    assert paper.id == "10.1016/j.hrthm.2026.10.001"
    assert paper.doi == "10.1016/j.hrthm.2026.10.001"
    assert paper.pub_types == []          # Crossref type 粒度太粗，不映射
    assert paper.pdf == ""                # 日报不依赖 PDF


def test_online_date_preferred_over_print():
    """online 2026-10-18 vs print 2027-01：日报要的是「哪天能读到」。"""
    assert crossref._parse_date(WORK_ITEM) == "2026-10-18"


def test_date_falls_back_to_print_year_month():
    item = {"published-print": {"date-parts": [[2027, 1]]}}
    assert crossref._parse_date(item) == "2027-01"
    item = {"issued": {"date-parts": [[2027]]}}
    assert crossref._parse_date(item) == "2027"
    assert crossref._parse_date({}) == ""


def test_jats_abstract_stripped(cfg):
    paper = crossref.parse_work(WORK_ITEM, "Heart Rhythm", cfg)
    assert "<jats:" not in paper.summary
    assert "noninferior" in paper.summary
    assert paper.no_abstract is False


def test_missing_abstract_flagged(cfg):
    item = dict(WORK_ITEM)
    item.pop("abstract")
    paper = crossref.parse_work(item, "Heart Rhythm", cfg)
    assert paper.summary == "" and paper.no_abstract is True


def test_missing_doi_or_title_rejected(cfg):
    assert crossref.parse_work(dict(WORK_ITEM, DOI=""), "Heart Rhythm", cfg) is None
    assert crossref.parse_work(dict(WORK_ITEM, title=[]), "Heart Rhythm", cfg) is None


def test_authors_given_family_and_collective(cfg):
    paper = crossref.parse_work(WORK_ITEM, "Heart Rhythm", cfg)
    assert paper.authors == ["John A. Smith", "EHRA Writing Group", "OnlyFamily"]


def test_topic_assigned_from_journal_and_title(cfg):
    paper = crossref.parse_work(WORK_ITEM, "Heart Rhythm", cfg)
    assert paper.topic == "房颤与消融"


# ---------------------------------------------------------------------------
# 查询参数与 fetch 编排
# ---------------------------------------------------------------------------

def test_works_params_filter_and_select():
    params = crossref.works_params(date(2026, 10, 19), date(2026, 10, 20))
    assert "from-pub-date:2026-10-19" in params["filter"]
    assert "until-pub-date:2026-10-20" in params["filter"]
    assert "type:journal-article" in params["filter"]
    assert "abstract" in params["select"]
    # mailto 空时不出现在参数里（进 polite pool 才带）
    assert "mailto" not in params
    assert crossref.works_params(date(2026, 10, 19), date(2026, 10, 20), mailto="a@b.c")[
        "mailto"
    ] == "a@b.c"


class FakeClient:
    """按 URL 里的 ISSN 分发响应；没配 fixture 的期刊返回空页。"""

    def __init__(self, pages_by_issn):
        self.pages_by_issn = pages_by_issn  # {issn: [payload, ...]}
        self.urls = []

    def get_json(self, url, params=None):
        self.urls.append(url)
        issn = url.rstrip("/").split("/")[-2]
        pages = self.pages_by_issn.get(issn)
        if not pages:
            return message([])
        return pages.pop(0) if len(pages) > 1 else pages[0]


def test_fetch_iterates_whitelist_and_skips_missing_issn(cfg, caplog):
    """没有 ISSN 条目的期刊要跳过并告警，不能让刊名检索混进来。"""
    import dataclasses
    import logging

    cfg = dataclasses.replace(
        cfg,
        journals=["Heart Rhythm", "No ISSN Journal"],
        journal_issn={"Heart Rhythm": "1547-5271"},
    )
    client = FakeClient({"1547-5271": [message([WORK_ITEM])]})
    with caplog.at_level(logging.WARNING):
        papers = crossref.fetch(cfg, days=2, today=date(2026, 10, 20), client=client)
    assert len(papers) == 1
    assert papers[0].journal == "Heart Rhythm"
    assert all("/journals/1547-5271/" in u for u in client.urls)
    assert any("No ISSN Journal" in r.message for r in caplog.records)


def test_fetch_dedups_within_source(cfg):
    issn = cfg.journal_issn[cfg.journals[0]]
    client = FakeClient({issn: [message([WORK_ITEM, dict(WORK_ITEM)])]})
    papers = crossref.fetch(cfg, days=1, today=date(2026, 10, 20), client=client)
    assert len(papers) == 1


def test_fetch_journal_paginates_with_cursor(cfg):
    issn = cfg.journal_issn[cfg.journals[0]]
    pages = [
        message([WORK_ITEM], next_cursor="PAGE2"),
        message([dict(WORK_ITEM, DOI="10.1016/j.hrthm.2026.10.002")], next_cursor="PAGE2"),
    ]
    client = FakeClient({issn: pages})
    papers = crossref.fetch(cfg, days=1, today=date(2026, 10, 20), client=client)
    assert {p.doi for p in papers} == {
        "10.1016/j.hrthm.2026.10.001",
        "10.1016/j.hrthm.2026.10.002",
    }


def test_issn_map_covers_every_whitelist_journal(cfg):
    """漏配 ISSN 的期刊会在 Crossref/OpenAlex 里静默消失，必须有测试兜住。"""
    missing = [j for j in cfg.journals if j not in cfg.journal_issn]
    assert missing == []
