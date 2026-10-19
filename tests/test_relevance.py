"""相关性评分的测试。

回归用例直接取自 2026-10-19 的**真实采集数据**（34 篇），由 lead 逐条人工确认：
  - MUST_KEEP：11 篇必须保留（真电生理论文）
  - MUST_DROP：6 篇必须滤掉（典型噪声）
这些用例是标定阈值的依据，任何人改信号/权重导致它们翻转，都会在这里变红。

纯函数、无网络。
"""

from __future__ import annotations

import pytest

from daily_ep.relevance import (
    DEFAULT_THRESHOLD,
    RelevanceResult,
    filter_papers,
    is_relevant,
    score_paper,
    score_text,
)
from daily_ep.relevance_config import RelevanceConfig
from daily_ep.schema import Paper

# ---------------------------------------------------------------------------
# 真实样本（标题 / 摘要片段 / 期刊 / 期望）
# ---------------------------------------------------------------------------

#: 必须保留 —— lead 已逐条确认是电生理论文
MUST_KEEP = [
    (
        "Guidelines for the management of atrial fibrillation in adult patients admitted to an intensive care unit.",
        "Most existing recommendations for atrial fibrillation are derived from cardiology settings. However, "
        "atrial fibrillation is also highly prevalent among patients admitted to intensive care units.",
        "Ann Intensive Care",
    ),
    (
        "Stellate ganglion block for ventricular arrhythmia disease states: Emerging treatment options.",
        "Autonomic modulation has emerged as an effective therapeutic target in the management of ventricular "
        "arrhythmias and electrical storm (e.g., refractory ventricular arrhythmias), in a background of growing "
        "evidence of the link between the autonomic nervous system and arrhythmias. Stellate ganglion blockade "
        "(SGB) has shown potential efficacy in short-term reduction of arrhythmic burden and termination of "
        "electrical storm, leading to ablation-free treatment of ventricular tachycardia.",
        "J Anesth Transl Med",
    ),
    (
        "Optogenetic cardioversion: To depolarize or to hyperpolarize?",
        "",
        "J Physiol",
    ),
    (
        "Channelrhodopsin ion selectivity determines mechanisms and efficacy of optogenetic defibrillation in human atria and ventricles.",
        "Cardiac re-entrant arrhythmias are a major cause of sudden cardiac death. Optogenetic defibrillation has "
        "emerged as a promising anti-arrhythmic strategy.",
        "J Physiol",
    ),
    (
        "Unmasking Brugada Syndrome With Oral Propafenone in a Resource-Limited Setting: A Case Report.",
        "Brugada syndrome (BrS) is a cardiac channelopathy predisposing patients to malignant ventricular "
        "arrhythmias and sudden cardiac death (SCD) in the absence of structural heart disease.",
        "Cureus",
    ),
    (
        "Heart failure and atrial fibrillation prediction from non-gated chest CT scans using AI-based cardiac chamber volumetry.",
        "BACKGROUND: We previously demonstrated that AI-derived cardiac chamber volumetry in ECG-gated CT scans "
        "predicts atrial fibrillation (AF) and heart failure (HF).",
        "Am J Prev Cardiol",
    ),
    (
        "External validation of artificial intelligence-guided electrocardiogram (AI-ECG) in detecting asymptomatic left ventricular dysfunction: A systematic review and meta-analysis.",
        "AIM: Asymptomatic left ventricular dysfunction (LVD) represents a key and frequently under-recognized stage "
        "in the heart failure (HF) continuum.",
        "Am J Prev Cardiol",
    ),
    # 摘要为空 —— 只有标题可用，是最难判的边界情况
    (
        "Comparison of AI-enabled cardiac chambers volumetry based on non-gated chest CT scans with echocardiography: "
        "An AI-CVD study within the Multi-Ethnic Study of Atherosclerosis (MESA).",
        "",
        "Am J Prev Cardiol",
    ),
    (
        "Recurrent Supraventricular Tachycardia Triggered by Tea Consumption in a Postoperative Adolescent Female: A Case Report.",
        "Supraventricular tachycardia (SVT) in adolescents may be triggered by physiological stress or caffeine. "
        "ECG showed SVT and intravenous adenosine restored sinus rhythm.",
        "Clin Case Rep",
    ),
    (
        "Pathogenic Variants in NKX2-5 Associated With Familial Atrial Septal Defect and Conduction System Disease.",
        "BACKGROUND: Most atrial septal defects (ASDs) occur sporadically, but familial cases can occur. "
        "CASE SUMMARY: We describe a 3-generation family with autosomal dominant secundum ASD and conduction "
        "system disease related to a novel NKX2-5 variant. The index patient presented with chronotropic "
        "incompetence, atrial fibrillation, left bundle branch block, and prolonged pauses, leading to "
        "pacemaker implantation. Her mother had an ASD, atrial fibrillation, left bundle branch block, and "
        "pulmonary arterial hypertension.",
        "JACC Case Rep",
    ),
    (
        "Structured postmortem genetic counselling and testing in sudden unexplained death among young adults "
        "in the Netherlands: results and implications regarding hereditary risk.",
        "Sudden unexplained death (SUD) in young adults may result from hereditary cardiovascular disorders that "
        "remain undetected even after conventional autopsy.",
        "J Community Genet",
    ),
]

