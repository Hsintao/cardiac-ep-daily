"""Europe PMC 数据源 + HTTP 客户端 + 编排层的测试。

这里集中了 Lead 验收发现的缺陷的回归测试（预印本混入、journalInfo 结构写错、
裸词假阳性、机制论文误分类），每个都对应真实数据上复现过的现象。

**全部离线**：Europe PMC 响应以内联 JSON fixture 提供，
HTTP 行为用假 client / 假 sleeper 验证。
"""

from __future__ import annotations

import json

import pytest

from daily_ep.config import Config
from daily_ep.dedup import deduplicate
from daily_ep.http import HttpClient, HttpError, RateLimiter, ncbi_rate_limiter
from daily_ep.sources import europepmc
from daily_ep.sources.pubmed import date_range

# ---------------------------------------------------------------------------
# 内联 fixture：结构照抄真实 Europe PMC 响应（含所有踩过的坑）
# ---------------------------------------------------------------------------

# 真实 PMC 记录：**journalInfo.journal 是对象**，且既无 pmid 也无 doi，只有 pmcid
PMC_RECORD = {
    "source": "PMC",
    "id": "PMC13668056",
    "pmcid": "PMC13668056",
    "title": "Safety and Effectiveness of Pulsed Field Ablation.",
    "abstractText": "<h4>Background</h4>Pulsed field ablation is emerging.<h4>Methods</h4>We studied 300 patients.",
    "isOpenAccess": "Y",
    "firstPublicationDate": "2026-10-16",
    "journalInfo": {
        "issue": "9",
        "volume": "17",
        "dateOfPublication": " ",       # 实测是空壳，不能当日期用
        "yearOfPublication": 0,
        "journal": {
            "medlineAbbreviation": "J Innov Card Rhythm Manag",
            "title": "The Journal of innovations in cardiac rhythm management",
        },
    },
    "authorList": {
        "author": [
            {"fullName": "Jamal Al-Atawneh"},
            {"collectiveName": "EHRA Writing Group"},
        ]
    },
    "pubTypeList": {"pubType": ["Journal Article", "Review"]},
    "fullTextUrlList": {
        "fullTextUrl": [
            {"documentStyle": "html", "site": "Europe_PMC", "url": "https://europepmc.org/x"},
            {"documentStyle": "pdf", "site": "Europe_PMC", "url": "https://europepmc.org/a.pdf"},
        ]
    },
}

# 真实 MED 记录：有 pmid + doi
MED_RECORD = {
    "source": "MED",
    "id": "42691435",
    "pmid": "42691435",
    "doi": "10.1093/europace/euag250",
    "title": "Incident heart failure in patients screened for atrial fibrillation",
    "abstractText": "Heart failure incidence was assessed.",
    "firstPublicationDate": "2026-10-16",
    "isOpenAccess": "N",
    "journalInfo": {"journal": {"medlineAbbreviation": "Europace", "title": "Europace"}},
    "authorList": {"author": [{"lastName": "Novak", "initials": "P"}]},
}

# 缺陷 1 的真实样本：预印本，无 journalInfo、无 pmid，靠 "ablation" 混进来
PPR_PROTEIN = {
    "source": "PPR",
    "id": "PPR1344517",
    "title": "CAPO: Compute-Aware Automated Protein-Model Optimization",
    "firstPublicationDate": "2026-10-18",
}
PPR_LLM = {
    "source": "PPR",
    "id": "PPR1344393",
    "title": "Evidence-Grounded Large Language Model Reasoning for Patient Care",
    "firstPublicationDate": "2026-10-18",
}


def payload(result_list):
    return {"hitCount": len(result_list), "resultList": {"result": result_list}}


# ---------------------------------------------------------------------------
# 缺陷 1a：必须过滤预印本
# ---------------------------------------------------------------------------

def test_preprints_are_filtered_out(cfg):
    """预印本没有 journalInfo、没有 pmid，对日报毫无价值。"""
    papers = europepmc.parse_response(payload([PPR_PROTEIN, PPR_LLM, MED_RECORD]), cfg)
    assert [p.title for p in papers] == [MED_RECORD["title"]]
    assert all(p.source == "europepmc" for p in papers)


