"""电生理相关性评分。

**为什么需要它**：检索式里的 ``[tiab]`` 主题词是无语境的子串匹配，
   - ``ablation`` 会命中气道消融、肿瘤消融、机器学习「消融实验」；
   - ``atrial fibrillation`` 会命中「摘要里提了一句 AF」的卒中/外科/影像论文。
实测 34 条里有 10+ 条与电生理无关。

**为什么单纯加心脏锚定不够**：实测 ``(主题词) AND (heart OR cardiac ...)`` 只把
34 条减到 26 条，最差的几条**全部留下** —— 因为
   - "airway ablation in a rat model of COPD" 的摘要里就有 "cardiac"；
   - 下肢缺血、儿科模拟教学、教室拥挤与 HRV 的摘要里也都有 "cardiac"/"heart"。
所以锚定是**必要但不充分**，这里改成「多项证据加权打分」：

    score = w_strong·(强信号命中) + w_neutral·(中性信号) + w_context·(心脏语境)
            - 负信号惩罚

强信号 / 负信号是**短语级**规则（见 ``STRONG_SIGNALS`` / ``NEGATIVE_SIGNALS``），
每个信号可带 ``exclude`` 列表：命中主词但紧邻部位修饰词时不计分
（如 "airway ablation" 不算电生理消融）。

**负信号的两分法**（``CONDITIONAL_NEGATIVE_NAMES`` 的选取原则）：
负信号分两类，边界是「命中词描述的是研究对象本身，还是仅仅出现在研究对象旁边」：

  - **无条件**（器官/系统类，如 ``stroke`` / ``limb_ischemia`` / ``vascular_device`` /
    ``noncardiac_ablation`` / ``hf_only``）：命中即说明这篇文献研究的是**别的器官系统**，
    即使标题带 EP 词也只是「共病视角」，应当扣分。**不要往条件化名单里加这类。**
  - **条件触发**（合并症/研究类型类，如 ``hypertension_only`` / ``education_admin`` /
    ``structural_intervention``）：词本身只是背景（"62% 合并高血压"），
    或只在非 EP 语境下才有意义（裸词 ``survey`` 会误伤「某术式的全国调查」）。
    判定前提是 ``TITLE_EP_DIAGNOSIS``：**标题已声明 EP 诊断 → 不扣分**，
    因为标题写的才是研究对象，摘要里的合并症提及不该推翻它。

**标题下限保护（标题有 EP 词就强制过阈值）已评估并否决**：真实 43 篇上实测会放行
5 篇噪声（含卒中/AF 论文、ECG 采集硬件、教学仿真、内分泌病例），且条件化之后
已无「标题明确 EP 却被误杀」的案例。若将来有人想加回来，请先重跑该评估。

本模块是**纯函数**，不打网络、不读文件，便于单元测试与调参。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, NamedTuple, Optional, Pattern, Sequence, Tuple

from .schema import Paper

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 0.35


# ---------------------------------------------------------------------------
# 信号定义
# ---------------------------------------------------------------------------

class Signal(NamedTuple):
    """一条信号规则。

    ``pattern`` 是主词正则；``exclude`` 里的正则若出现在**同一个句子**中，
    则该主词不算命中（用于排除 "airway ablation" 这种部位修饰）。
    """

    name: str
    pattern: Pattern[str]
    exclude: Tuple[Pattern[str], ...] = ()


def _sig(name: str, pattern: str, excludes: Sequence[str] = ()) -> Signal:
    return Signal(
        name=name,
        pattern=re.compile(pattern, re.IGNORECASE),
        exclude=tuple(re.compile(e, re.IGNORECASE) for e in excludes),
    )


#: 非心脏部位修饰词：与其同句的 "ablation" 不算电生理消融
_NONCARDIAC_SITE = (
    r"(?:airway|airways|bronch\w*|lung|lungs|pulmonary\s+(?:vein\s+)?(?:tumou?r|nodule|lesion)s?"
    r"|thyroid|renal|kidney|hepatic|liver|prostate|pancrea\w*|gastric|esophageal|oesophageal"
    r"|colonic|colorectal|uterine|ovarian|breast|bone|spinal|spine|brain|cerebral|skin"
    r"|tumou?r|tumous?r|neoplas\w*|cancer|carcinoma|malignan\w*|metasta\w*|osteoid|barrett)"
)

#: 强信号：出现基本就是电生理文献
STRONG_SIGNALS: Tuple[Signal, ...] = (
    # --- 房性心律失常 ---
    # ⚠️ AF 不能只是标题里的缩写repeat：这里只用展开式或独立的 AF 缩写（见 af_abbrev）
    # 独立的 "AF" 缩写也要认（"incident AF" / "AF burden" / "AF recurrence"）。
    # 用常见搭配而非裸 AF，避免 "AF" 在别的语境下误命中。
    _sig("af", r"\batrial\s+fibrillation\b|\batrial\s+flutter\b|\bAFib\b"
               r"|\bAF\s+(?:burden|recurrence|ablation|patients?|cohort|risk|detection"
               r"|incidence|management)\b"
               r"|\b(?:incident|new[-\s]?onset|paroxysmal|persistent|postoperative)\s+AF\b"),
    _sig("pvi", r"\bpulmonary\s+vein\s+isolation\b|\bPVI\b"),
    # --- 室性心律失常 ---
    # ⚠️ 必须覆盖 "ventricular arrhythmias" / "electrical storm"：
    #    J Anesth Transl Med 那篇标题写的是 "ventricular arrhythmia"（不是 VT），
    #    原正则只认 tachycardi/fibrillat，导致真论文差点被滤掉（实测）。
    _sig("vt", r"\bventricular\s+(?:tachycardi|fibrillat|arrhythmi)\w*"
               r"|\belectrical\s+storm\b"
               r"|\b(?:VT|VF)\s+(?:storm|ablation|substrate"
               r"|induction|inducibility|recurrence|free\s+survival)\b"),
    # ⚠️ PVC 必须排除聚氯乙烯材质 "PVC tube"/"PVC catheter"（实测踩过）
    _sig(
        "pvc",
        r"\bpremature\s+ventricular\s+(?:complex|contraction)s?\b|\bPVCs\b"
        r"|\bPVC\s+(?:burden|ablation|frequency|count|induction)\b",
        excludes=(r"\bPVC\s+(?:tube|tubing|catheter|material|plastic|pipe)\b",),
    ),
    _sig("scd", r"\bsudden\s+(?:cardiac|arrhythmic)\s+death\b|\bSUDEP\b"
                r"|\bsudden\s+unexplained\s+death\b|\bSCD\b(?!\w)"),
    _sig("arrhythmogenic", r"\barrhythmogenic\b"),
    # 心律失常本身的诊疗（非 VT/AF 特定）：
    # 儿科急诊「心悸/心律失常风险评估」类综述只有这条能命中。
    # 要求 "cardiac arrhythmia" / "arrhythmia risk" 这类组合，避免裸 "arrhythmia" 过宽。
    _sig("arrhythmia_care", r"\bcardiac\s+arrhythmi\w*|\barrhythmi\w+\s+(?:risk|detection|diagnosis|"
                             r"management|burden|recurrence|monitoring)\b"
                             r"|\b(?:diagnos\w+|identif\w+|detect\w*)\s+(?:of\s+)?"
                             r"(?:dangerous|life-threatening|cardiac)\s+arrhythmi\w*"),
    # --- 消融（必须排除非心脏部位修饰）---
    _sig(
        "ablation_cardiac",
        r"\b(?:catheter|pulsed\s+field|cryo|cryothermal|radiofrequency|surgical|STAR|"
        r"stereotactic\s+arrhythmia\s+radio)\s*(?:balloon\s*)?ablation\w*"
        r"|\bablation\s+of\s+(?:atrial|ventricular|the\s+atrial|the\s+ventricular|AF|VT)",
        excludes=(_NONCARDIAC_SITE,),
    ),    # --- 器械 ---
    _sig("icd", r"\bICDs?\b|\bimplantable\s+cardioverter[\s-]*defibrillators?\b"),
    _sig("crt", r"\bCRT[- ]?[DP]?\b|\bcardiac\s+resynchroni[sz]ation\b"),
    _sig("pacemaker", r"\bpacemakers?\b|\b(?:leadless|transcatheter|cardiac|biventricular)\s+pacing\b|\bCIEDs?\b"),
    _sig("defibrillator", r"\bdefibrillators?\b", excludes=(r"automated\s+external\s+defibrillator|\bAEDs?\b",)),
    # --- 标测与电生理检查 ---
    _sig("mapping", r"\belectroanatomic(?:al)?\s+mapping\b|\bsubstrate\s+mapping\b|\bphase\s+mapping\b"
                     r"|\brotors?\b|\bEP\s+study\b|\belectrophysiolog\w*\s+(?:study|testing)\b"),
    _sig("ep_study", r"\bprogrammed\s+(?:electrical\s+)?stimulation\b|\binduci\w+\s+(?:of\s+)?(?:VT|VF)\b"),
    # --- 遗传性心律失常综合征 ---
    _sig("inherited", r"\bbrugada\b|\blong\s+QT\b|\bLQTS\b|\bWPW\b|\bWolff[\s-]*Parkinson[\s-]*White\b"
                       r"|\b(?:AV|atrioventricular)\s+(?:nodal\s+)?re-?entry\b|\bAVNRT\b|\bAVRT\b"),
    _sig("svt", r"\bsupraventricular\s+tachycardi\w*|\bSVT\b"),
    # --- 电生理机理指标 ---
    _sig("ep_metric", r"\brefractory\s+period\w*|\bconduction\s+velocity\b|\bwavebreak\b"
                      r"|\bspiral\s+waves?\b|\bre-?entrant\b|\barrhythmogenesis\b|\bcardioversion\b"
                      r"|\belectrophysiologic(?:al)?\s+(?:propert|substrate|remodel\w+)"),
)

#: 中性信号：心血管相关，但本身不足以判定是电生理论文
NEUTRAL_SIGNALS: Tuple[Signal, ...] = (
    _sig("ecg", r"\bECG\b|\bEKG\b|\belectrocardiogra\w*"),
    _sig("holter", r"\bholter\b|\bambulatory\s+(?:ECG|monitoring)\b"),
    _sig("hrv", r"\bheart\s+rate\s+variab\w*|\bHRV\b"),
    _sig("cardiologist", r"\bcardiologist\w*"),
    _sig("arrhythmia", r"\barrhythmi\w*"),
    # 心电/心电成像类工具（AI-ECG、ECG 筛查等）—— 电生理相关但非特异性
    _sig("ai_ecg", r"\bAI[-\s]?ECG\b|\bdigital\s+ECG\b|\bECG[-\s]based\b"),
)

#: 「猝死基因检测/遗传咨询」类：电生理相关，但常不出现心律失常专有名词。
#: 单独成组，命中时按半个强信号计分（避免被 neutral 稀释掉）。
GENETIC_DEATH_SIGNALS: Tuple[Signal, ...] = (
    _sig(
        "sudden_death_genetics",
        r"\b(?:postmortem|post-mortem)\s+(?:genetic|molecular)\b"
        r"|\bgenetic\s+(?:counsell?ing|counsell?ing\s+and\s+testing|testing)\b"
        r"|\bhereditary\s+(?:cardiovascular|heart)\s+(?:disorders?|diseases?)\b"
        r"|\bsudden\s+(?:unexplained|unexpected)\s+death\b"
        r"|\bcardiogenetic\w*",
    ),
)

#: 「心电成像/心电图 AI」类：标题级出现即说明研究的是心电信号本身。
#: 实测 Am J Prev Cardiol 的 AI-ECG 研究 // 无摘要的 AI 心腔容积研究都属于此类，
#: 若只靠摘要里的 ECG 会漏掉（后者摘要为空）。
ECG_IMAGING_SIGNALS: Tuple[Signal, ...] = (
    _sig(
        "ecg_ai_title",
        r"\bAI[-\s]?ECG\b|\bartificial\s+intelligence[-\s]+guided\s+electrocardiogra\w*"
        r"|\belectrocardiogra\w+\s+\(AI[-\s]?ECG\)",
    ),
    _sig(
        "ecg_volumetry",
        # 心腔容积 / ECG-gated / AI-CVD 研究：都是心脏结构与电学成像，
        # 即便用了 non-gated CT 也仍属心血管影像评估，不能排除（实测踩过）
        r"\b(?:cardiac\s+chambers?\s+volumetr\w*|ECG[-\s]gat\w+|AI[-\s]CVD\s+study)\b",
    ),
)

#: 负信号：命中即扣分（每个信号最多扣一次）
NEGATIVE_SIGNALS: Tuple[Signal, ...] = (
    # 非心脏部位消融：这是 abation 假阳性的最大来源
    _sig(
        "noncardiac_ablation",
        r"\b(?:airway|bronchial|pulmonary|lung|thyroid|renal|hepatic|liver|prostate|pancreatic"
        r"|gastric|esophageal|oesophageal|colonic|uterine|ovarian|breast|bone|spinal|tumou?r"
        r"|neoplas\w*|cancer|carcinoma|malign\w*|metasta\w*)\s+(?:[a-z-]+\s+){0,2}ablat\w*",
    ),
    _sig("site_ablation", r"\bablati\w+\s+of\s+(?:the\s+)?(?:airway|lung|tumou?r|renal|liver|thyroid"
                          r"|prostate|cardiac\s+tumou?r)"),
    # 机器学习「消融实验」与蛋白/模型语境
    _sig("ml_ablation", r"\bablation\s+(?:stud(?:y|ies)|experiments?)\b|\bcomput\w*[- ]aware\b"),
    # 卒中/神经影像
    _sig("stroke", r"\bischemic\s+stroke\b|\bischaemic\s+stroke\b|\bcryptogenic\s+stroke\b"
                   r"|\bthrombectomy\b|\bneuroimag\w*|\bcerebral\b|\bintracranial\b"),
    # 教学/管理/质性研究
    _sig("education_admin", r"\bsimulation\s+(?:training|based\s+education)\b|\bcurricul\w*"
                            r"|\bhealthcare\s+utilization\b|\bhealth\s+care\s+utilization\b"
                            r"|\bqualitative\s+stud\w*|\bsurvey\b|\bfocus\s+groups?\b"
                            r"|\btraining\s+tool\b|\bmedical\s+education\b|\bcompetenc\w+\s+framework\b"),
    # 非电生理的血管/外科疾病
    _sig("limb_ischemia", r"\bacute\s+limb\s+ischemia?\b|\bgangrene\b|\bamputation\b"),
    _sig("flap_recon", r"\bflap\s+(?:reconstruction|surgery)\b|\bfree\s+flap\b|\bhead\s+and\s+neck\s+reconstruction\b"),
    # ⚠️ "absence of structural heart disease" 是 Brugada/离子通道病的标准表述，
    #    不能罚 —— 只有真正的结构性干预（封堵器/经导管关闭）才罚。
    _sig(
        "structural_intervention",
        r"\boccluders?\b|\btranscatheter\s+(?:closure|repair)\b"
        r"|\bstructural\s+heart\s+disease\b(?!\s*\))",
        excludes=(
            r"\b(?:absence|without|no|free)\s+of\s+structural\s+heart\s+disease\b"
            r"|\babsence\s+of\s+structural\b",
        ),
    ),
    # 纯心衰/代谢，无心律失常内容
    _sig("hf_only", r"\bheart\s+failure\s+(?:readmission|hospitali[sz]ation)\b|\bunplanned\s+readmission\b"),
    # ⚠️ 只罚「原发性高血压」语境；"pulmonary arterial hypertension" 是并发症描述，不该罚。
    # ⚠️ 且属 CONDITIONAL_NEGATIVE_NAMES：标题已声明 EP 诊断时不生效（见该常量说明）。
    _sig(
        "hypertension_only",
        r"\bhypertension\b|\bhigh\s+blood\s+pressure\b",
        excludes=(r"(?:pulmonary|portal|arterial|pulmonary\s+arterial)\s+hypertension",),
    ),
)

#: 「合并症 / 研究类型」类负信号：只有在**标题没有 EP 诊断词**时才生效。
#:
#: 为什么必须条件化（实测误杀）：
#:   - ``hypertension_only`` 是裸词模式，只要摘要出现 "hypertension" 就扣 0.30。
#:     但高血压在心血管论文里几乎无处不在（「62% 合并高血压」只是基线协变量）。
#:     实测把 "Investigating Factors Associated with the Occurrence of Atrial
#:     Fibrillation after Cardiac Surgery" 压到 0.24 丢掉了 —— 这是标准术后房颤研究。
#:   - ``education_admin`` 里的裸词 ``survey`` 同样致命：
#:     "A survey of anticoagulation practices in atrial fibrillation" 得 0.24 被误杀，
#:     而「某术式的全国调查」是正经电生理论文类型。
#:   - ``structural_intervention`` 的 "structural heart disease" 会误伤
#:     "Structural heart disease and atrial fibrillation"。
#:
#: 判据：标题里出现 AF/VT/ablation/pacemaker/ICD... 就说明**研究对象本身就是电生理**，
#: 此时摘要里的合并症提及不该再扣分。标题没有 EP 诊断词时，这些负信号照常生效
#: （如儿科模拟教学、心血管器械、IBD 的 HRV 研究仍会被滤掉）。
CONDITIONAL_NEGATIVE_NAMES = frozenset({"hypertension_only", "education_admin", "structural_intervention"})

#: 心脏语境锚定词（命中加分，说明文章确实在心内科语境）
#: ⚠️ 必须覆盖 tachycardia / supraventricular / sinus / conduction 等心律失常词，
#:    否则开 require_cardiac_context 会误伤真论文：
#:    实测 "Recurrent Supraventricular Tachycardia Triggered by Tea Consumption"
#:    全文只有 SVT/adolescent/adolescent，原正则一条都没命中 → 被门槛误杀。
CARDIAC_CONTEXT = re.compile(
    r"\b(?:heart|cardiac|cardio\w*|atrial|ventricular|supraventricular|arrhythmi\w*"
    r"|electrophysiolog\w*|electrocardiogra\w*|ECG|EKG|cardiovascular|myocardi\w*"
    r"|tachycardi\w*|bradycardi\w*|fibrillation|flutter|sinus\s+rhythm|conduction|bundle"
    r"|pacemaker\w*|defibrillator\w*|palpitations?|rhythm)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# 权重
# ---------------------------------------------------------------------------
#: 强信号基础分（单条，位于摘要非首句时的满值）。
#: 之所以给到 0.42：确保「标题/首句里出现 1 个强信号」就能过 0.35 阈值。
W_STRONG_BASE = 0.42
#: 同一条论文里第 2、3…个强信号的递减系数（饱和累加）。
#: 实测教训：af + pacemaker 两条词曾直接把无关论文顶到 1.0，
#: 递减后 2 个强信号 ≈ 0.42+0.25 ≈ 0.67，仍可判相关，但不再等于满分。
W_STRONG_DECAY = (1.0, 0.6, 0.4)

#: 位置权重：标题 > 摘要首句 > 其余句。
#: 核心依据：标题写的是研究对象，摘要首句通常是研究目的/背景主句；
#: 而摘要中部/末尾常是结果讨论里的附带提及。
W_POS_TITLE = 1.0
W_POS_FIRST_SENTENCE = 0.8
W_POS_BODY = 0.35

#: incidental（伴随/既往史/排除/对照）上下文中命中强信号时的额外折扣
W_INCIDENTAL = 0.3

W_NEUTRAL_EACH = 0.08    # 每个中性信号
W_CONTEXT = 0.12         # 心脏语境
# 半个强信号级证据（猝死遗传检测 / 心电成像）：
# 实测 0.22 时「心腔容积」这类条目得 0.34，刚好卡在 0.35 阈值下方 —— 太脆弱。
# 提到 0.26 让「标题级明确证据 + 心脏语境」能稳定过线。
W_GENETIC_EACH = 0.26
W_NEG_EACH = 0.30        # 每个负信号
W_NEG_STRONG = 0.55      # 最恶劣的负信号（非心脏部位消融）额外重罚

#: 句子切分：负信号的 exclude 在「同一句子」内生效
_SENTENCE_RE = re.compile(r"(?<=[.!?。！？;；])\s+|\n+")

#: 需要额外重罚的负信号
SEVERE_NEGATIVE = {
    "noncardiac_ablation",
    "site_ablation",
    "ml_ablation",
    "limb_ischemia",
    "vascular_device",
    "noncardiac_subject",
}

# ---------------------------------------------------------------------------
# incidental（附带提及）判定
# ---------------------------------------------------------------------------
#: 命中这些短语说明该句里的疾病词只是既往史/合并症/排除/对照，不是研究对象。
#: 实测来源：
#:   "Atrial fibrillation was noted on admission electrocardiogram and the patient was
#:    started on a pacemaker for intermittent bradycardia."  ← 髂动脉支架病例报告
#:   "Despite chronic therapeutic anticoagulation for atrial fibrillation, ..."  ← 同上
INCIDENTAL_PATTERNS: Tuple[Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bwas\s+noted\b|\bwere\s+noted\b",
        r"\bincidental\w*",
        r"\bhistory\s+of\b|\bpast\s+medical\s+history\b|\bmedical\s+history\b",
        r"\bcomorbid\w*",
        r"\bconcomitant\w*",
        r"\bcoexisting\b",
        r"\b(?:were|was)\s+excluded\b|\bexcluding\b|\bexcluded\b",
        r"\bno\s+history\s+of\b|\bwithout\s+(?:prior|any)\b|\bdid\s+not\s+have\b",
        r"\bunlike\b|\bin\s+contrast\s+to\b",
        r"\bcompared\s+(?:with|to)\s+patients\b",
        r"\b(?:started|placed|implanted|received)\s+on\s+a\s+pacemaker\b",
        r"\bbrought\s+to\s+(?:the\s+)?(?:cardiac\s+)?cath",  # 无关语境提一句
    )
)

# ---------------------------------------------------------------------------
# 标题级「研究对象解剖部位」规则（最稳、误伤最小的一条）
# ---------------------------------------------------------------------------
#: 标题里出现这些**非心脏**解剖部位/术式，且标题里没有任何心脏/电生理词时，
#: 直接判无关 —— 因为标题写的就是研究对象。
#: 依据：髂动脉支架病例报告的标题 "Collapse and Thrombosis of an Iliac Branch
#: Endovascular Device Following Blunt Force Trauma" 里没有一个心脏词，
#: AF 只出现在摘要的合并症描述里。
NONCARDIAC_SUBJECT = re.compile(
    r"\b(?:iliac|aortoiliac|femoropopliteal|femoral\b|popliteal|carotid|subclavian"
    r"|aortic\s+aneurysm\w*|abdominal\s+aortic|endovascular\s+device|endograft|stent\s+graft"
    r"|peripheral\s+(?:artery|arterial|vascular)|iliac\s+branch"
    r"|flap|head\s+and\s+neck|airway|bronchial|larynx\w*|thyroid|prostate|hepatic\b|liver\b"
    r"|renal\s+artery|retinal|ocular|skin\b|dental|orthopaedic|orthopedic|colorectal"
    r"|gastric|esophageal|oesophageal|bariatric|hysterectomy|ovarian|uterine)\b",
    re.IGNORECASE,
)

#: 判「这是心脏/电生理论文」的标题级词
CARDIAC_TITLE = re.compile(
    r"\b(?:heart|cardiac|cardio\w*|atrial|ventricular|arrhythmi\w*|electrophysiolog\w*"
    r"|electrocardiogra\w*|ECG|EKG|pacemaker\w*|defibrillator\w*|ECG|cath\s+lab"
    r"|sinus|conduction|bundle|myocardi\w*|cardiomyopath\w*|valv\w*"
    r"|coronary|pulmonary\s+vein|aortic\s+valve|tachycardi\w*|fibrillation|flutter)\b",
    re.IGNORECASE,
)

#: 病历报告中「合并症」类短语，用于 incidental 判定补充
_VASCULAR_DEVICE = re.compile(
    r"\b(?:endovascular\s+device|endograft|stent\s+graft|stent-graft|femoropopliteal\s+bypass"
    r"|carotid\s+endarterectomy|peripheral\s+artery\s+disease|claudication|EVAR)\b",
    re.IGNORECASE,
)

#: 额外的负信号（补充在 NEGATIVE_SIGNALS 之后）
EXTRA_NEGATIVE_SIGNALS: Tuple[Signal, ...] = (
    _sig(
        "vascular_device",
        r"\b(?:endovascular\s+device|endograft|stent\s+graft|stent-graft|femoropopliteal\s+bypass"
        r"|carotid\s+endarterectomy|iliac\s+(?:branch|artery)|aortobiiliac"
        r"|peripheral\s+artery\s+disease|claudication|EVAR)\b",
    ),
)

# ---------------------------------------------------------------------------
# 条件负信号（只在「标题里没有心脏电生理诊断词」时才生效）
# ---------------------------------------------------------------------------
# 这类词本身是普通医学/营养/统计概念，经常出现在真电生理论文里当**测量工具**或
# **干预手段**：
#   - "Omega-3 fatty acids and atrial fibrillation recurrence: a randomized trial"  ← 真 EP 研究
#   - "Heart rate variability predicts ventricular arrhythmia recurrence"            ← 真 EP 研究
# 所以不能无条件扣分，必须以标题是否给出 EP 诊断词作为前提。

#: 「标题里有电生理诊断词」的判定。比 CARDIAC_TITLE 更严格：
#: 只认诊断/术式，不认 heart/cardiac 这类宽泛词 —— 因为
#: "Heart rate variability and IBD activity" 标题里也有 "heart"，
#: 若用宽泛词判定就永远无法触发条件负信号。
TITLE_EP_DIAGNOSIS = re.compile(
    r"\b(?:atrial\s+fibrillation|atrial\s+flutter|AFib|AF\b"
    r"|ventricular\s+(?:tachycardi|fibrillat|arrhythmi)\w*|\bVT\b|\bVF\b|\bPVCs?\b"
    r"|arrhythmi\w*|supraventricular|tachycardi\w*|bradycardi\w*"
    r"|ablation\w*|pulmonary\s+vein\s+isolation|\bPVI\b"
    r"|pacemaker\w*|\bICD\b|implantable\s+cardioverter|defibrillator\w*"
    r"|cardiac\s+resynchronization|\bCRT\b|electrophysiolog\w*"
    r"|electrocardiogra\w*|\bECG\b|\bEKG\b|brugada|long\s+QT|channelopathy"
    r"|sudden\s+cardiac\s+death|cardiac\s+arrest|palpitations?|syncope)\b",
    re.IGNORECASE,
)

#: HRV 仅作为测量工具时的负信号（条件触发）
HRV_METRIC_SIGNAL = _sig(
    "hrv_as_metric",
    r"\bheart\s+rate\s+variab\w*|\bHRV\b",
)

#: 营养补充剂综述的负信号（条件触发）
NUTRITION_SIGNAL = _sig(
    "nutrition_supplement",
    r"\b(?:fish\s+oil|omega[-\s]?3|ω[-\s]?3|n[-\s]?3\s+(?:fatty\s+acids?|PUFA)"
    r"|polyunsaturated\s+fatty\s+acids?|\bPUFA\w*"
    r"|nutritional\s+supplement\w*|dietary\s+supplement\w*|food\s+supplement\w*)\b",
)


def _conditional_negative_hits(
    title: str, haystack: str, sentences: Sequence[str]
) -> List[str]:
    """条件负信号：标题无 EP 诊断词，但正文在讲 HRV 工具 / 营养补充。

    返回命中的信号名列表（可能为空）。
    """
    if TITLE_EP_DIAGNOSIS.search(title):
        return []
    hits: List[str] = []
    if _signal_hit(HRV_METRIC_SIGNAL, haystack, sentences):
        hits.append(HRV_METRIC_SIGNAL.name)
    if _signal_hit(NUTRITION_SIGNAL, haystack, sentences):
        hits.append(NUTRITION_SIGNAL.name)
    return hits


# ---------------------------------------------------------------------------
# 评分
# ---------------------------------------------------------------------------

@dataclass
class RelevanceResult:
    """评分结果。``reasons`` 用于日志/调参，不写入 JSONL。"""

    score: float
    strong: List[str] = field(default_factory=list)
    neutral: List[str] = field(default_factory=list)
    negative: List[str] = field(default_factory=list)
    has_cardiac_context: bool = False
    #: strong 里每条信号最终用了多少权重（用于调参/回归排查）
    weighted: Dict[str, float] = field(default_factory=dict)
    incidental: List[str] = field(default_factory=list)
    #: 标题部位规则直接判无关时置 True
    subject_mismatch: bool = False
    #: require_cardiac_context 门槛拦下时置 True
    missing_cardiac_context: bool = False

    @property
    def reason(self) -> str:
        parts = []
        if self.subject_mismatch:
            parts.append("标题非心脏解剖部位且无心脏词")
        if self.missing_cardiac_context:
            parts.append("无心脏语境(require_cardiac_context)")
        if self.strong:
            detail = ",".join(
                f"{name}({self.weighted.get(name, 0):.2f})" for name in self.strong
            )
            parts.append("强信号:" + detail)
        if self.incidental:
            parts.append("伴随提及:" + ",".join(sorted(set(self.incidental))))
        if self.negative:
            parts.append("负信号:" + ",".join(self.negative))
        if not parts:
            parts.append("无电生理强信号")
        return "; ".join(parts)


def _split_sentences(text: str) -> List[str]:
    return [s for s in _SENTENCE_RE.split(text) if s and s.strip()]


def _signal_hit(signal: Signal, text: str, sentences: Sequence[str]) -> bool:
    """信号是否命中。

    有 exclude 规则时逐句判断：主词与部位修饰词在**同一句**内则不算命中。
    """
    if not signal.pattern.search(text):
        return False
    if not signal.exclude:
        return True
    for sentence in sentences:
        if not signal.pattern.search(sentence):
            continue
        if any(ex.search(sentence) for ex in signal.exclude):
            continue
        return True
    return False


def _is_incidental(sentence: str) -> bool:
    """该句是否属于「附带提及」上下文。"""
    return any(p.search(sentence) for p in INCIDENTAL_PATTERNS)


def _position_weight(index: Optional[int]) -> float:
    """句子位置权重。``None`` 表示标题。"""
    if index is None:
        return W_POS_TITLE
    return W_POS_FIRST_SENTENCE if index == 0 else W_POS_BODY


def _strong_signal_evidence(
    signal: Signal, title: str, abstract_sentences: Sequence[str]
) -> Optional[float]:
    """某条强信号在本文中的**最佳**位置权重（0 表示未命中）。

    逐处扫描（标题 + 每个摘要句子），取「位置权重 × incidental 折扣」的最大值。
    用最大值而不是求和：同一信号在摘要里出现三次不该比出现在标题一次更重要。
    """
    best = 0.0

    # 标题：满权重，且不看 incidental（标题里出现就是研究对象）
    if _signal_hit(signal, title, [title]):
        best = max(best, W_POS_TITLE)

    for index, sentence in enumerate(abstract_sentences):
        if not _signal_hit(signal, sentence, [sentence]):
            continue
        weight = _position_weight(index)
        if _is_incidental(sentence):
            weight *= W_INCIDENTAL
        best = max(best, weight)
    return best if best > 0 else None


def _incidental_names(
    signals: Sequence[Signal], abstract_sentences: Sequence[str]
) -> List[str]:
    """记录哪些信号只出现在 incidental 语境里（供日志说明）。"""
    names: List[str] = []
    for signal in signals:
        for sentence in abstract_sentences:
            if _signal_hit(signal, sentence, [sentence]) and _is_incidental(sentence):
                names.append(signal.name)
                break
    return names


def score_text(
    title: str,
    abstract: str = "",
    journal: str = "",
    require_cardiac_context: bool = False,
) -> RelevanceResult:
    """对「标题 + 摘要 + 期刊」打分，返回 :class:`RelevanceResult`。

    打分由四部分构成：
      1. 强信号 —— 位置加权（标题 1.0 / 首句 0.8 / 其余 0.35）× incidental 折扣 × 递减累加
      2. 半强信号（猝死遗传检测、心电成像）
      3. 中性信号 + 心脏语境
      4. 负信号扣分

    另有两条**短路规则**（最稳、误伤最小）：
      - 标题含非心脏解剖部位且无任何心脏/电生理词 → 直接判无关（score=0）
      - ``require_cardiac_context=True`` 时，全文若不含任何心脏语境词 → 直接判无关（score=0）

    关于 ``require_cardiac_context``：它是**可关闭的硬门槛**，不是评分项。
    单纯靠锚定不足以判相关（实测 34→26，最差几条全留下），但作为前置门槛
    可以挡住「通篇没有心脏语境却有强信号词」的漏网（如器械/材料学论文）。
    默认关闭，由 config.yaml 的 ``relevance.require_cardiac_context`` 控制，
    **主题词分支**才生效；期刊白名单直通的论文不受影响。
    """
    title = title or ""
    abstract = abstract or ""
    journal = journal or ""
    abstract_sentences = _split_sentences(abstract)

    # --- 短路规则 1：标题就是研究对象 ---
    if NONCARDIAC_SUBJECT.search(title) and not CARDIAC_TITLE.search(title):
        return RelevanceResult(score=0.0, subject_mismatch=True)

    haystack = f"{title} {abstract} {journal}"
    all_sentences = _split_sentences(haystack)
    has_context = bool(CARDIAC_CONTEXT.search(haystack))

    # --- 短路规则 2：心脏语境硬门槛（可关闭）---
    if require_cardiac_context and not has_context:
        return RelevanceResult(
            score=0.0,
            has_cardiac_context=False,
            missing_cardiac_context=True,
        )

    # --- 强信号（位置 + incidental 加权）---
    weighted: Dict[str, float] = {}
    for signal in STRONG_SIGNALS:
        evidence = _strong_signal_evidence(signal, title, abstract_sentences)
        if evidence is not None:
            weighted[signal.name] = evidence

    # 按证据强度降序，套用递减系数（饱和累加，避免两条 incidental 顶格）
    strong_total = 0.0
    for rank, (name, evidence) in enumerate(
        sorted(weighted.items(), key=lambda kv: kv[1], reverse=True)
    ):
        if rank < len(W_STRONG_DECAY):
            decay = W_STRONG_DECAY[rank]
        else:
            decay = W_STRONG_DECAY[-1] * 0.6
        strong_total += W_STRONG_BASE * evidence * decay
        weighted[name] = round(W_STRONG_BASE * evidence * decay, 4)

    genetic = [s.name for s in GENETIC_DEATH_SIGNALS if _signal_hit(s, haystack, all_sentences)]
    # 心电成像类只在**标题**里找：摘要里偶然提到 ECG 不足以说明研究主题
    ecg_imaging = [s.name for s in ECG_IMAGING_SIGNALS if _signal_hit(s, title, [title])]
    neutral = [s.name for s in NEUTRAL_SIGNALS if _signal_hit(s, haystack, all_sentences)]
    negative = [s.name for s in NEGATIVE_SIGNALS + EXTRA_NEGATIVE_SIGNALS
                if _signal_hit(s, haystack, all_sentences)]
    # 合并症/研究类型类负信号：标题已声明 EP 诊断时不再扣分（见 CONDITIONAL_NEGATIVE_NAMES）
    if TITLE_EP_DIAGNOSIS.search(title):
        negative = [n for n in negative if n not in CONDITIONAL_NEGATIVE_NAMES]
    # 条件负信号：仅在标题无 EP 诊断词时生效（见 _conditional_negative_hits）
    negative += _conditional_negative_hits(title, haystack, all_sentences)

    score = strong_total
    score += W_GENETIC_EACH * len(genetic)
    score += W_GENETIC_EACH * len(ecg_imaging)
    score += W_NEUTRAL_EACH * len(neutral)
    if has_context:
        score += W_CONTEXT
    for name in negative:
        score -= W_NEG_STRONG if name in SEVERE_NEGATIVE else W_NEG_EACH

    score = max(0.0, min(1.0, score))
    return RelevanceResult(
        score=round(score, 4),
        strong=sorted(weighted, key=lambda k: weighted[k], reverse=True) + genetic + ecg_imaging,
        neutral=neutral,
        negative=negative,
        has_cardiac_context=has_context,
        weighted=weighted,
        incidental=_incidental_names(STRONG_SIGNALS, abstract_sentences),
    )


def score_paper(paper: Paper, require_cardiac_context: bool = False) -> RelevanceResult:
    """对 :class:`Paper` 打分。"""
    return score_text(
        paper.title, paper.summary, paper.journal, require_cardiac_context=require_cardiac_context
    )


def is_relevant(
    paper: Paper,
    threshold: float = DEFAULT_THRESHOLD,
    require_cardiac_context: bool = False,
) -> bool:
    """是否达到相关性阈值。"""
    return score_paper(paper, require_cardiac_context=require_cardiac_context).score >= threshold


# ---------------------------------------------------------------------------
# 批量过滤（供 collect 调用）
# ---------------------------------------------------------------------------

@dataclass
class FilterStats:
    """过滤统计，用于日志与验收报告。"""

    before: int = 0
    after: int = 0
    dropped: List[Tuple[str, float, str]] = field(default_factory=list)
    kept_by_journal: int = 0

    @property
    def dropped_count(self) -> int:
        return len(self.dropped)

    def summary(self) -> str:
        return (
            f"相关性过滤：{self.before} 篇 → {self.after} 篇"
            f"（丢弃 {self.dropped_count} 篇，其中白名单期刊直通 {self.kept_by_journal} 篇）"
        )


def filter_papers(
    papers: Iterable[Paper],
    threshold: float = DEFAULT_THRESHOLD,
    journals: Optional[Iterable[str]] = None,
    enabled: bool = True,
    require_cardiac_context: bool = False,
) -> Tuple[List[Paper], FilterStats]:
    """按相关性过滤论文。

    ⚠️ **期刊白名单直通**：来自白名单期刊的论文不做评分过滤 ——
    EP 专业期刊里的文献本身就是目标领域（即使是基础/影像子方向）。
    本次采集白名单恰为 0 篇，但这是常态保障，必须保留。

    ``require_cardiac_context=True`` 时，非白名单的论文必须含心脏语境词才可能保留
    （见 :func:`score_text`）。
    """
    papers = list(papers)
    stats = FilterStats(before=len(papers))
    if not enabled:
        stats.after = len(papers)
        return papers, stats

    whitelist = [j.strip().lower() for j in (journals or []) if j and j.strip()]

    kept: List[Paper] = []
    for paper in papers:
        journal_lower = (paper.journal or "").strip().lower()
        if whitelist and any(_journal_matches(journal_lower, w) for w in whitelist):
            stats.kept_by_journal += 1
            kept.append(paper)
            continue

        result = score_paper(paper, require_cardiac_context=require_cardiac_context)
        if result.score >= threshold:
            kept.append(paper)
        else:
            stats.dropped.append((paper.title, result.score, result.reason))

    stats.after = len(kept)
    for title, score, reason in stats.dropped:
        logger.info("丢弃(相关性 %.2f): %s  ← %s", score, title[:80], reason)
    logger.info("%s", stats.summary())
    return kept, stats


def _journal_matches(journal: str, whitelist_entry: str) -> bool:
    """期刊名匹配（白名单里是缩写，ISOAbbreviation 可能大小写不同）。"""
    if not journal:
        return False
    return journal == whitelist_entry or journal.replace(".", "").replace(" ", "") == (
        whitelist_entry.replace(".", "").replace(" ", "")
    )