#: 必须滤掉 —— 典型噪声（lead 确认）
MUST_DROP = [
    (
        "Technical safety and efficacy of PFA catheter electrode for airway ablation in a rat model of COPD.",
        "Chronic obstructive pulmonary disease (COPD) is characterized by progressive airway obstruction. "
        "Pulsed field ablation (PFA) has emerged as a promising non-thermal technology. "
        "A dual-lumen PVC tube (outer diameter: 1.80 mm) was used.",
        "Respir Res",
    ),
    (
        "Rapidly Progressive Acute Limb Ischemia With Irreversible Gangrene and Major Amputation in a "
        "Resource-Limited Setting: A Case Report.",
        "Acute limb ischemia (ALI) is a vascular emergency in which delayed recognition can result in "
        "irreversible tissue necrosis, gangrene, limb loss.",
        "Cureus",
    ),
    (
        "In situ Simulation as a Training Tool in a Tertiary Pediatric Emergency Department in Italy.",
        "In situ simulation differs from in-centre simulation as it is conducted in the actual clinical "
        "environment, thereby enhancing contextual realism.",
        "Adv Med Educ Pract",
    ),
    (
        "Dose-dependent cardiac autonomic dysregulation under classroom crowding: heart rate variability "
        "analysis and machine learning classification.",
        "Classroom overcrowding affects hundreds of millions of students globally, yet their physiological "
        "impact on cardiac autonomic function remains uncharacterized.",
        "J Physiol Anthropol",
    ),
    (
        "Secondary Prevention Gaps at Recurrent Ischemic Stroke Admission: A Nationwide Registry Study.",
        "BACKGROUND AND PURPOSE: Recurrent ischemic stroke is a sentinel audit point for secondary prevention "
        "delivery. Many patients had a history of atrial fibrillation.",
        "J Stroke",
    ),
    (
        "Sex-related differences in baseline neuroimaging, treatment workflow, and outcomes of ischemic stroke "
        "following thrombectomy.",
        "PURPOSE: We investigated sex differences in baseline neuroimaging, treatment workflow among ischemic "
        "stroke patients undergoing endovascular treatment.",
        "Radiol Med",
    ),
]


def _make(title: str, abstract: str, journal: str) -> Paper:
    return Paper(id="1", title=title, summary=abstract, journal=journal, pmid="1")


# ---------------------------------------------------------------------------
# 回归：必须保留 / 必须滤掉
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title,abstract,journal", MUST_KEEP)
def test_must_keep_survives(title, abstract, journal):
    """11 篇真电生理论文必须全部通过阈值。"""
    result = score_text(title, abstract, journal)
    assert result.score >= DEFAULT_THRESHOLD, (
        f"{title[:60]} 得分 {result.score} < {DEFAULT_THRESHOLD}; 信号={result.strong} 负={result.negative}"
    )


@pytest.mark.parametrize("title,abstract,journal", MUST_DROP)
def test_must_drop_is_filtered(title, abstract, journal):
    """6 条典型噪声必须被滤掉。"""
    result = score_text(title, abstract, journal)
    assert result.score < DEFAULT_THRESHOLD, (
        f"{title[:60]} 得分 {result.score} >= {DEFAULT_THRESHOLD}; 信号={result.strong} 负={result.negative}"
    )


def test_all_must_keep_pass_together():
    assert sum(1 for t, a, j in MUST_KEEP if is_relevant(_make(t, a, j))) == len(MUST_KEEP)


def test_all_must_drop_fail_together():
    assert sum(1 for t, a, j in MUST_DROP if is_relevant(_make(t, a, j))) == 0


# ---------------------------------------------------------------------------
# 关键陷阱：每条都是实测踩过的坑
# ---------------------------------------------------------------------------

def test_airway_ablation_is_not_cardiac_ablation():
    """非心脏部位消融：即使含 "pulsed field ablation" 也不能算电生理。"""
    r = score_text(
        "PFA catheter electrode for airway ablation",
        "Pulsed field ablation for airway obstruction.",
        "Respir Res",
    )
    assert r.score < DEFAULT_THRESHOLD
    assert "ablation_cardiac" not in r.strong


def test_pvc_tube_is_not_premature_ventricular_complex():
    """"PVC tube"（聚氯乙烯管）是材质，不是室早。实测踩过。"""
    r = score_text("Device design", "We used a dual-lumen PVC tube as the sheath.", "Biomaterials")
    assert "pvc" not in r.strong