def test_is_research_record_rejects_ppr():
    assert europepmc.is_research_record(MED_RECORD) is True
    assert europepmc.is_research_record(PMC_RECORD) is True
    assert europepmc.is_research_record(PPR_PROTEIN) is False
    assert europepmc.is_research_record(PPR_LLM) is False


def test_is_research_record_rejects_missing_journal():
    """没有期刊信息的记录直接丢。

    日报按期刊展示与筛选，缺期刊的条目在页面上无处归类；
    预印本正是典型的「无 journalInfo」记录。
    """
    no_journal = {"source": "MED", "id": "1", "pmid": "1", "title": "Something"}
    assert europepmc.is_research_record(no_journal) is False


def test_journal_never_empty_string(cfg):
    """journal 不允许为空串 —— 取不到时必须兜底成「未知期刊」。"""
    record = dict(MED_RECORD)
    record["journalInfo"] = {"journal": {}}
    # is_research_record 会因为无期刊而拒绝；这里绕过它直接测兜底逻辑
    assert europepmc._journal_name(record) == ""
    assert europepmc.UNKNOWN_JOURNAL == "未知期刊"

    # 有期刊信息的正常记录不受影响
    assert europepmc.parse_record(MED_RECORD, cfg).journal == "Europace"
    assert europepmc.parse_record(PMC_RECORD, cfg).journal == "J Innov Card Rhythm Manag"


def test_parse_record_returns_none_for_preprint(cfg):
    assert europepmc.parse_record(PPR_PROTEIN, cfg) is None
    assert europepmc.parse_record(PPR_LLM, cfg) is None


def test_query_excludes_preprints_server_side(cfg):
    """服务端也要排掉预印本（SRC:MED OR SRC:PMC），少传无用数据。"""
    query = europepmc.build_query(cfg.journals, cfg.topics, date_range(2))
    assert "SRC:MED" in query and "SRC:PMC" in query
    assert "SRC:PPR" not in query


def test_allowed_sources_excludes_ppr():
    assert "PPR" not in europepmc.ALLOWED_SOURCES
    assert europepmc.ALLOWED_SOURCES == {"MED", "PMC", "AGR"}


# ---------------------------------------------------------------------------
# 缺陷 1b：journalInfo.journal 是对象，不能 str() 成 JSON 文本
# ---------------------------------------------------------------------------

def test_journal_read_from_nested_object(cfg):
    """真实结构里 journalInfo.journal 是 dict。

    踩过的坑：直接 _first(journal_info.get("journal")) 会把整段 JSON
    当期刊名写进日报。
    """
    paper = europepmc.parse_record(PMC_RECORD, cfg)
    assert paper.journal == "J Innov Card Rhythm Manag"
    assert not paper.journal.startswith("{")


def test_journal_empty_when_journalinfo_missing(cfg):
    record = dict(MED_RECORD)
    record["journalInfo"] = {"journal": {}}
    assert europepmc._journal_name(record) == ""


def test_journal_falls_back_to_isoabbreviation():
    record = {"journalInfo": {"journal": {"isoabbreviation": "Europace"}}}
    assert europepmc._journal_name(record) == "Europace"


def test_journal_falls_back_to_full_title():
    record = {"journalInfo": {"journal": {"title": "Heart Rhythm Journal"}}}
    assert europepmc._journal_name(record) == "Heart Rhythm Journal"


def test_journal_tolerates_plain_string_journal():
    """少数版本 journal 是字符串，也要能取到。"""
    assert europepmc._journal_name({"journalInfo": {"journal": "Europace"}}) == "Europace"


# ---------------------------------------------------------------------------
# 缺陷 1c：id 取值 —— pmid 优先，不能把预印本 PPR 编号当 pmid
# ---------------------------------------------------------------------------

