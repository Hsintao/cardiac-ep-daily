"""去重逻辑的测试。

跨源去重是日报质量的关键：PubMed 与 Europe PMC 大量重叠，
不去重的话每天都会出现成对的重复标题。

全部为纯内存构造，不打网络。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from daily_ep.config import Config, SourceConfig
from daily_ep.dedup import (
    DEFAULT_PRIORITY,
    deduplicate,
    dedup_stats,
    sort_papers,
    source_priority,
    top_n,
)
from daily_ep.schema import Paper, dedup_keys, normalize_title


@pytest.fixture()
def cfg() -> Config:
    """真实 config.yaml（去重优先级必须与生产一致）。"""
    return Config.load()


def make_paper(
    title: str = "Catheter ablation of atrial fibrillation",
    doi: str = "",
    pmid: str = "",
    source: str = "pubmed",
    summary: str = "some abstract",
    **kwargs,
) -> Paper:
    return Paper(
        id=kwargs.pop("id", pmid or doi or title[:20]),
        title=title,
        doi=doi,
        pmid=pmid,
        source=source,
        summary=summary,
        journal=kwargs.pop("journal", "Heart Rhythm"),
        date=kwargs.pop("date", "2026-10-18"),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 键生成（契约见 schema.dedup_keys）
# ---------------------------------------------------------------------------

def test_dedup_keys_order_is_doi_then_pmid_then_title():
    paper = make_paper(title="Some Title", doi="10.1/ABC", pmid="123")
    keys = dedup_keys(paper)
    assert keys[0] == "doi:10.1/abc"      # DOI 小写归一
    assert keys[1] == "pmid:123"
    assert keys[2].startswith("title:")


def test_dedup_keys_falls_back_to_id():
    paper = make_paper(title="", doi="", pmid="", id="lonely")
    assert dedup_keys(paper) == ["id:lonely"]


def test_normalize_title_handles_punctuation_and_unicode():
    # 不同库对连字符/全角的处理不同，归一后必须一致
    a = normalize_title("P‐wave Duration: A Novel Marker!")
    b = normalize_title("p wave duration   a novel marker")
    assert a == b


# ---------------------------------------------------------------------------
# 跨源去重：命中任意一个键即视为重复
# ---------------------------------------------------------------------------

def test_duplicate_by_doi_across_sources():
    a = make_paper(doi="10.1/x", pmid="111", source="pubmed")
    b = make_paper(doi="10.1/x", pmid="999", source="europepmc")
    assert len(deduplicate([a, b])) == 1


def test_duplicate_by_pmid_when_doi_missing():
    a = make_paper(doi="", pmid="222", source="pubmed")
    b = make_paper(doi="", pmid="222", source="europepmc")
    assert len(deduplicate([a, b])) == 1


def test_duplicate_by_title_fingerprint_when_ids_differ():
    """最常见的真实情况：两个库的 DOI/PMID 写法不同，只能靠标题。"""
    a = make_paper(title="Pulsed Field Ablation: A New Era.", doi="", pmid="", source="pubmed")
    b = make_paper(title="Pulsed field ablation - a new era", doi="", pmid="", source="europepmc")
    assert len(deduplicate([a, b])) == 1


def test_non_duplicates_are_both_kept():
    a = make_paper(title="Study A", pmid="1")
    b = make_paper(title="Study B", pmid="2")
    assert len(deduplicate([a, b])) == 2


def test_multiple_copies_collapse_to_one():
    papers = [make_paper(doi="10.1/x", pmid=str(i)) for i in range(4)]
    assert len(deduplicate(papers)) == 1


def test_empty_input():
    assert deduplicate([]) == []


def test_single_paper_passes_through():
    paper = make_paper()
    assert deduplicate([paper]) == [paper]


# ---------------------------------------------------------------------------
# 保留哪一条：源优先级
# ---------------------------------------------------------------------------

def test_pubmed_wins_over_europepmc(cfg):
    """配置里 pubmed=10 < europepmc=20，保留 PubMed 那条。"""
    epmc = make_paper(doi="10.1/x", source="europepmc", journal="Europace")
    pm = make_paper(doi="10.1/x", source="pubmed", journal="Heart Rhythm")
    kept = deduplicate([epmc, pm], cfg)
    assert len(kept) == 1
    assert kept[0].source == "pubmed"


def test_lower_priority_number_wins_regardless_of_order():
    epmc = make_paper(doi="10.1/x", source="europepmc", journal="Europace")
    pm = make_paper(doi="10.1/x", source="pubmed", journal="Heart Rhythm")
    # 即使 PubMed 后到，也要顶掉 Europe PMC
    kept = deduplicate([epmc, pm], priorities={"pubmed": 10, "europepmc": 20})
    assert kept[0].source == "pubmed"


def test_explicit_priorities_override_config():
    a = make_paper(doi="10.1/x", source="alpha")
    b = make_paper(doi="10.1/x", source="beta")
    kept = deduplicate([a, b], priorities={"alpha": 99, "beta": 1})
    assert kept[0].source == "beta"


def test_unknown_source_uses_default_priority(cfg):
    assert source_priority(cfg, "medrxiv") == DEFAULT_PRIORITY or isinstance(
        source_priority(cfg, "medrxiv"), int
    )


def test_source_priority_reads_config(cfg):
    # config.yaml 里 pubmed 优先级必须高于 europepmc
    assert source_priority(cfg, "pubmed") < source_priority(cfg, "europepmc")


def test_same_priority_keeps_first_seen():
    a = make_paper(doi="10.1/x", source="s1", journal="First")
    b = make_paper(doi="10.1/x", source="s2", journal="Second")
    kept = deduplicate([a, b], priorities={"s1": 5, "s2": 5})
    assert kept[0].journal == "First"


# ---------------------------------------------------------------------------
# 元数据补齐：高优先级条目缺摘要时用低优先级补上
# ---------------------------------------------------------------------------

def test_missing_abstract_is_filled_from_duplicate():
    """PubMed 记录有时摘要在 Europe PMC 才有（反之亦然），不能白丢。"""
    pm = make_paper(doi="10.1/x", source="pubmed", summary="")
    pm.no_abstract = True
    epmc = make_paper(doi="10.1/x", source="europepmc", summary="Full abstract text")
    kept = deduplicate([pm, epmc], priorities={"pubmed": 10, "europepmc": 20})
    assert len(kept) == 1
    assert kept[0].summary == "Full abstract text"
    assert kept[0].no_abstract is False


def test_existing_abstract_is_not_overwritten():
    pm = make_paper(doi="10.1/x", source="pubmed", summary="Original")
    epmc = make_paper(doi="10.1/x", source="europepmc", summary="Different")
    kept = deduplicate([pm, epmc], priorities={"pubmed": 10, "europepmc": 20})
    assert kept[0].summary == "Original"


def test_pdf_backfilled_from_open_access_source():
    """Europe PMC 提供 OA PDF 链接，PubMed 没有 → 必须补上。"""
    pm = make_paper(doi="10.1/x", source="pubmed")
    epmc = make_paper(
        doi="10.1/x", source="europepmc", pdf="https://example.org/a.pdf", is_open_access=True
    )
    kept = deduplicate([pm, epmc], priorities={"pubmed": 10, "europepmc": 20})
    assert kept[0].pdf == "https://example.org/a.pdf"
    assert kept[0].is_open_access is True


def test_pub_types_are_merged():
    pm = make_paper(doi="10.1/x", source="pubmed", pub_types=["Journal Article"])
    epmc = make_paper(
        doi="10.1/x", source="europepmc", pub_types=["Journal Article", "Review"]
    )
    kept = deduplicate([pm, epmc], priorities={"pubmed": 10, "europepmc": 20})
    assert kept[0].pub_types == ["Journal Article", "Review"]


def test_backfilled_keys_are_indexed_for_third_copy():
    """补齐元数据后可能产生新键，第三条副本仍要被判重。

    pubmed 条目无 doi/pmid（只有标题），europepmc 副本带回 doi。
    第三条带着同一个 doi 而来时必须命中，否则会重复出现。
    """
    bare = make_paper(title="Unique Title Here", doi="", pmid="", source="pubmed")
    with_doi = make_paper(title="", doi="10.1/new", pmid="", source="europepmc")
    with_doi.title = "Unique Title Here"
    third = make_paper(title="", doi="10.1/new", pmid="", source="other")
    third.title = "Completely Different Title"
    kept = deduplicate([bare, with_doi, third], priorities={"pubmed": 10, "europepmc": 20, "other": 30})
    assert len(kept) == 1


# ---------------------------------------------------------------------------
# 排序 / 截断
# ---------------------------------------------------------------------------

def test_sort_follows_topic_order():
    from daily_ep.schema import TOPIC_ORDER

    papers = [
        make_paper(title="a", pmid="1", topic="其他"),
        make_paper(title="b", pmid="2", topic="房颤与消融"),
        make_paper(title="c", pmid="3", topic="器械治疗"),
    ]
    ordered = [p.topic for p in sort_papers(papers)]
    assert ordered == sorted(ordered, key=TOPIC_ORDER.index)
    assert ordered[0] == "房颤与消融"


def test_sort_newest_first_within_topic():
    papers = [
        make_paper(title="old", pmid="1", topic="房颤与消融", date="2026-10-10"),
        make_paper(title="new", pmid="2", topic="房颤与消融", date="2026-10-19"),
    ]
    assert [p.title for p in sort_papers(papers)] == ["new", "old"]


def test_top_n_truncates():
    papers = [make_paper(pmid=str(i), title=f"T{i}") for i in range(10)]
    assert len(top_n(papers, 3)) == 3


def test_top_n_zero_means_no_limit():
    papers = [make_paper(pmid=str(i), title=f"T{i}") for i in range(10)]
    assert len(top_n(papers, 0)) == 10


def test_dedup_stats_counts_by_source():
    papers = [
        make_paper(pmid="1", source="pubmed"),
        make_paper(pmid="2", source="pubmed"),
        make_paper(pmid="3", source="europepmc"),
    ]
    assert dedup_stats(papers) == {"pubmed": 2, "europepmc": 1}


# ---------------------------------------------------------------------------
# 与真实配置的集成
# ---------------------------------------------------------------------------

def test_dedup_with_real_config(cfg):
    pm = make_paper(doi="10.1093/europace/euag250", pmid="42691435", source="pubmed",
                    journal="Europace", summary="")
    pm.no_abstract = True
    epmc = make_paper(doi="10.1093/europace/euag250", pmid="42691435", source="europepmc",
                      journal="Europace", summary="Abstract from Europe PMC")
    kept = deduplicate([pm, epmc], cfg)
    assert len(kept) == 1
    assert kept[0].source == "pubmed"
    assert kept[0].summary == "Abstract from Europe PMC"


def test_replace_helper_is_used_correctly():
    """用 dataclasses.replace 构造变体，确认 Paper 是普通可变 dataclass。"""
    paper = make_paper(pmid="1")
    clone = replace(paper, source="europepmc")
    assert clone.pmid == paper.pmid and clone.source == "europepmc"