def test_pvc_burden_is_still_premature_ventricular_complex():
    """真·PVC（室早负荷）必须识别。"""
    r = score_text("PVC burden and cardiomyopathy", "Frequent PVCs were ablated.", "Heart Rhythm")
    assert "pvc" in r.strong


def test_absence_of_structural_heart_disease_not_penalized():
    """"absence of structural heart disease" 是 Brugada 的标准表述，不该扣分。"""
    r = score_text(
        "Brugada syndrome unmasked",
        "A cardiac channelopathy causing ventricular arrhythmias in the absence of structural heart disease.",
        "Cureus",
    )
    assert "structural_intervention" not in r.negative


def test_structural_intervention_still_penalized():
    r = score_text(
        "Polymeric occluders for interventional therapy",
        "Transcatheter closure of structural heart disease with occluders.",
        "Biomaterials",
    )
    assert "structural_intervention" in r.negative


def test_pulmonary_hypertension_does_not_trigger_hypertension_penalty():
    """肺动脉高压是并发症描述，不是「纯高血压研究」。"""
    r = score_text(
        "NKX2-5 variants and conduction disease",
        "Patients had atrial fibrillation, left bundle branch block, and pulmonary arterial hypertension.",
        "JACC Case Rep",
    )
    assert "hypertension_only" not in r.negative


def test_primary_hypertension_still_penalized():
    r = score_text(
        "Gut neuronal signalling in high blood pressure",
        "Hypertension affects 1 in 3 adults.",
        "Trials",
    )
    assert "hypertension_only" in r.negative


def test_stroke_context_penalized():
    r = score_text(
        "Ischemic stroke prevention",
        "Many patients had atrial fibrillation and underwent thrombectomy.",
        "J Stroke",
    )
    assert "stroke" in r.negative
    assert r.score < DEFAULT_THRESHOLD


def test_cardiac_anchor_alone_is_insufficient():
    """核心设计论断：只含 "cardiac"/"heart" 但没有强信号，不该通过。

    这正是 lead 否掉纯 (b) 方案的原因。
    """
    r = score_text(
        "Heart failure readmission prediction",
        "Cardiac function was assessed in heart failure patients.",
        "JACC Case Rep",
    )
    assert r.score < DEFAULT_THRESHOLD


# ---------------------------------------------------------------------------
# 强信号本身
# ---------------------------------------------------------------------------

def test_atrial_fibrillation_scores_high():
    r = score_text("Atrial fibrillation ablation outcomes", "AF ablation.", "Europace")
    assert r.score >= 0.5


def test_ventricular_tachycardia_recognized():
    r = score_text("Ventricular tachycardia storm", "", "Heart Rhythm")
    assert "vt" in r.strong


def test_brugada_and_long_qt_recognized():
    assert "inherited" in score_text("Brugada syndrome", "").strong
    assert "inherited" in score_text("Long QT syndrome in children", "").strong


def test_pacemaker_and_icd_recognized():
    assert "pacemaker" in score_text("Leadless pacemaker outcomes", "").strong
    assert "icd" in score_text("Subcutaneous ICD implantation", "").strong


def test_hrv_alone_is_not_enough():
    """HRV 是中性信号，单独不足以判定电生理。"""
    r = score_text("Heart rate variability in athletes", "HRV was measured.", "Sports Med")
    assert r.score < DEFAULT_THRESHOLD


def test_empty_paper_scores_zero():
    r = score_text("", "", "")
    assert r.score == 0.0
    assert r.strong == [] and r.negative == []


def test_score_bounded_zero_to_one():
    for title, abstract, journal in MUST_KEEP:
        assert 0.0 <= score_text(title, abstract, journal).score <= 1.0
    for title, abstract, journal in MUST_DROP:
        assert 0.0 <= score_text(title, abstract, journal).score <= 1.0


def test_result_explains_itself():
    """reasons 必须可读，供日志调参用。"""
    r = score_text("Atrial fibrillation ablation", "", "Europace")
    assert isinstance(r, RelevanceResult)
    assert "af" in r.strong
    assert r.reason  # 非空说明


# ---------------------------------------------------------------------------
# filter_papers：批量过滤 + 白名单直通
# ---------------------------------------------------------------------------

def test_filter_papers_drops_irrelevant():
    papers = [_make(t, a, j) for t, a, j in MUST_KEEP + MUST_DROP]
    kept, stats = filter_papers(papers, threshold=DEFAULT_THRESHOLD)
    assert stats.before == len(papers)
    assert stats.dropped_count == len(MUST_DROP)
    assert len(kept) == len(MUST_KEEP)