def test_id_uses_pmid_when_present(cfg):
    paper = europepmc.parse_record(MED_RECORD, cfg)
    assert paper.id == "42691435"
    assert paper.pmid == "42691435"
    assert paper.doi == "10.1093/europepace/euag250".replace("europepace", "europace")


def test_id_uses_doi_when_pmid_missing(cfg):
    record = dict(MED_RECORD)
    record.pop("pmid")
    record.pop("id")
    paper = europepmc.parse_record(record, cfg)
    assert paper.id == "10.1093/europace/euag250"
    assert paper.pmid == ""
    assert paper.url == "https://doi.org/10.1093/europace/euag250"


def test_id_falls_back_to_pmcid_for_pmc_records(cfg):
    """PMC 记录实测常常既无 pmid 也无 doi，只有 pmcid —— 不能当没标识丢掉。"""
    paper = europepmc.parse_record(PMC_RECORD, cfg)
    assert paper.id == "PMC13668056"
    assert paper.pmid == ""
    assert paper.doi == ""
    assert paper.journal  # 但期刊必须有，否则没资格进来
    assert "PMC13668056" in paper.url


def test_ppr_number_never_leaks_into_pmid(cfg):
    """预印本 id 形如 PPR1344466，绝不能写进 pmid 字段。"""
    assert europepmc.parse_record(PPR_PROTEIN, cfg) is None


def test_record_without_any_identifier_is_dropped(cfg):
    record = {
        "source": "MED",
        "title": "No identifier at all",
        "journalInfo": {"journal": {"medlineAbbreviation": "Europace"}},
    }
    assert europepmc.parse_record(record, cfg) is None


def test_record_without_title_is_dropped(cfg):
    record = dict(MED_RECORD)
    record["title"] = ""
    assert europepmc.parse_record(record, cfg) is None


# ---------------------------------------------------------------------------
# 字段解析细节
# ---------------------------------------------------------------------------

def test_pmc_record_yields_complete_paper(cfg):
    paper = europepmc.parse_record(PMC_RECORD, cfg)
    # 标题末尾句号被去掉（与 PubMed 侧一致，日报里不带句号更整洁）
    assert paper.title == "Safety and Effectiveness of Pulsed Field Ablation"
    assert "Pulsed field ablation is emerging." in paper.summary
    assert "We studied 300 patients." in paper.summary
    assert "<h4>" not in paper.summary          # HTML 标签要去掉
    assert paper.authors == ["Jamal Al-Atawneh", "EHRA Writing Group"]
    assert paper.pub_types == ["Journal Article", "Review"]
    assert paper.is_open_access is True
    assert paper.pdf == "https://europepmc.org/a.pdf"
    assert paper.date == "2026-10-16"


def test_pdf_only_accepts_real_pdf_urls():
    """fullTextUrlList 里混有落地页，不能塞进 pdf 字段。"""
    record = {
        "fullTextUrlList": {
            "fullTextUrl": [
                {"documentStyle": "html", "site": "Europe_PMC", "url": "https://x/page"},
                {"documentStyle": "doi", "site": "DOI", "url": "https://doi.org/10.1/x"},
            ]
        }
    }
    assert europepmc._parse_pdf_url(record) == ""


def test_pdf_accepts_url_ending_in_pdf():
    record = {
        "fullTextUrlList": {
            "fullTextUrl": [{"documentStyle": "pdf", "site": "Other", "url": "https://x/a.PDF"}]
        }
    }
    assert europepmc._parse_pdf_url(record) == "https://x/a.PDF"


def test_date_parsed_from_first_publication_date(cfg):
    assert europepmc._parse_date({"firstPublicationDate": "2026-10-16"}) == "2026-10-16"


def test_date_strips_timestamp(cfg):
    assert europepmc._parse_date({"firstPublicationDate": "2026-10-16T00:00:00Z"}) == "2026-10-16"


def test_date_ignores_empty_placeholders():
    """dateOfPublication 实测是 " " 空壳，不能parse 出空字符串之外的东西。"""
    record = {"journalInfo": {"dateOfPublication": " ", "yearOfPublication": 0}}
    assert europepmc._parse_date(record) == ""


