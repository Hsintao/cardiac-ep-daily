"""非研究性文献过滤（勘误/撤稿/无内容指针）的测试。

背景：真实数据里混进过一条纯勘误通知 ——
``PMID 42890521 | Front Physiol | "Correction: Machine learning for enhancing the
ablation outcomes..." | summary="[This corrects the article DOI: ...]"``，
它靠标题里的 ablation/AF 拿到 0.54 分进了日报。读者点开只有一个 DOI 指针。

全部离线、纯函数。
"""

from __future__ import annotations

import pytest

from daily_ep.pubtypes import (
    HARD_DROP_TYPES,
    SOFT_TYPES,
    drop_reason,
    filter_non_research,
    is_pointer_only,
)
from daily_ep.schema import Paper


def make(title="Some title", pub_types=None, summary="A real abstract with content.", **kw) -> Paper:
    return Paper(
        id=kw.pop("id", "1"),
        title=title,
        pmid=kw.pop("pmid", "1"),
        pub_types=list(pub_types or ["Journal Article"]),
        summary=summary,
        no_abstract=not bool(summary),
        **kw,
    )


# ---------------------------------------------------------------------------
# 勘误/撤稿：无条件丢弃
# ---------------------------------------------------------------------------

def test_real_erratum_from_production_is_dropped():
    """生产环境漏网的原始样本（PMID 42890521）。"""
    paper = make(
        title="Correction: Machine learning for enhancing the ablation outcomes using "
              "recurrence quantification analysis in atrial fibrillation",
        pub_types=["Published Erratum"],
        summary="[This corrects the article DOI: 10.3389/fphys.2026.1831826.].",
    )
    assert drop_reason(paper) is not None


@pytest.mark.parametrize(
    "pub_type",
    [
        "Published Erratum",
        "Erratum",
        "Retraction of Publication",
        "Retracted Publication",
        "Corrected and Republished Article",
        "Retraction",
        "Expression of Concern",
    ],
)
def test_hard_drop_types_always_dropped(pub_type):
    # HARD_DROP_TYPES 存小写，匹配时统一 lower()（见 _norm_types）
    assert pub_type.lower() in HARD_DROP_TYPES
    paper = make(pub_types=[pub_type], summary="[This corrects the article DOI: 10.1/x].")
    assert drop_reason(paper) is not None


def test_erratum_dropped_even_with_long_summary():
    """勘误类不看摘要长短，一律丢弃。"""
    paper = make(
        pub_types=["Published Erratum"],
        summary="A long explanation of the correction which still has no research content. " * 3,
    )
    assert drop_reason(paper) is not None


# ---------------------------------------------------------------------------
# 社论/评论等：有真实摘要才保留
# ---------------------------------------------------------------------------

def test_comment_with_real_abstract_is_kept():
    """有独立观点的评论文章应保留。"""
    paper = make(
        title="Comment on atrial fibrillation ablation outcomes",
        pub_types=["Comment"],
        summary="This study demonstrates that pulsed field ablation reduces recurrence.",
    )
    assert drop_reason(paper) is None


def test_editorial_with_empty_summary_is_dropped():
    paper = make(title="Editorial: the future of electrophysiology", pub_types=["Editorial"], summary="")
    assert drop_reason(paper) is not None


def test_editorial_with_real_text_is_kept():
    paper = make(
        title="Editorial: training in electrophysiology",
        pub_types=["Editorial"],
        summary="We argue that simulation should complement, not replace, hands-on training.",
    )
    assert drop_reason(paper) is None


def test_letter_with_real_text_is_kept():
    paper = make(
        title="Letter: ablation outcomes",
        pub_types=["Letter"],
        summary="We read with interest the recent report on catheter ablation.",
    )
    assert drop_reason(paper) is None


def test_letter_with_empty_summary_is_dropped():
    paper = make(pub_types=["Letter"], summary="")
    assert drop_reason(paper) is not None


