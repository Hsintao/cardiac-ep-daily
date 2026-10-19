"""编排层（collect）与配置质量的测试。

配置质量部分守护 Lead 验收发现的缺陷 2/3：
  - 裸词 ablation / pacing 会引入无关论文；
  - topic_rules 的顺序会让机制论文被判成临床亚专业。
这些都是**纯配置断言**，改错配置就变红。

网络调用用假 client 替身，不打真实接口。
"""

from __future__ import annotations

import json

import pytest

from daily_ep.collect import (
    build_parser,
    collect_from_sources,
    default_output_path,
    read_jsonl,
    write_jsonl,
)
from daily_ep.schema import Paper


def make_paper(pmid: str, title: str = "T", source: str = "pubmed", **kw) -> Paper:
    return Paper(id=pmid, title=title, pmid=pmid, source=source, journal="Heart Rhythm", **kw)


# ---------------------------------------------------------------------------
# 源失败隔离
# ---------------------------------------------------------------------------

def test_one_source_failure_does_not_break_others(cfg, monkeypatch):
    """单源失败必须隔离：PubMed 挂掉时 Europe PMC 仍要出结果。"""
    from daily_ep import collect as collect_module
    from daily_ep.sources import SOURCES

    def ok_fetch(cfg, days=1, today=None, client=None):
        return [make_paper("1", "Good paper", source="europepmc")]

    def bad_fetch(cfg, days=1, today=None, client=None):
        raise RuntimeError("boom")

    monkeypatch.setitem(SOURCES, "pubmed", bad_fetch)
    monkeypatch.setitem(SOURCES, "europepmc", ok_fetch)

    papers, status = collect_from_sources(cfg, days=1, sources=["pubmed", "europepmc"])
    assert len(papers) == 1
    assert papers[0].title == "Good paper"
    status_by_name = {name: (count, error) for name, count, error in status}
    assert status_by_name["pubmed"][1] is not None       # 记下了错误
    assert status_by_name["europepmc"][0] == 1           # 另一个源正常


def test_all_sources_failing_is_reported(cfg, monkeypatch):
    from daily_ep.sources import SOURCES

    def bad_fetch(cfg, days=1, today=None, client=None):
        raise RuntimeError("network down")

    monkeypatch.setitem(SOURCES, "pubmed", bad_fetch)
    papers, status = collect_from_sources(cfg, days=1, sources=["pubmed"])
    assert papers == []
    assert all(error for _, _, error in status)


def test_collect_deduplicates_across_sources(cfg, monkeypatch):
    from daily_ep.sources import SOURCES

    def pm(cfg, days=1, today=None, client=None):
        p = make_paper("42", "Shared Title", source="pubmed")
        p.doi = "10.1/shared"
        return [p]

    def epmc(cfg, days=1, today=None, client=None):
        p = make_paper("42", "Shared Title", source="europepmc")
        p.doi = "10.1/shared"
        p.summary = "filled in by europepmc"
        return [p]

    monkeypatch.setitem(SOURCES, "pubmed", pm)
    monkeypatch.setitem(SOURCES, "europepmc", epmc)
    papers, _ = collect_from_sources(cfg, days=1, sources=["pubmed", "europepmc"])
    assert len(papers) == 1
    assert papers[0].source == "pubmed"


def test_collect_respects_max_papers(cfg, monkeypatch):
    from daily_ep.sources import SOURCES

    def many(cfg, days=1, today=None, client=None):
        return [make_paper(str(i), f"Paper {i}") for i in range(50)]

    monkeypatch.setitem(SOURCES, "pubmed", many)
    cfg.max_papers = 10
    papers, _ = collect_from_sources(cfg, days=1, sources=["pubmed"])
    assert len(papers) == 10


def test_unknown_source_name_is_ignored(cfg):
    papers, status = collect_from_sources(cfg, days=1, sources=["nope"])
    assert papers == [] and status == []


def test_client_factory_is_used(cfg, monkeypatch):
    from daily_ep.sources import SOURCES

    seen = {}

    def fake(cfg, days=1, today=None, client=None):
        seen["client"] = client
        return []

    monkeypatch.setitem(SOURCES, "pubmed", fake)
    sentinel = object()
    collect_from_sources(cfg, days=1, sources=["pubmed"], client_factory=lambda name: sentinel)
    assert seen["client"] is sentinel


# ---------------------------------------------------------------------------
# JSONL 读写
# ---------------------------------------------------------------------------

def test_write_and_read_jsonl_roundtrip(tmp_path):
    papers = [
        Paper(id="1", title="房颤消融研究", pmid="1", topic="房颤与消融", journal="Europace"),
        Paper(id="2", title="Device therapy", pmid="2", topic="器械治疗"),
    ]
    for paper in papers:
        paper.categories = [paper.topic]
        paper.category = paper.topic

    path = write_jsonl(papers, tmp_path / "out.jsonl")
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2

    loaded = read_jsonl(path)
    assert [p.title for p in loaded] == ["房颤消融研究", "Device therapy"]
    assert loaded[0].topic == "房颤与消融"


def test_jsonl_is_utf8_not_ascii_escaped(tmp_path):
    """中文标签必须直接可读，不能写成 \\u623f\\u98a4。"""
    paper = Paper(id="1", title="房颤消融", pmid="1", topic="房颤与消融")
    path = write_jsonl([paper], tmp_path / "o.jsonl")
    raw = path.read_text(encoding="utf-8")
    assert "房颤与消融" in raw