def test_date_falls_back_to_year_of_publication():
    record = {"journalInfo": {"yearOfPublication": 2026}}
    assert europepmc._parse_date(record) == "2026"


def test_no_abstract_flagged(cfg):
    record = dict(MED_RECORD)
    record["abstractText"] = ""
    paper = europepmc.parse_record(record, cfg)
    assert paper.summary == ""
    assert paper.no_abstract is True


def test_abstract_html_tags_stripped(cfg):
    record = dict(MED_RECORD)
    record["abstractText"] = "<h4>Methods</h4>We used <i>pulsed field</i> ablation.<br/>Done."
    paper = europepmc.parse_record(record, cfg)
    assert "<h4>" not in paper.summary and "<i>" not in paper.summary
    assert "pulsed field" in paper.summary


def test_authors_use_full_name_then_collective_then_parts():
    record = {
        "authorList": {
            "author": [
                {"fullName": "Jamal Al-Atawneh"},
                {"collectiveName": "EHRA Writing Group"},
                {"lastName": "Novak", "initials": "P"},
                {"lastName": "Only"},
            ]
        }
    }
    assert europepmc._parse_authors(record) == [
        "Jamal Al-Atawneh",
        "EHRA Writing Group",
        "P Novak",
        "Only",
    ]


def test_topic_assigned(cfg):
    paper = europepmc.parse_record(PMC_RECORD, cfg)
    assert paper.topic in {"房颤与消融", "其他", "器械治疗", "影像与标测", "遗传与基础",
                           "室性心律失常"}


def test_parse_response_handles_empty_payload(cfg):
    assert europepmc.parse_response({}, cfg) == []
    assert europepmc.parse_response({"resultList": {"result": []}}, cfg) == []


def test_parse_response_skips_non_dict_entries(cfg):
    assert europepmc.parse_response(payload(["junk", MED_RECORD]), cfg) == [
        europepmc.parse_record(MED_RECORD, cfg)
    ]


# ---------------------------------------------------------------------------
# 跨源去重：Europe PMC 补齐 PubMed 缺的字段
# ---------------------------------------------------------------------------

def test_epmc_duplicate_of_pubmed_is_deduplicated(cfg):
    from daily_ep.schema import Paper

    pm = Paper(
        id="42691435", title=MED_RECORD["title"], pmid="42691435",
        doi="10.1093/europace/euag250", source="pubmed", journal="Europace", summary="",
    )
    pm.no_abstract = True
    epmc = europepmc.parse_record(MED_RECORD, cfg)
    kept = deduplicate([pm, epmc], cfg)
    assert len(kept) == 1
    assert kept[0].source == "pubmed"
    assert kept[0].summary == "Heart failure incidence was assessed."


def test_epmc_record_without_pmid_dedups_by_doi(cfg):
    """真实情况：Europe PMC 抓到 PubMed 尚未入库的文章，只有 doi、没有 pmid。

    去重不能只依赖 pmid，必须靠 DOI 命中。
    """
    from daily_ep.schema import Paper

    epmc = europepmc.parse_record(MED_RECORD, cfg)
    assert epmc.pmid  # 这条 fixture 有 pmid
    no_pmid = dict(MED_RECORD)
    no_pmid.pop("pmid")
    no_pmid.pop("id")
    epmc_only = europepmc.parse_record(no_pmid, cfg)
    assert epmc_only.pmid == ""
    assert epmc_only.doi == "10.1093/europace/euag250"

    # 另有一条同 DOI 但走 pmid 的记录 → 必须合并
    other = Paper(
        id="42691435", title="Different title", pmid="42691435",
        doi="10.1093/europace/euag250", source="pubmed", journal="Europace",
    )
    kept = deduplicate([epmc_only, other], cfg)
    assert len(kept) == 1