def test_journal_whitelist_bypasses_scoring():
    """期刊白名单命中的直接保留，不受评分过滤。

    这是常态保障：EP 专业期刊里的文献本身就是目标领域。
    （本次采集白名单恰为 0 篇，但机制必须在。）
    """
    paper = Paper(
        id="1", title="Unrelated looking title about statistics", pmid="1",
        journal="Heart Rhythm", summary="",
    )
    kept, stats = filter_papers(
        [paper], threshold=DEFAULT_THRESHOLD, journals=["Heart Rhythm", "Europace"]
    )
    assert kept == [paper]
    assert stats.kept_by_journal == 1
    assert stats.dropped_count == 0


def test_whitelist_matches_ignoring_case_and_dots():
    paper = Paper(id="1", title="x", pmid="1", journal="heart rhythm")
    kept, _ = filter_papers([paper], journals=["Heart Rhythm"])
    assert len(kept) == 1


def test_non_whitelist_journal_still_scored():
    paper = Paper(id="1", title="Ischemic stroke thrombectomy", pmid="1", journal="J Stroke", summary="")
    kept, stats = filter_papers([paper], journals=["Heart Rhythm"])
    assert kept == []
    assert stats.kept_by_journal == 0


def test_filter_disabled_keeps_everything():
    papers = [_make(t, a, j) for t, a, j in MUST_DROP]
    kept, stats = filter_papers(papers, enabled=False)
    assert len(kept) == len(papers)
    assert stats.dropped_count == 0


def test_threshold_is_adjustable():
    """调高阈值应当更严格。"""
    papers = [_make(t, a, j) for t, a, j in MUST_KEEP]
    strict, strict_stats = filter_papers(papers, threshold=0.99)
    loose, _ = filter_papers(papers, threshold=0.1)
    assert len(strict) <= len(loose)
    assert strict_stats.dropped_count >= 0


def test_same_paper_kept_at_loose_and_dropped_at_strict():
    """同一篇论文：threshold 0.25 下保留、0.5 下丢弃。

    样本得分落在 (0.25, 0.5) 区间（中性信号 + 心脏语境，无强信号），
    用来证明阈值确实在起作用，而不是个装饰参数。
    """
    # 样本：心脏语境 + 中性信号但无强信号 → 落在 (0.25, 0.5) 区间。
    # 注意不能用 "HRV + 运动"：那会被 hrv_as_metric 条件负信号扣到 0（实测踩过）。
    paper = _make(
        "Holter monitoring in older adults",
        "Ambulatory ECG monitoring was performed in adults.",
        "J Geriatr Cardiol",
    )
    score = score_paper(paper).score
    assert 0.25 <= score < 0.5, f"样本得分 {score} 不在 (0.25, 0.5) 区间，用例失去意义"

    kept_loose, _ = filter_papers([paper], threshold=0.25)
    kept_strict, _ = filter_papers([paper], threshold=0.5)
    assert len(kept_loose) == 1
    assert kept_strict == []


def test_stats_summary_is_readable():
    papers = [_make(t, a, j) for t, a, j in MUST_KEEP + MUST_DROP]
    _, stats = filter_papers(papers, threshold=DEFAULT_THRESHOLD)
    text = stats.summary()
    assert "34" in text or str(stats.before) in text
    assert str(stats.after) in text


def test_dropped_items_record_reason():
    papers = [_make(t, a, j) for t, a, j in MUST_DROP]
    _, stats = filter_papers(papers, threshold=DEFAULT_THRESHOLD)
    assert stats.dropped
    for title, score, reason in stats.dropped:
        assert isinstance(score, float)
        assert reason and title


# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------

def test_relevance_config_reads_yaml():
    rc = RelevanceConfig.load()
    assert rc.enabled is True
    assert rc.threshold == pytest.approx(0.35)
    assert rc.require_cardiac_context is True


def test_relevance_config_defaults_match_module():
    assert RelevanceConfig().threshold == DEFAULT_THRESHOLD