# ---------------------------------------------------------------------------
# 关键：不能误杀「正常论文但没有摘要」
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "title,pub_types",
    [
        # 这三篇都是生产数据里必须保留的电生理论文（PubMed 未标引摘要）
        ("Optogenetic cardioversion: To depolarize or to hyperpolarize?", ["Journal Article"]),
        (
            "Comparison of AI-enabled cardiac chambers volumetry based on non-gated chest CT "
            "scans with echocardiography",
            ["Journal Article"],
        ),
        (
            "A misleading wide-complex rhythm in an implantable cardioverter-defibrillator recipient",
            ["Journal Article"],
        ),
        ("Pathogenic Variants in NKX2-5", ["Journal Article", "Case Reports"]),
        ("Twenty Years at the Crossroads", ["Journal Article", "Review"]),
    ],
)
def test_normal_papers_with_empty_abstract_are_kept(title, pub_types):
    """空摘要**只**对 SOFT_TYPES 生效；Journal Article 空摘要必须保留。"""
    paper = make(title=title, pub_types=pub_types, summary="")
    assert drop_reason(paper) is None


def test_jacc_ep_whitelisted_review_with_empty_abstract_survives():
    """白名单保留的那篇 JACC Clin Electrophysiol 是 Review 且摘要为空 —— 必须留下。"""
    paper = make(
        title="Twenty Years at the Crossroads: Is Clinical Cardiac Electrophysiology Science, "
              "Procedure, or Both?",
        pub_types=["Journal Article", "Review"],
        summary="",
    )
    assert drop_reason(paper) is None


# ---------------------------------------------------------------------------
# 指针式摘要判定
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "summary",
    [
        "",
        "   ",
        "[This corrects the article DOI: 10.3389/fphys.2026.1831826.].",
        "This retracts the article DOI: 10.1/x.",
        "[This retracts the article on p. 123.]",
    ],
)
def test_pointer_only_detected(summary):
    assert is_pointer_only(summary) is True


@pytest.mark.parametrize(
    "summary",
    [
        "We enrolled 320 patients and performed catheter ablation.",
        "Atrial fibrillation recurred in 28% of patients over 12 months of follow-up.",
        "We argue that simulation complements hands-on training in electrophysiology.",
    ],
)
def test_real_abstract_not_pointer(summary):
    assert is_pointer_only(summary) is False


# ---------------------------------------------------------------------------
# 批量过滤与白名单无关
# ---------------------------------------------------------------------------

def test_filter_non_research_batch():
    papers = [
        make(title="Good paper", pub_types=["Journal Article"], summary="Real abstract."),
        make(title="Correction: X", pub_types=["Published Erratum"], summary="[This corrects...]"),
        make(title="Editorial no abs", pub_types=["Editorial"], summary=""),
    ]
    kept, stats = filter_non_research(papers)
    assert len(kept) == 1
    assert kept[0].title == "Good paper"
    assert stats.before == 3 and stats.after == 1
    assert stats.dropped_count == 2


def test_erratum_dropped_even_from_whitelisted_journal():
    """白名单期刊不能豁免非研究性过滤。

    本模块不接收 journals 参数，天然不会给白名单开后门；
    这里用白名单期刊名构造样本，断言仍然被丢。
    """
    paper = make(
        title="Correction: something about atrial fibrillation",
        pub_types=["Published Erratum"],
        summary="[This corrects the article DOI: 10.1/x].",
        journal="Heart Rhythm",
    )
    kept, stats = filter_non_research([paper])
    assert kept == []
    assert stats.dropped_count == 1


def test_empty_input():
    kept, stats = filter_non_research([])
    assert kept == []
    assert stats.before == 0 and stats.after == 0


def test_stats_summary_readable():
    _, stats = filter_non_research(
        [make(pub_types=["Published Erratum"], summary="[This corrects...]")]
    )
    text = stats.summary()
    assert "1" in text


def test_dropped_records_reason_and_types():
    _, stats = filter_non_research(
        [make(title="Correction: X", pub_types=["Published Erratum"], summary="[This corrects...]")]
    )
    title, reason, types = stats.dropped[0]
    assert "Correction" in title
    assert "勘误" in reason or "erratum" in reason.lower()
    assert "Published Erratum" in types


def test_pub_types_case_insensitive():
    paper = make(pub_types=["PUBLISHED ERRATUM"], summary="[This corrects...]")
    assert drop_reason(paper) is not None


def test_soft_types_contains_editorial_like():
    for name in ("comment", "editorial", "letter", "news"):
        assert name in SOFT_TYPES