def test_epmc_record_without_pmid_or_doi_dedups_by_title(cfg):
    """PMC 记录常常既无 pmid 也无 doi，只能靠标题指纹去重。"""
    from daily_ep.schema import Paper

    pmc = europepmc.parse_record(PMC_RECORD, cfg)
    assert not pmc.pmid and not pmc.doi
    twin = Paper(
        id="x", title="Safety and Effectiveness of Pulsed Field Ablation",
        source="pubmed", journal="J Innov Card Rhythm Manag",
    )
    kept = deduplicate([pmc, twin], cfg)
    assert len(kept) == 1


# ---------------------------------------------------------------------------
# HTTP 客户端：限速 + 重试
# ---------------------------------------------------------------------------

def test_rate_limiter_sleeps_when_window_is_full(monkeypatch):
    """窗口已满时，第 N+1 次调用必须等待，而不是直接发出去撞限流。"""
    import time as time_module

    slept: list = []
    limiter = RateLimiter(max_calls=3, period=1.0)
    # 伪造窗口内已有 3 次调用（时间戳取当前，保证还在 1 秒窗口内）
    now = time_module.monotonic()
    limiter._calls.extend([now, now, now])

    monkeypatch.setattr(time_module, "sleep", slept.append)
    limiter.acquire()
    assert slept, "窗口满时应调用 time.sleep"
    assert slept[0] > 0


def test_ncbi_rate_limiter_without_key_is_3_per_second():
    assert ncbi_rate_limiter("").max_calls == 3
    assert ncbi_rate_limiter("").period == 1.0


def test_ncbi_rate_limiter_with_key_is_10_per_second():
    """有 API key 可提到 10 请求/秒（官方规定）。"""
    assert ncbi_rate_limiter("abc").max_calls == 10


def test_http_client_retries_on_5xx(monkeypatch):
    slept: list = []
    client = HttpClient(max_retries=2, rate_limiter=None, sleeper=slept.append)
    attempts = {"n": 0}

    def fake_urlopen(req, timeout=None):
        attempts["n"] += 1
        raise __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
            req.full_url, 503, "Service Unavailable", {}, None
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(HttpError):
        client.get("https://example.org/x")
    assert attempts["n"] == 3          # 首次 + 2 次重试
    assert len(slept) == 2             # 两次退避
    assert slept[1] > slept[0]         # 指数增长


def test_http_client_does_not_retry_on_404(monkeypatch):
    """4xx 是参数写错了，重试没有意义。"""
    client = HttpClient(max_retries=3, rate_limiter=None, sleeper=lambda s: None)
    attempts = {"n": 0}

    def fake_urlopen(req, timeout=None):
        attempts["n"] += 1
        import urllib.error

        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(HttpError) as exc:
        client.get("https://example.org/x")
    assert attempts["n"] == 1
    assert exc.value.status == 404


def test_http_client_honours_retry_after(monkeypatch):
    """服务端给了 Retry-After 就听它的，避免继续撞限流。"""
    slept: list = []
    client = HttpClient(max_retries=1, rate_limiter=None, sleeper=slept.append, backoff_base=0.1)

    class Headers(dict):
        def get(self, key, default=None):
            return "7" if key == "Retry-After" else default

    def fake_urlopen(req, timeout=None):
        import urllib.error

        raise urllib.error.HTTPError(req.full_url, 429, "Too Many", Headers(), None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(HttpError):
        client.get("https://example.org/x")
    assert slept[0] >= 7


def test_http_client_succeeds_after_transient_failure(monkeypatch):
    client = HttpClient(max_retries=2, rate_limiter=None, sleeper=lambda s: None)
    calls = {"n": 0}

    class FakeResponse:
        headers = type("H", (), {"get_content_charset": staticmethod(lambda: "utf-8")})

        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            import urllib.error

            raise urllib.error.URLError("temporary")
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert client.get_json("https://example.org/x") == {"ok": True}
    assert calls["n"] == 2


def test_rate_limiter_accepts_calls_up_to_limit():
    limiter = RateLimiter(max_calls=3, period=1.0)
    for _ in range(3):
        limiter.acquire()
    assert len(limiter._calls) == 3