def test_relevance_config_custom_path(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("relevance:\n  enabled: false\n  threshold: 0.6\n", encoding="utf-8")
    rc = RelevanceConfig.load(path)
    assert rc.enabled is False
    assert rc.threshold == pytest.approx(0.6)


def test_relevance_config_missing_file_uses_defaults(tmp_path):
    rc = RelevanceConfig.load(tmp_path / "nope.yaml")
    assert rc.enabled is True and rc.threshold == DEFAULT_THRESHOLD


# ---------------------------------------------------------------------------
# 位置加权 / incidental 降权 / 标题部位规则 / 饱和累加
# 这一组对应 lead 复现的系统性假阳性：摘要里 incidental 提及强信号词 → 满分
# ---------------------------------------------------------------------------

#: lead 复现的原始用例（当时得分 1.0 被误留）
ILIAC_TITLE = (
    "Collapse and Thrombosis of an Iliac Branch Endovascular Device Following Blunt Force Trauma."
)
ILIAC_ABSTRACT = (
    "Iliac branch endovascular device thrombosis following blunt force trauma has rarely been "
    "reported. We describe an 81-year-old man who developed proximal aortobiiliac endograft "
    "collapse with subsequent thrombosis after blunt force trauma occurring two months "
    "following endovascular aneurysm repair. The patient delayed vascular follow-up for one "
    "year. Despite chronic therapeutic anticoagulation for atrial fibrillation, the patient "
    "developed progressive thrombosis with ischemic symptoms. The case highlights a rare "
    "mechanical cause of post-EVAR aortoiliac thrombosis."
)


def test_iliac_branch_device_is_dropped():
    """实测漏网的 false positive：髂动脉支架病例报告必须丢弃。"""
    r = score_text(ILIAC_TITLE, ILIAC_ABSTRACT, "Vasc Endovascular Surg")
    assert r.score < DEFAULT_THRESHOLD
    assert r.subject_mismatch is True


def test_lead_exact_reproduction_now_rejected():
    """lead 给出的精确复现：AF + pacemaker 只在合并症语境 → 必须丢弃。

    修复前该用例 score=1.0（强信号:af,pacemaker）。
    """
    r = score_text(
        ILIAC_TITLE,
        "...Atrial fibrillation was noted on admission electrocardiogram and the patient "
        "was started on a pacemaker for intermittent bradycardia.",
        "Vasc Endovascular Surg",
    )
    assert r.score < DEFAULT_THRESHOLD


def test_femoropopliteal_bypass_with_incidental_af_is_dropped():
    """构造用例 2：标题非心脏，AF 仅在摘要 incidental。"""
    r = score_text(
        "Femoropopliteal bypass outcomes in octogenarians: a retrospective cohort.",
        "Atrial fibrillation was noted on admission. Bypass patency was assessed at 12 months.",
        "J Vasc Surg",
    )
    assert r.score < DEFAULT_THRESHOLD


def test_carotid_endarterectomy_with_prior_pacemaker_is_dropped():
    """构造用例 3：12% had a prior pacemaker —— 既往史不是研究对象。"""
    r = score_text(
        "Carotid endarterectomy under regional anesthesia: 10-year institutional experience.",
        "Of 412 patients, 12% had a prior pacemaker. Stroke-free survival was excellent.",
        "J Vasc Surg",
    )
    assert r.score < DEFAULT_THRESHOLD


def test_af_excluded_study_is_heavily_downweighted():
    """构造用例 4：标题含 AF 但正文说 "patients with AF were excluded" → 显著降分。"""
    r = score_text(
        "Exercise capacity in metabolic syndrome: a cross-sectional study.",
        "Patients with atrial fibrillation or prior ablation were excluded. "
        "We enrolled 200 adults and measured exercise capacity.",
        "Metabolism",
    )
    assert r.score < DEFAULT_THRESHOLD
    assert "af" in r.incidental


def test_title_signal_outweighs_abstract_signal():
    """位置加权：同样的信号放标题权重应高于放摘要中部。"""
    title_case = score_text("Ventricular tachycardia ablation outcomes", "", "Heart Rhythm")
    body_case = score_text(
        "Ablation outcomes in a general cohort",
        "We enrolled patients. Outcomes were recorded at 12 months. "
        "Ventricular tachycardia recurred in 20%.",
        "Heart Rhythm",
    )
    assert title_case.score > body_case.score


def test_first_sentence_outweighs_body_sentence():
    """首句里的强信号权重应高于摘要中部。

    注意标题不能写成 "Ablation study" —— 会触发 ML「消融实验」负信号（实测踩过）。
    """
    first = score_text(
        "Outcomes registry of arrhythmia patients",
        "Ventricular tachycardia was the primary outcome. We enrolled 100 patients.",
        "Heart Rhythm",
    )
    body = score_text(
        "Outcomes registry of arrhythmia patients",
        "We enrolled 100 patients in this registry. Outcomes were collected. "
        "Ventricular tachycardia was the primary outcome.",
        "Heart Rhythm",
    )
    assert first.weighted["vt"] > body.weighted["vt"]
    assert first.score > body.score


def test_saturation_prevents_two_signals_from_maxing_out():
    """饱和累加：两条强信号不再直接顶到 1.0（修复前 af+pacemaker = 1.0）。"""
    r = score_text(
        "Device outcomes",
        "We studied outcomes. Atrial fibrillation and pacemaker use were recorded.",
        "Some Journal",
    )
    assert r.score < 0.9


def test_noncardiac_subject_titles_are_rejected():
    for title in (
        "Thoracic aortic aneurysm repair outcomes",
        "Airway management in the emergency department",
        "Renal artery stenting for resistant hypertension",
        "Head and neck reconstruction with free flap",
        "Prostate cancer screening in elderly men",
        "Hepatic resection outcomes",
        "Thyroidectomy complications",
        "Bronchial thermoplasty for asthma",
    ):
        r = score_text(title, "Atrial fibrillation was noted on admission.", "J Gen Med")
        assert r.score < DEFAULT_THRESHOLD, title



def test_cardiac_title_with_noncancer_marker_is_not_shortcut():
    """反例：标题含非心脏部位但也有心脏词（如 "aortic valve"）时不走短路规则。"""
    r = score_text(
        "Aortic valve replacement and atrial fibrillation outcomes",
        "Atrial fibrillation was common.",
        "Heart Rhythm",
    )
    assert r.subject_mismatch is False
    assert r.score >= DEFAULT_THRESHOLD


# ---------------------------------------------------------------------------
# require_cardiac_context 硬门槛
# ---------------------------------------------------------------------------

def test_require_cardiac_context_gate_blocks_contextless_ablation():
    """开启门槛后，无任何心脏语境的消融/器械类论文直接判无关。

    这正是该开关的价值：挡住"通篇没有心脏语境却有强信号词"的漏网。
    """
    title = "Radiofrequency ablation outcomes in solid organ lesions"
    abstract = "We performed catheter ablation in 40 patients with hepatic lesions."
    off = score_text(title, abstract, "J Surg Oncol", require_cardiac_context=False)
    on = score_text(title, abstract, "J Surg Oncol", require_cardiac_context=True)
    assert off.score >= DEFAULT_THRESHOLD       # 关：靠强信号过线
    assert on.score == 0.0                      # 开：被门槛拦下
    assert on.missing_cardiac_context is True


def test_require_cardiac_context_does_not_hurt_real_ep_papers():
    """开启门槛不能误伤真电生理论文（含 SVT 这类只有 supraventricular 的）。"""
    for title, abstract, journal in MUST_KEEP:
        r = score_text(title, abstract, journal, require_cardiac_context=True)
        assert r.score >= DEFAULT_THRESHOLD, f"误伤: {title[:60]} score={r.score}"
        assert r.missing_cardiac_context is False


def test_supraventricular_tachycardia_counts_as_cardiac_context():
    """实测踩过：原语境正则不含 tachycardia/supraventricular，
    开启门槛后把 Clin Case Rep 的 SVT 病例报告误杀（0.62 → 0.00）。"""
    r = score_text(
        "Recurrent Supraventricular Tachycardia Triggered by Tea Consumption: A Case Report.",
        "ECG showed SVT and intravenous adenosine restored sinus rhythm.",
        "Clin Case Rep",
        require_cardiac_context=True,
    )
    assert r.has_cardiac_context is True
    assert r.score >= DEFAULT_THRESHOLD


def test_filter_papers_respects_require_cardiac_context():
    """filter_papers 必须把开关透传下去（否则又变成死字段）。"""
    paper = _make(
        "Radiofrequency ablation outcomes in solid organ lesions",
        "We performed catheter ablation in 40 patients with hepatic lesions.",
        "J Surg Oncol",
    )
    kept_off, _ = filter_papers([paper], threshold=DEFAULT_THRESHOLD, require_cardiac_context=False)
    kept_on, _ = filter_papers([paper], threshold=DEFAULT_THRESHOLD, require_cardiac_context=True)
    assert len(kept_off) == 1
    assert kept_on == []


def test_whitelist_bypass_still_works_with_cardiac_context_gate():
    """白名单直通不受 cardiac context 门槛影响。"""
    paper = _make("Twenty Years at the Crossroads", "", "JACC Clin Electrophysiol")
    kept, stats = filter_papers(
        [paper],
        journals=["JACC Clin Electrophysiol"],
        require_cardiac_context=True,
    )
    assert len(kept) == 1
    assert stats.kept_by_journal == 1


# ---------------------------------------------------------------------------
# lead 点名的保留确认
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "title,abstract,journal",
    [
        (
            "Rapid Progression of Cardiac Allograft Vasculopathy With Sudden Cardiac Death "
            "Within the First Year of Heart Transplantation: A Case Report.",
            "CAV results in progressive coronary artery stenosis and may lead to arrhythmias and "
            "sudden cardiac death.",
            "Transplant Proc",
        ),
        (
            "Stellate ganglion block for ventricular arrhythmia disease states: Emerging "
            "treatment options.",
            "Autonomic modulation is an effective therapeutic target in the management of "
            "ventricular arrhythmias and electrical storm, leading to treatment of ventricular "
            "tachycardia.",
            "J Anesth Transl Med",
        ),
        (
            "Determinants of outcome in atrial functional tricuspid regurgitation.",
            "Atrial functional tricuspid regurgitation (AFTR) is increasingly recognized as a "
            "result of atrial remodelling in patients with atrial fibrillation (AF).",
            "Echo Res Pract",
        ),
    ],
)
def test_lead_named_keep_cases(title, abstract, journal):
    """lead 明确要求保留的边界样本（含 Echo Res Pract 的 atrial functional TR）。"""
    assert score_text(title, abstract, journal).score >= DEFAULT_THRESHOLD


