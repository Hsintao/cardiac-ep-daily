"""medRxiv 数据源测试。

全部离线：api.biorxiv.org 的响应以内联 JSON fixture 提供，
翻页用按 URL 分发的假 client 验证。
"""

from __future__ import annotations

from daily_ep.sources import medrxiv

# 真实响应裁剪：心血管分区 + EP 主题词
CARDIO_RECORD = {
    "title": "Pulsed Field Ablation for Atrial Fibrillation: Real-World Outcomes.",
    "authors": "Smith, John A.; Doe, Jane",
    "abstract": "We evaluated pulsed field ablation in 500 patients with atrial fibrillation.",
    "date": "2026-10-19",
    "version": "1",
    "category": "cardiovascular medicine",
    "server": "medRxiv",
    "doi": "10.1101/2026.10.18.26300001",
    "published": "NA",
}

# 心血管分区但主题无关（心衰，不含任何 EP 主题词）→ 主题词预过滤应丢弃
CARDIO_OFFTOPIC = {
    "title": "Biomarkers in dilated cardiomyopathy prognosis",
    "authors": "Roe, Richard",
    "abstract": "Natriuretic peptides and outcomes.",
    "date": "2026-10-19",
    "version": "1",
    "category": "cardiovascular medicine",
    "doi": "10.1101/2026.10.18.26300002",
}

# 非心血管分区：命中主题词也不能收
EPIDEMIOLOGY_RECORD = {
    "title": "Atrial fibrillation screening in a national cohort",
    "authors": "Lee, Kim",
    "abstract": "Atrial fibrillation screening.",
    "date": "2026-10-19",
    "version": "1",
    "category": "epidemiology",
    "doi": "10.1101/2026.10.18.26300003",
}


def payload(records, total=None):
    return {
        "messages": [{"status": "ok", "total": total if total is not None else len(records)}],
        "collection": records,
    }


# ---------------------------------------------------------------------------
# parse_record
# ---------------------------------------------------------------------------

def test_cardio_record_parsed(cfg):
    paper = medrxiv.parse_record(CARDIO_RECORD, cfg)
    assert paper is not None
    assert paper.source == "medrxiv"
    assert paper.journal == "medRxiv"
    assert paper.pub_types == ["Preprint"]
    assert paper.is_open_access is True
    assert paper.doi == "10.1101/2026.10.18.26300001"
    assert paper.id == paper.doi
    assert paper.date == "2026-10-19"
    # 标题末尾句号去掉（与 PubMed 侧一致）
    assert paper.title.endswith("Outcomes")
    assert paper.no_abstract is False


def test_pdf_url_constructed_from_doi_and_version(cfg):
    paper = medrxiv.parse_record(CARDIO_RECORD, cfg)
    assert paper.pdf == (
        "https://www.medrxiv.org/content/10.1101/2026.10.18.26300001v1.full.pdf"
    )


def test_authors_reordered_to_given_family():
    assert medrxiv._parse_authors("Smith, John A.; Doe, Jane") == ["John A. Smith", "Jane Doe"]
    assert medrxiv._parse_authors("EHRA Group") == ["EHRA Group"]


def test_non_cardiovascular_category_rejected(cfg):
    assert medrxiv.parse_record(EPIDEMIOLOGY_RECORD, cfg) is None


def test_offtopic_cardio_record_rejected_by_topic_prefilter(cfg):
    """分区对了但主题词全不命中：心血管分区里也有大量非 EP 内容。"""
    assert medrxiv.parse_record(CARDIO_OFFTOPIC, cfg) is None


def test_missing_title_or_doi_rejected(cfg):
    no_title = dict(CARDIO_RECORD, title="")
    no_doi = dict(CARDIO_RECORD, doi="")
    assert medrxiv.parse_record(no_title, cfg) is None
    assert medrxiv.parse_record(no_doi, cfg) is None


def test_topic_assigned(cfg):
    paper = medrxiv.parse_record(CARDIO_RECORD, cfg)
    assert paper.topic == "房颤与消融"


# ---------------------------------------------------------------------------
# parse_response / 翻页
# ---------------------------------------------------------------------------

def test_parse_response_skips_other_categories(cfg):
    papers = medrxiv.parse_response(payload([EPIDEMIOLOGY_RECORD, CARDIO_RECORD]), cfg)
    assert [p.doi for p in papers] == [CARDIO_RECORD["doi"]]


def test_parse_response_handles_empty_payload(cfg):
    assert medrxiv.parse_response({}, cfg) == []
    assert medrxiv.parse_response({"messages": [{"status": "ok", "total": 0}]}, cfg) == []


def test_total_tolerates_string():
    assert medrxiv._total(payload([], total="42")) == 42
    assert medrxiv._total({}) == 0


class FakeClient:
    """按 URL 里的 cursor 分发不同页。"""

    def __init__(self, pages):
        self.pages = pages  # {cursor: payload}
        self.urls = []

    def get_json(self, url, params=None):
        self.urls.append(url)
        cursor = int(url.rstrip("/").split("/")[-1])
        return self.pages[cursor]


def test_fetch_paginates_until_total(cfg):
    page0 = payload([CARDIO_RECORD], total=150)
    page1 = payload([dict(CARDIO_RECORD, doi="10.1101/2026.10.18.26300009")], total=150)
    client = FakeClient({0: page0, 100: page1})
    papers = medrxiv.fetch(cfg, days=2, today=__import__("datetime").date(2026, 10, 20), client=client)
    assert len(papers) == 2
    assert any("/2026-10-19/2026-10-20/0" in u for u in client.urls)
    assert any(u.endswith("/100") for u in client.urls)


def test_fetch_stops_when_collection_empty(cfg):
    client = FakeClient({0: payload([], total=0)})
    papers = medrxiv.fetch(cfg, days=1, today=__import__("datetime").date(2026, 10, 20), client=client)
    assert papers == []
    assert len(client.urls) == 1