def test_jsonl_lines_are_valid_json_with_schema_fields(tmp_path):
    paper = Paper(id="7", title="X", pmid="7", journal="Europace", doi="10.1/x")
    paper.topic = "器械治疗"
    path = write_jsonl([paper], tmp_path / "o.jsonl")
    item = json.loads(path.read_text(encoding="utf-8").strip())
    for field in ("id", "title", "authors", "summary", "url", "abs", "pdf", "date",
                  "categories", "category", "source", "journal", "doi", "pmid",
                  "pub_types", "is_open_access", "topic", "no_abstract", "AI"):
        assert field in item, f"缺少契约字段 {field}"
    assert item["category"] == item["topic"] == item["categories"][0]


def test_write_jsonl_creates_parent_dir(tmp_path):
    path = write_jsonl([make_paper("1")], tmp_path / "nested" / "deep" / "o.jsonl")
    assert path.exists()


def test_read_jsonl_skips_blank_lines(tmp_path):
    path = tmp_path / "o.jsonl"
    path.write_text('{"id":"1","title":"A"}\n\n  \n', encoding="utf-8")
    assert len(read_jsonl(path)) == 1


def test_default_output_path_uses_data_dir():
    from datetime import date

    assert default_output_path(date(2026, 10, 19)) == __import__("pathlib").Path(
        "data/2026-10-19.jsonl"
    )


# ---------------------------------------------------------------------------
# CLI 参数
# ---------------------------------------------------------------------------

def test_parser_accepts_documented_flags():
    args = build_parser().parse_args(["--days", "2", "--out", "data/x.jsonl"])
    assert args.days == 2
    assert args.out == "data/x.jsonl"


def test_parser_defaults():
    args = build_parser().parse_args([])
    assert args.days is None          # None = 用 config.lookback_days+1
    assert args.out is None
    assert args.sources is None


def test_parser_accepts_date_and_sources():
    args = build_parser().parse_args(
        ["--date", "2026-10-19", "--sources", "pubmed,europepmc"]
    )
    assert args.date == "2026-10-19"
    assert args.sources == "pubmed,europepmc"


# ---------------------------------------------------------------------------
# 缺陷 2 回归：主题词里不能有裸词 ablation / pacing
# ---------------------------------------------------------------------------

def test_topics_do_not_contain_bare_ablation(cfg):
    """实测 "ablation"[tiab] 近 2 天 36 篇，多数是肿瘤/组织消融与 ML 消融实验。"""
    assert "ablation" not in cfg.topics


def test_topics_do_not_contain_bare_pacing(cfg):
    """实测 "pacing"[tiab] 会命中 Lupus 的质性研究（英文 pacing = 踱步）。"""
    assert "pacing" not in cfg.topics


def test_topics_contain_precise_replacements(cfg):
    for term in ("catheter ablation", "cardiac pacing"):
        assert term in cfg.topics


def test_topic_rules_do_not_use_bare_pacing(cfg):
    """topic_rules 里的裸 "pacing" 是子串匹配，会误伤无关摘要。"""
    assert "pacing" not in cfg.topic_rules.get("器械治疗", [])


def test_all_topics_are_non_empty_strings(cfg):
    assert cfg.topics
    assert all(isinstance(t, str) and t.strip() for t in cfg.topics)


# ---------------------------------------------------------------------------
# 缺陷 3 回归：机制论文必须归入「遗传与基础」
# ---------------------------------------------------------------------------

def test_optogenetic_paper_is_classified_as_basic_science(cfg):
    """Lead 验收发现：J Physiol 的 "Optogenetic cardioversion" 被判成「其他」。"""
    topic = cfg.classify(
        "Optogenetic cardioversion of atrial fibrillation", "Light-gated ion channels.", "J Physiol"
    )
    assert topic == "遗传与基础"


def test_mechanism_keywords_beat_clinical_keywords(cfg):
    """标题里同时有临床词和机制词时，机制优先（论文重心在机理）。"""
    cases = [
        ("Optogenetic control of ventricular tachycardia", "遗传与基础"),
        ("Ion channel mutations in Brugada syndrome", "遗传与基础"),
        ("Induced pluripotent stem cell model of long QT syndrome", "遗传与基础"),
        ("Cardiomyocyte calcium handling in heart failure", "遗传与基础"),
    ]
    for title, expected in cases:
        assert cfg.classify(title, "", "J Physiol") == expected, title


def test_clinical_papers_still_classified_clinically(cfg):
    """修顺序不能把临床论文误伤成基础研究。"""
    cases = [
        ("Catheter ablation of atrial fibrillation", "房颤与消融"),
        ("Pulsed field ablation versus cryoablation for AF", "房颤与消融"),
        ("Leadless pacemaker implantation outcomes", "器械治疗"),
        ("Cardiac magnetic resonance substrate mapping", "影像与标测"),
        ("Ventricular tachycardia storm management", "室性心律失常"),
    ]
    for title, expected in cases:
        assert cfg.classify(title, "", "Heart Rhythm") == expected, title


def test_irrelevant_paper_goes_to_other(cfg):
    assert cfg.classify("A qualitative study on exercise", "", "Lupus") == "其他"


def test_topic_rules_declaration_order(cfg):
    """遗传与基础必须排在临床亚专业之前；影像与标测在器械治疗之后。"""
    order = list(cfg.topic_rules)
    assert order.index("遗传与基础") < order.index("房颤与消融")
    assert order.index("遗传与基础") < order.index("室性心律失常")


def test_topic_order_matches_schema(cfg):
    """配置里的标签必须是 schema.TOPIC_ORDER 的子集，否则前端分组会漏。"""
    from daily_ep.schema import TOPIC_ORDER

    assert set(cfg.topic_rules) <= set(TOPIC_ORDER)