# ---------------------------------------------------------------------------
# 三条边界样本的最终判定（lead 2026-10-20 复核后定稿）
# ---------------------------------------------------------------------------

def test_aconitum_poisoning_is_kept():
    """Aconitum 中毒：VT→VF→心脏骤停是该病例的核心临床过程，属室性心律失常。

    lead 曾判"该滤"，复核真实摘要后**纠正为必须保留**。
    切勿加 toxicology 负信号 —— 会误杀这条。
    """
    r = score_text(
        "Seizures Secondary to Aconitum Plant Poisoning: a case report.",
        "We report a 53-year-old man who presented with ventricular tachycardia rapidly "
        "degenerating into ventricular fibrillation and cardiac arrest following ingestion "
        "of Aconitum. Initial convulsions were noted.",
        "J Med Case Rep",
    )
    assert r.score >= DEFAULT_THRESHOLD
    assert "vt" in r.strong


def test_hrv_ibd_activity_is_dropped():
    """HRV 只是测量工具，研究对象是 IBD 活动度 → 丢弃。"""
    r = score_text(
        "Association between heart rate variability and inflammatory bowel disease activity: "
        "a prospective cohort study.",
        "Heart rate variability (HRV) is one of the methods used to assess autonomic nervous "
        "system activity. We determined whether HRV monitoring can be used to assess the "
        "activity of inflammatory bowel diseases.",
        "Prz Gastroenterol",
    )
    assert r.score < DEFAULT_THRESHOLD
    assert "hrv_as_metric" in r.negative


