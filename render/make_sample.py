"""生成虚构样例数据 + 样例日报页面（仅用于自测 / 预览，不是生产入口）。

用法::

    python -m render.make_sample

产物：
  render/sample/2026-10-19_AI_enhanced_Chinese.jsonl  虚构样例数据
  render/sample/index.html                            主页面
  render/sample/archive/2026-10-19.html               归档页

样例刻意覆盖四种降级/边界路径：
  - 完整数据（有 AI、有摘要、多链接）
  - 无 AI 解读（只有摘要）
  - no_abstract=True（暂无摘要）
  - 未知 topic（归入「其他」）+ 含特殊字符的标题（验证转义不影响可读性）
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from render.build_html import build_pages  # noqa: E402

SAMPLE_DIR = Path(__file__).resolve().parent / "sample"
SAMPLE_DATE = "2026-10-19"


def _ai(tldr, motivation, method, result, conclusion):
    return {
        "tldr": tldr,
        "motivation": motivation,
        "method": method,
        "result": result,
        "conclusion": conclusion,
    }


SAMPLE_PAPERS = [
    # ---------------- 房颤与消融 ----------------
    {
        "id": "pmid-41100001",
        "title": "Pulsed Field Ablation Versus Cryoballoon Ablation for Paroxysmal Atrial Fibrillation: "
                 "A Multicenter Randomized Trial",
        "authors": ["Chen Yuxin", "Wang Hao", "Martinez L", "Kobayashi T", "Schmidt A"],
        "summary": "BACKGROUND: Pulsed field ablation (PFA) has emerged as a non-thermal modality for "
                   "pulmonary vein isolation, but head-to-head randomized data against cryoballoon "
                   "ablation remain limited. METHODS: In this multicenter, randomized, non-inferiority "
                   "trial, 482 patients with symptomatic paroxysmal atrial fibrillation were assigned to "
                   "PFA or cryoballoon ablation and followed for 12 months with 7-day Holter monitoring. "
                   "The primary endpoint was freedom from atrial arrhythmia recurrence. RESULTS: Freedom "
                   "from recurrence at 12 months was 79.4% with PFA and 74.1% with cryoballoon (difference "
                   "5.3 percentage points; 95% CI, 1.2 to 9.4). Procedure time was shorter with PFA "
                   "(52 vs 74 minutes; P<0.001). Phrenic nerve palsy occurred in 0.4% of PFA patients "
                   "versus 3.1% of cryoballoon patients. CONCLUSIONS: PFA was non-inferior and reduced "
                   "phrenic nerve injury compared with cryoballoon ablation.",
        "url": "https://pubmed.ncbi.nlm.nih.gov/41100001/",
        "abs": "https://pubmed.ncbi.nlm.nih.gov/41100001/",
        "pdf": "https://europepmc.org/articles/PMC41100001",
        "date": "2026-10-19",
        "categories": ["房颤与消融"],
        "category": "房颤与消融",
        "source": "pubmed",
        "journal": "Heart Rhythm",
        "doi": "10.1016/j.hrthm.2026.10.014",
        "pmid": "41100001",
        "pub_types": ["Randomized Controlled Trial", "Multicenter Study"],
        "is_open_access": True,
        "topic": "房颤与消融",
        "no_abstract": False,
        "AI": _ai(
            "在阵发性房颤中，脉冲场消融 12 个月无复发率优于冷冻球囊，且膈神经损伤更少。",
            "脉冲场消融作为非热能方式近年快速普及，但缺乏与冷冻球囊的头对头随机对照证据，"
            "临床上选择术式时缺少高等级依据。",
            "多中心随机非劣效试验，纳入 482 例症状性阵发性房颤患者，随机接受脉冲场消融或冷冻球囊消融，"
            "随访 12 个月并以 7 天动态心电图评估复发，主要终点为无房性心律失常复发。",
            "12 个月无复发率脉冲场消融 79.4%、冷冻球囊 74.1%（差值 5.3 个百分点，95% CI 1.2–9.4）；"
            "手术时间更短（52 分钟 vs 74 分钟，P<0.001），膈神经麻痹发生率 0.4% vs 3.1%。",
            "脉冲场消融不劣于并可能优于冷冻球囊，安全性方面显著降低膈神经损伤，"
            "支持其作为阵发性房颤一线消融方式。",
        ),
    },
    {
        "id": "pmid-41100002",
        "title": "Left Atrial Appendage Closure Versus Direct Oral Anticoagulation in Patients "
                 "With Prior Intracranial Hemorrhage",
        "authors": ["Novak P", "Rahman S", "de Vries J"],
        "summary": "OBJECTIVES: To compare percutaneous left atrial appendage closure with direct oral "
                   "anticoagulation in patients with atrial fibrillation and a history of intracranial "
                   "hemorrhage. METHODS: Observational cohort of 1,204 patients, propensity-score matched. "
                   "RESULTS: Over a median follow-up of 3.2 years, the composite of stroke, systemic "
                   "embolism, and major bleeding was lower with appendage closure (HR 0.71; 95% CI "
                   "0.55-0.92). Major bleeding alone was halved. CONCLUSIONS: In this high-bleeding-risk "
                   "population, appendage closure was associated with better net clinical outcomes.",
        "url": "https://pubmed.ncbi.nlm.nih.gov/41100002/",
        "abs": "https://pubmed.ncbi.nlm.nih.gov/41100002/",
        "pdf": "",
        "date": "2026-10-19",
        "categories": ["房颤与消融"],
        "category": "房颤与消融",
        "source": "pubmed",
        "journal": "JACC Clin Electrophysiol",
        "doi": "10.1016/j.jacep.2026.09.021",
        "pmid": "41100002",
        "pub_types": ["Observational Study"],
        "is_open_access": False,
        "topic": "房颤与消融",
        "no_abstract": False,
        "AI": _ai(
            "有颅内出血史的房颤患者中，左心耳封堵较口服抗凝药净临床获益更好。",
            "此类患者长期被视为抗凝禁忌，但缺血性卒中风险仍在，需要比较封堵与继续抗凝的取舍。",
            "观察性队列共 1204 例，采用倾向性评分匹配，主要终点为卒中、体循环栓塞与大出血的复合终点。",
            "中位随访 3.2 年，封堵组复合终点更低（HR 0.71，95% CI 0.55–0.92），单看大出血降至约一半。",
            "在高出血风险人群中，左心耳封堵可能优于继续口服抗凝，但观察性设计仍需随机试验确认。",
        ),
    },
    {
        "id": "pmid-41100003",
        "title": "Catheter Ablation of Atrial Fibrillation in Heart Failure With Preserved Ejection "
                 "Fraction: 24-Month Outcomes",
        "authors": ["Ahmed K", "Liu X"],
        "summary": "Aims: The benefit of catheter ablation in heart failure with preserved ejection "
                   "fraction (HFpEF) is uncertain. Methods and results: We enrolled 268 patients with "
                   "AF and HFpEF. At 24 months, the ablation group had lower AF burden and fewer heart "
                   "failure hospitalizations compared with medical therapy.",
        "url": "https://pubmed.ncbi.nlm.nih.gov/41100003/",
        "abs": "https://pubmed.ncbi.nlm.nih.gov/41100003/",
        "pdf": "https://europepmc.org/articles/PMC41100003",
        "date": "2026-10-19",
        "categories": ["房颤与消融"],
        "category": "房颤与消融",
        "source": "europepmc",
        "journal": "Europace",
        "doi": "10.1093/europace/euag2026.118",
        "pmid": "41100003",
        "pub_types": ["Clinical Trial"],
        "is_open_access": True,
        "topic": "房颤与消融",
        "no_abstract": False,
        "AI": None,  # 无 AI 解读：降级只显示摘要
    },

    # ---------------- 室性心律失常 ----------------
    {
        "id": "pmid-41100004",
        "title": "Substrate-Based Ablation of Ventricular Tachycardia in Non-Ischemic "
                 "Cardiomyopathy: Long-Term Freedom From Recurrence",
        "authors": ["Rossi G", "Tanaka H", "Okafor C", "Bergström E"],
        "summary": "BACKGROUND: Catheter ablation of ventricular tachycardia (VT) in non-ischemic "
                   "cardiomyopathy remains challenging because of intramural and epicardial substrates. "
                   "METHODS: 197 consecutive patients underwent substrate mapping with combined "
                   "endocardial and epicardial access. RESULTS: Complete substrate modification was "
                   "achieved in 84%. At 36 months, freedom from VT recurrence was 68% in the complete "
                   "modification group versus 41% in the incomplete group (P<0.001). Major complications "
                   "occurred in 6.6%. CONCLUSIONS: Complete substrate modification predicts durable "
                   "VT control and should be the procedural target.",
        "url": "https://pubmed.ncbi.nlm.nih.gov/41100004/",
        "abs": "https://pubmed.ncbi.nlm.nih.gov/41100004/",
        "pdf": "",
        "date": "2026-10-19",
        "categories": ["室性心律失常"],
        "category": "室性心律失常",
        "source": "pubmed",
        "journal": "Circ Arrhythm Electrophysiol",
        "doi": "10.1161/CIRCEP.126.013402",
        "pmid": "41100004",
        "pub_types": ["Journal Article"],
        "is_open_access": False,
        "topic": "室性心律失常",
        "no_abstract": False,
        "AI": _ai(
            "非缺血性心肌病室速消融中，完整基质改良是长期不复发的独立预测因素。",
            "非缺血性心肌病室速常累及心肌内与心外膜基质，单纯心内膜消融复发率高，需要明确术式目标。",
            "197 例连续患者接受心内膜联合心外膜基质标测与消融，按基质改良是否完整分组随访 36 个月。",
            "84% 实现完整基质改良；36 个月无室速复发率完整组 68%、不完整组 41%（P<0.001），"
            "主要并发症 6.6%。",
            "完整基质改良应作为手术目标，但需权衡心外膜入路带来的并发症风险。",
        ),
    },
    {
        "id": "pmid-41100005",
        "title": "Genetic Architecture of Brugada Syndrome in a Large Multinational Cohort",
        "authors": ["Silva M", "Yamamoto R", "Petrov D"],
        "summary": "Brugada syndrome is associated with loss-of-function variants in SCN5A, yet the "
                   "genetic yield in unselected cohorts is modest. We sequenced 3,412 probands and "
                   "identified pathogenic or likely pathogenic variants in 21.4%, with SCN5A accounting "
                   "for the majority. Rare variants in additional genes implicated calcium handling.",
        "url": "https://pubmed.ncbi.nlm.nih.gov/41100005/",
        "abs": "https://pubmed.ncbi.nlm.nih.gov/41100005/",
        "pdf": "",
        "date": "2026-10-19",
        "categories": ["室性心律失常"],
        "category": "室性心律失常",
        "source": "pubmed",
        "journal": "Heart Rhythm",
        "doi": "10.1016/j.hrthm.2026.10.022",
        "pmid": "41100005",
        "pub_types": ["Journal Article"],
        "is_open_access": False,
        "topic": "室性心律失常",
        "no_abstract": False,
        "AI": None,
    },

    # ---------------- 器械治疗 ----------------
    {
        "id": "pmid-41100006",
        "title": "Leadless Pacemaker Versus Transvenous Pacing: 5-Year Device-Related "
                 "Complication Rates",
        "authors": ["Dubois A", "Nguyen T", "Kim S", "Oyelaran B"],
        "summary": "BACKGROUND: Leadless pacing eliminates the transvenous lead and subcutaneous pocket "
                   "but long-term comparative data are sparse. METHODS: We compared 8,930 patients "
                   "receiving a leadless pacemaker with 26,790 receiving transvenous systems using a "
                   "national registry. RESULTS: Device-related complications at 5 years were 9.1% versus "
                   "16.3% (HR 0.58; 95% CI 0.52-0.65). Cardiac perforation was numerically higher with "
                   "leadless devices in the first 30 days. CONCLUSIONS: Leadless pacing was associated "
                   "with fewer long-term complications, with a small early perforation signal requiring "
                   "operator vigilance.",
        "url": "https://pubmed.ncbi.nlm.nih.gov/41100006/",
        "abs": "https://pubmed.ncbi.nlm.nih.gov/41100006/",
        "pdf": "https://europepmc.org/articles/PMC41100006",
        "date": "2026-10-19",
        "categories": ["器械治疗"],
        "category": "器械治疗",
        "source": "pubmed",
        "journal": "Europace",
        "doi": "10.1093/europace/euag2026.204",
        "pmid": "41100006",
        "pub_types": ["Observational Study"],
        "is_open_access": True,
        "topic": "器械治疗",
        "no_abstract": False,
        "AI": _ai(
            "无导线起搏器 5 年器械相关并发症显著低于经静脉起搏，但早期穿孔信号需警惕。",
            "无导线起搏避免了经静脉导线与皮下囊袋，理论上并发症更少，但缺乏长期大规模对比数据。",
            "基于全国性注册登记，比较 8930 例无导线起搏与 26790 例经静脉起搏患者，随访 5 年。",
            "5 年器械相关并发症 9.1% vs 16.3%（HR 0.58，95% CI 0.52–0.65）；"
            "无导线组前 30 天心脏穿孔数值上更高。",
            "长期安全性支持无导线起搏，但术者需重视早期穿孔风险与操作学习曲线。",
        ),
    },
    {
        "id": "pmid-41100007",
        "title": "Subcutaneous Implantable Cardioverter-Defibrillator in Patients With "
                 "End-Stage Renal Disease",
        "authors": ["Ferrari L", "Abebe Y"],
        "summary": "Patients with end-stage renal disease are at high risk of sudden cardiac death but "
                   "have elevated infection risk with transvenous defibrillators. In this cohort of 412 "
                   "patients, the subcutaneous ICD was implanted without systemic infection at 2 years "
                   "in 96.6%, and appropriate therapy occurred in 18%.",
        "url": "https://pubmed.ncbi.nlm.nih.gov/41100007/",
        "abs": "https://pubmed.ncbi.nlm.nih.gov/41100007/",
        "pdf": "",
        "date": "2026-10-18",
        "categories": ["器械治疗"],
        "category": "器械治疗",
        "source": "pubmed",
        "journal": "J Cardiovasc Electrophysiol",
        "doi": "10.1111/jce.16234",
        "pmid": "41100007",
        "pub_types": ["Journal Article"],
        "is_open_access": False,
        "topic": "器械治疗",
        "no_abstract": False,
        "AI": _ai(
            "终末期肾病患者植入皮下 ICD 2 年无全身感染率高，可作为经静脉 ICD 的替代。",
            "该人群猝死风险高，但经静脉 ICD 的感染与导线相关并发症风险显著升高。",
            "412 例终末期肾病患者的单臂队列研究，随访 2 年评估感染率与恰当治疗发生率。",
            "2 年无全身感染率 96.6%，18% 患者接受了恰当治疗。",
            "皮下 ICD 在这一高感染风险人群中显示出可接受的安全性，值得进一步随机验证。",
        ),
    },
    {
        "id": "pmid-41100008",
        "title": "Cardiac Resynchronization Therapy Response Prediction Using Machine Learning",
        "authors": ["Hoffmann K", "Zhou L"],
        "summary": "",
        "url": "https://pubmed.ncbi.nlm.nih.gov/41100008/",
        "abs": "https://pubmed.ncbi.nlm.nih.gov/41100008/",
        "pdf": "",
        "date": "2026-10-18",
        "categories": ["器械治疗"],
        "category": "器械治疗",
        "source": "pubmed",
        "journal": "Pacing Clin Electrophysiol",
        "doi": "10.1111/pace.15201",
        "pmid": "41100008",
        "pub_types": ["Editorial"],
        "is_open_access": False,
        "topic": "器械治疗",
        "no_abstract": True,  # 无摘要：显示「暂无摘要」
        "AI": None,
    },

    # ---------------- 影像与标测 ----------------
    {
        "id": "pmid-41100009",
        "title": "Late Gadolinium Enhancement-Derived Substrate Maps Predict Ventricular "
                 "Tachycardia Circuits Better Than Voltage Maps",
        "authors": ["Ibrahim N", "Sørensen M", "Park J"],
        "summary": "BACKGROUND: Identifying critical isthmuses for ventricular tachycardia ablation is "
                   "central to procedural success. METHODS: We compared cardiac magnetic resonance-derived "
                   "late gadolinium enhancement (LGE) substrate maps with invasive voltage maps in 88 "
                   "patients. RESULTS: LGE-derived maps identified 94% of critical isthmuses confirmed by "
                   "entrainment mapping, compared with 71% for voltage maps. CONCLUSIONS: Pre-procedural "
                   "LGE imaging improves isthmus targeting and may shorten mapping time.",
        "url": "https://pubmed.ncbi.nlm.nih.gov/41100009/",
        "abs": "https://pubmed.ncbi.nlm.nih.gov/41100009/",
        "pdf": "https://europepmc.org/articles/PMC41100009",
        "date": "2026-10-19",
        "categories": ["影像与标测"],
        "category": "影像与标测",
        "source": "pubmed",
        "journal": "JACC Clin Electrophysiol",
        "doi": "10.1016/j.jacep.2026.08.017",
        "pmid": "41100009",
        "pub_types": ["Journal Article"],
        "is_open_access": True,
        "topic": "影像与标测",
        "no_abstract": False,
        "AI": _ai(
            "术前 LGE 磁共振基质图识别室速关键峡部的能力优于术中电压图。",
            "关键峡部定位决定室速消融成败，术中电压图受血流与接触影响，可能漏判深部基质。",
            "88 例患者对比磁共振延迟强化（LGE）基质图与有创电压图，以拖带标测确认的峡部为金标准。",
            "LGE 图识别出 94% 的关键峡部，电压图为 71%。",
            "术前 LGE 成像可提升靶点定位准确性并可能缩短标测时间，支持影像引导的消融策略。",
        ),
    },
    {
        "id": "pmid-41100010",
        "title": "Intracardiac Echocardiography-Guided Versus Fluoroscopy-Guided "
                 "Transseptal Puncture: A Meta-Analysis",
        "authors": ["Kaur P", "Bianchi F"],
        "summary": "We pooled 14 studies comprising 5,921 patients. Intracardiac echocardiography "
                   "guidance was associated with shorter fluoroscopy time and a lower rate of "
                   "procedure-related complications, with no difference in success rate.",
        "url": "https://pubmed.ncbi.nlm.nih.gov/41100010/",
        "abs": "https://pubmed.ncbi.nlm.nih.gov/41100010/",
        "pdf": "",
        "date": "2026-10-19",
        "categories": ["影像与标测"],
        "category": "影像与标测",
        "source": "pubmed",
        "journal": "J Interv Card Electrophysiol",
        "doi": "10.1007/s10840-026-01987-3",
        "pmid": "41100010",
        "pub_types": ["Meta-Analysis", "Review"],
        "is_open_access": False,
        "topic": "影像与标测",
        "no_abstract": False,
        "AI": None,
    },

    # ---------------- 遗传与基础 ----------------
    {
        "id": "pmid-41100011",
        "title": "Optogenetic Termination of Reentrant Ventricular Arrhythmias in "
                 "Human-Induced Pluripotent Stem Cell-Derived Cardiac Tissue",
        "authors": ["Lindqvist A", "Costa R", "Xu M"],
        "summary": "Optogenetics offers millisecond-scale control of cardiomyocyte membrane potential. "
                   "Using human induced pluripotent stem cell-derived cardiac sheets, we demonstrate "
                   "that brief red-light pulses terminate reentrant activity in 92% of episodes, with "
                   "lower energy requirements than electrical defibrillation.",
        "url": "https://pubmed.ncbi.nlm.nih.gov/41100011/",
        "abs": "https://pubmed.ncbi.nlm.nih.gov/41100011/",
        "pdf": "https://europepmc.org/articles/PMC41100011",
        "date": "2026-10-19",
        "categories": ["遗传与基础"],
        "category": "遗传与基础",
        "source": "pubmed",
        "journal": "Circ Arrhythm Electrophysiol",
        "doi": "10.1161/CIRCEP.126.014001",
        "pmid": "41100011",
        "pub_types": ["Journal Article"],
        "is_open_access": True,
        "topic": "遗传与基础",
        "no_abstract": False,
        "AI": _ai(
            "在人心肌细胞片中，光遗传学红光脉冲可终止 92% 的折返性心律失常。",
            "电除颤能量高且造成心肌损伤，寻找低能量、精准终止折返的方式是重要方向。",
            "以人诱导多能干细胞来源的心肌片为模型，施加毫秒级红光脉冲诱导终止折返，并与电除颤比较能量需求。",
            "92% 的折返事件被红光脉冲终止，所需能量低于电除颤。",
            "光遗传学为低能量终止室性心律失常提供概念验证，但距临床应用仍有基因递送等障碍。",
        ),
    },
    {
        "id": "pmid-41100012",
        "title": "Ion Channel Remodeling in Chronic Atrial Tachypacing: Implications for "
                 "Antiarrhythmic Drug Response",
        "authors": ["Nakamura S", "Bauer T"],
        "summary": "",
        "url": "https://pubmed.ncbi.nlm.nih.gov/41100012/",
        "abs": "https://pubmed.ncbi.nlm.nih.gov/41100012/",
        "pdf": "",
        "date": "2026-10-17",
        "categories": ["遗传与基础"],
        "category": "遗传与基础",
        "source": "pubmed",
        "journal": "Heart Rhythm O2",
        "doi": "10.1016/j.hroo.2026.09.009",
        "pmid": "41100012",
        "pub_types": ["Comment"],
        "is_open_access": False,
        "topic": "遗传与基础",
        "no_abstract": True,
        "AI": None,
    },

    # ---------------- 其他（含未知 topic 兜底 + 特殊字符转义验证） ----------------
    {
        "id": "pmid-41100013",
        "title": "Wearable Electrocardiogram Screening for Atrial Fibrillation: "
                 "The \"P-Wave\" Challenge & Cost-Effectiveness",
        "authors": ["O'Brien J", "Håkansson E", "Villanueva M"],
        "summary": "Consumer wearable devices detect atrial fibrillation with high sensitivity but "
                   "generate frequent false positives. This analysis of 22,480 participants examines "
                   "cost-effectiveness of confirmatory pathways. P-wave visibility was the strongest "
                   "predictor of true positive rhythm (OR 3.4; P<0.001).",
        "url": "https://pubmed.ncbi.nlm.nih.gov/41100013/",
        "abs": "https://pubmed.ncbi.nlm.nih.gov/41100013/",
        "pdf": "",
        "date": "2026-10-19",
        "categories": ["其他"],
        "category": "其他",
        "source": "pubmed",
        "journal": "Indian Pacing Electrophysiol J",
        "doi": "10.1016/j.ipej.2026.09.004",
        "pmid": "41100013",
        "pub_types": ["Journal Article"],
        "is_open_access": False,
        "topic": "其他",
        "no_abstract": False,
        "AI": _ai(
            "可穿戴心电筛查房颤灵敏度高但假阳性多，P 波可见性是真正的阳性预测因素。",
            "可穿戴设备普及带来大量疑似房颤告警，确认路径的成本效益尚不明确。",
            "分析 22480 名参与者，评估确认性检查路径的成本效益，并建模真阳性节律的预测因素。",
            "P 波可见性是真阳性节律最强预测因素（OR 3.4；P<0.001）。",
            "筛查流程应结合 P 波判读以降低假阳性带来的后续成本，不建议仅凭设备告警启动抗凝决策。",
        ),
    },
    {
        "id": "pmid-41100014",
        "title": "Supraventricular Tachycardia in Pregnancy: A Practical Management Review",
        "authors": ["Almeida C", "Sharma V"],
        "summary": "This review summarizes contemporary management of supraventricular tachycardia "
                   "during pregnancy, covering acute termination strategies with vagal maneuvers and "
                   "adenosine, and the safety profile of long-term antiarrhythmic therapy in pregnancy "
                   "and lactation.",
        "url": "https://pubmed.ncbi.nlm.nih.gov/41100014/",
        "abs": "https://pubmed.ncbi.nlm.nih.gov/41100014/",
        "pdf": "https://europepmc.org/articles/PMC41100014",
        "date": "2026-10-19",
        "categories": ["其他"],
        "category": "其他",
        "source": "pubmed",
        "journal": "Indian Pacing Electrophysiol J",
        "doi": "10.1016/j.ipej.2026.09.011",
        "pmid": "41100014",
        "pub_types": ["Review"],
        "is_open_access": True,
        "topic": "其他",
        "no_abstract": False,
        "AI": None,
    },
]


def main() -> int:
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    jsonl = SAMPLE_DIR / f"{SAMPLE_DATE}_AI_enhanced_Chinese.jsonl"
    with open(jsonl, "w", encoding="utf-8") as fh:
        for paper in SAMPLE_PAPERS:
            fh.write(json.dumps(paper, ensure_ascii=False) + "\n")

    build_pages(
        jsonl,
        SAMPLE_DIR / "index.html",
        date=SAMPLE_DATE,
        generated_at=datetime(2026, 10, 19, 7, 30),
    )
    print(f"[sample] 数据 -> {jsonl}")
    print(f"[sample] 主页面 -> {SAMPLE_DIR / 'index.html'}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