def test_fish_oil_review_is_dropped():
    """鱼油纯度/生物利用度综述，AF 只是泛泛提到的健康结局 → 丢弃。"""
    r = score_text(
        "Natural Fish Oil Versus Purified Fish Oil: Composition, Efficacy, and Applications.",
        "This review proposes a purity-bioavailability-indication framework to differentiate "
        "natural fish oil and purified fish oil. NFO retains natural triglyceride structures "
        "but lower omega-3 polyunsaturated fatty acids content.",
        "J Nutr Metab",
    )
    assert r.score < DEFAULT_THRESHOLD
    assert "nutrition_supplement" in r.negative


# ---------------------------------------------------------------------------
# 条件负信号的对照用例：标题有 EP 诊断词时必须放行
# ---------------------------------------------------------------------------

def test_nutrition_negative_signal_does_not_fire_when_title_has_ep_diagnosis():
    """标题含 AF 的营养干预研究是真 EP 研究，不能被 nutrition_supplement 误杀。"""
    r = score_text(
        "Nutritional supplements and incident AF",
        "We studied dietary supplements.",
        "J Nutr",
    )
    assert r.score >= DEFAULT_THRESHOLD
    assert "nutrition_supplement" not in r.negative


def test_omega3_af_recurrence_trial_is_kept():
    """Omega-3 与 AF 复发 RCT 是真电生理研究。"""
    r = score_text(
        "Omega-3 fatty acids and atrial fibrillation recurrence: a randomized trial",
        "Fish oil was given to patients.",
        "Circ Arrhythm Electrophysiol",
    )
    assert r.score >= DEFAULT_THRESHOLD
    assert "nutrition_supplement" not in r.negative


def test_hrv_signal_does_not_fire_when_title_has_arrhythmia():
    """HRV 预测室性心律失常复发是真 EP 研究。"""
    r = score_text(
        "Heart rate variability predicts ventricular arrhythmia recurrence",
        "HRV was measured.",
        "Heart Rhythm",
    )
    assert r.score >= DEFAULT_THRESHOLD
    assert "hrv_as_metric" not in r.negative


def test_conditional_negatives_require_absence_of_ep_title():
    """直接验证条件负信号的触发前提。"""
    from daily_ep.relevance import _conditional_negative_hits, TITLE_EP_DIAGNOSIS

    assert TITLE_EP_DIAGNOSIS.search("Atrial fibrillation ablation outcomes")
    assert not TITLE_EP_DIAGNOSIS.search("Natural Fish Oil Versus Purified Fish Oil")

    with_ep = _conditional_negative_hits(
        "Omega-3 and atrial fibrillation recurrence",
        "heart rate variability and fish oil",
        ["heart rate variability and fish oil"],
    )
    assert with_ep == []          # 标题有 EP 诊断词 → 不触发

    without_ep = _conditional_negative_hits(
        "Nutritional review of fish oil",
        "heart rate variability was measured",
        ["heart rate variability was measured"],
    )
    assert "hrv_as_metric" in without_ep


# ---------------------------------------------------------------------------
# 合并症类负信号的条件化（防误杀真电生理论文）
# ---------------------------------------------------------------------------

def test_af_after_cardiac_surgery_with_hypertension_comorbidity_is_kept():
    """实测误杀：标题就是「AF after Cardiac Surgery」，摘要里 62% 合并高血压。

    hypertension 只是基线协变量，不该把标准术后房颤研究压到阈值下。
    修复前 score=0.24（强信号 af 0.42 → 被 -0.30 压掉）。
    """
    r = score_text(
        "Investigating Factors Associated with the Occurrence of Atrial Fibrillation "
        "after Cardiac Surgery",
        "We investigated perioperative factors associated with the occurrence of atrial "
        "fibrillation after cardiac surgery in 320 patients. Hypertension was present in "
        "62% of patients. Postoperative atrial fibrillation occurred in 28%.",
        "J Cardiothorac Surg",
    )
    assert r.score >= DEFAULT_THRESHOLD
    assert "hypertension_only" not in r.negative


def test_postoperative_af_risk_factors_is_kept():
    r = score_text(
        "Postoperative atrial fibrillation: incidence and risk factors",
        "62% had hypertension. AF occurred in 28%.",
        "J Thorac Cardiovasc Surg",
    )
    assert r.score >= DEFAULT_THRESHOLD


def test_hypertension_and_incident_af_is_kept():
    """标题同时含高血压与 AF：本身就是 EP 相关研究，应保留。"""
    r = score_text(
        "Hypertension and incident atrial fibrillation",
        "Hypertension is a risk factor for AF.",
        "J Hypertens",
    )
    assert r.score >= DEFAULT_THRESHOLD
    assert "hypertension_only" not in r.negative


def test_hypertension_still_penalized_without_ep_title():
    """标题无 EP 词时高血压负信号照常生效（原始行为不能被破坏）。"""
    r = score_text(
        "Blood pressure control in primary care",
        "Hypertension management was audited in 500 adults.",
        "Fam Pract",
    )
    assert "hypertension_only" in r.negative


def test_ep_survey_is_kept():
    """实测误杀：education_admin 里的裸词 survey 杀掉了正经 EP 调查类论文。"""
    r = score_text(
        "A survey of anticoagulation practices in atrial fibrillation",
        "A survey of physicians was conducted.",
        "Heart Rhythm",
    )
    assert r.score >= DEFAULT_THRESHOLD
    assert "education_admin" not in r.negative


def test_simulation_training_without_ep_title_still_dropped():
    """标题无 EP 诊断词的教育类论文仍要被滤掉。"""
    r = score_text(
        "In situ Simulation as a Training Tool in a Pediatric Emergency Department",
        "In situ simulation improves preparedness.",
        "Adv Med Educ Pract",
    )
    assert r.score < DEFAULT_THRESHOLD


def test_structural_heart_disease_with_af_title_is_kept():
    r = score_text(
        "Structural heart disease and atrial fibrillation: a cohort study",
        "We studied AF patients.",
        "Heart Rhythm",
    )
    assert r.score >= DEFAULT_THRESHOLD
    assert "structural_intervention" not in r.negative


def test_occluder_paper_without_ep_title_still_dropped():
    r = score_text(
        "Polymeric and metallic occluders for interventional therapy of cardiovascular diseases",
        "Occluders are used in structural heart disease.",
        "Biomaterials",
    )
    assert r.score < DEFAULT_THRESHOLD


def test_conditional_negative_names_are_the_comorbidity_class():
    """只有「合并症/研究类型」类负信号可被条件化；研究对象不符类必须保持无条件。"""
    from daily_ep.relevance import CONDITIONAL_NEGATIVE_NAMES

    assert "hypertension_only" in CONDITIONAL_NEGATIVE_NAMES
    assert "education_admin" in CONDITIONAL_NEGATIVE_NAMES
    assert "structural_intervention" in CONDITIONAL_NEGATIVE_NAMES
    # 这些表示「研究对象是别的器官/系统」，绝不能因标题有 EP 词就放过
    for name in ("stroke", "limb_ischemia", "noncardiac_ablation", "vascular_device", "hf_only"):
        assert name not in CONDITIONAL_NEGATIVE_NAMES


def test_stroke_paper_with_af_title_still_dropped():
    """标题含 AF 的卒中论文仍应被 stroke 负信号滤掉（stroke 无条件）。"""
    r = score_text(
        "Acute ischemic stroke despite NOAC therapy in atrial fibrillation",
        "Clinical factors associated with ischemic stroke despite anticoagulation.",
        "J Stroke",
    )
    assert r.score < DEFAULT_THRESHOLD
    assert "stroke" in r.negative
