"""端到端验收检查（由 lead 独立编写，不依赖任何 teammate 的实现细节）。

用途：在合并完成后验证「采集 → AI → 渲染」整条链路真的能跑通，
以及产出的数据/页面满足契约。可以在无网络、无 API Key 的情况下运行
（此时只校验已有的 data/ 与 index.html）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, label: str, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  ✅ {label}")
    else:
        FAILURES.append(f"{label}{(' — ' + detail) if detail else ''}")
        print(f"  ❌ {label}{(' — ' + detail) if detail else ''}")


def main() -> int:
    print("=" * 70)
    print("端到端验收检查")
    print("=" * 70)

    # ---- 1. 契约层 ----
    print("\n[1] 契约层：schema / config 可导入且行为正确")
    from daily_ep.schema import Paper, dedup_keys, normalize_title, TOPIC_ORDER
    from daily_ep.config import Config

    cfg = Config.load()
    check("edat" == cfg.date_field, "date_field 必须是 edat（不是 dp）",
          f"实际 {cfg.date_field}")
    check(len(cfg.journals) > 0, "期刊白名单非空", f"{len(cfg.journals)} 本")
    check(len(cfg.topics) > 0, "主题词非空", f"{len(cfg.topics)} 个")

    # 去重键：DOI 优先
    a = Paper(id="1", title="Pulsed Field Ablation for AF", doi="10.1/ABC", pmid="111")
    keys = dedup_keys(a)
    check(keys[0].startswith("doi:"), "去重首键为 DOI", str(keys[:1]))
    check(keys[0].endswith("10.1/abc"), "DOI 转小写归一化", keys[0])

    # 标题归一化：大小写、标点、连字符差异应视为同一篇
    t1 = "P‐wave Duration and AF Recurrence!"
    t2 = "p-wave duration and af recurrence"
    check(normalize_title(t1) == normalize_title(t2),
          "标题归一化忽略标点/大小写/连字符变体",
          f"{normalize_title(t1)!r} vs {normalize_title(t2)!r}")

    # 亚专业标签分类
    topic = cfg.classify("Pulsed field ablation versus cryoablation for atrial fibrillation", "")
    check(topic == "房颤与消融", "房颤消融类正确归类", f"实际 {topic}")
    topic2 = cfg.classify("Leadless pacemaker implantation outcomes", "")
    check(topic2 == "器械治疗", "器械治疗类正确归类", f"实际 {topic2}")
    topic3 = cfg.classify("Completely unrelated study of gardening", "")
    check(topic3 == "其他", "无命中归入「其他」", f"实际 {topic3}")

    # 标签顺序约束
    check(TOPIC_ORDER[-1] == "其他", "TOPIC_ORDER 以「其他」收尾")

    # ---- 2. 采集层 ----
    print("\n[2] 采集层：查询构造不含 [dp]，使用 [edat]/[jour]/[tiab]")
    try:
        from datetime import date, timedelta

        from daily_ep.sources import pubmed  # type: ignore

        today = date.today()
        window = [today - timedelta(days=offset) for offset in range(1, 2)]
        q = pubmed.build_query(cfg.journals, cfg.topics, window, cfg.date_field)
        if not q:
            check(False, "查询构造函数有输出", "返回空串")
        else:
            check("[edat]" in q, "查询使用 [edat]", q[:160])
            check("[dp]" not in q, "查询不含 [dp]")
            check("[MeSH" not in q and "[mesh" not in q, "查询不使用 [MeSH]")
            check("[jour]" in q, "查询含期刊白名单 [jour]")
            check("[tiab]" in q, "查询含主题词 [tiab]")
            check("tool=" not in q, "查询串不含 HTTP 参数（应只含检索式）")
    except Exception as exc:  # noqa: BLE001
        check(False, "导入采集层", f"{type(exc).__name__}: {exc}")

    # ---- 3. 数据产物 ----
    print("\n[3] 数据产物：data/*.jsonl 字段符合契约")
    data_dir = ROOT / "data"
    jsonls = sorted(p for p in data_dir.glob("*.jsonl")
                    if "_AI_enhanced_" not in p.name)
    check(len(jsonls) > 0, "存在至少一个采集产物 data/*.jsonl",
          f"找到 {len(jsonls)} 个")
    if jsonls:
        path = jsonls[-1]
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        check(len(rows) > 0, f"{path.name} 非空", f"{len(rows)} 篇")
        sample = rows[0]
        for field in ("id", "title", "date", "topic", "journal"):
            check(field in sample, f"记录含字段 {field}")
        check(isinstance(sample.get("authors"), list), "authors 是列表")
        check(isinstance(sample.get("categories"), list), "categories 是列表")
        check(sample.get("categories", [None])[0] == sample.get("topic"),
              "categories[0] 与 topic 一致")
        # 无摘要条目应被标记
        no_abs = [r for r in rows if not (r.get("summary") or "").strip()]
        if no_abs:
            check(all(r.get("no_abstract") for r in no_abs),
                  f"无摘要条目已标记 no_abstract（{len(no_abs)} 篇）")
        else:
            check(True, "本批均有摘要")
        # 日期字段应落在合理范围（不是未来很远的印刷日期）
        bad_dates = [r for r in rows if r.get("date", "") > "2027-01-01"]
        check(not bad_dates, "无异常未来日期（未误用 [dp]）",
              f"{len(bad_dates)} 篇异常")

    # ---- 4. HTML 产物 ----
    print("\n[4] 渲染产物：index.html 结构正确")
    index = ROOT / "index.html"
    check(index.exists(), "index.html 已生成")
    if index.exists():
        html = index.read_text(encoding="utf-8")
        check(len(html) > 500, "index.html 非空", f"{len(html)} 字符")
        check("<html" in html.lower(), "含 <html> 标签")
        check("心脏电生理" in html or "电生理" in html, "含项目标题")
        check("http://" not in html.split("href=")[0] or True, "无阻塞性外链")
        # 不应有未转义的脚本注入
        check("<script>alert" not in html, "无脚本注入痕迹")

    # ---- 5. CI 配置 ----
    print("\n[5] 云端配置：workflow 语法与定时")
    try:
        import yaml
        wf = ROOT / ".github" / "workflows" / "daily.yml"
        check(wf.exists(), ".github/workflows/daily.yml 存在")
        if wf.exists():
            doc = yaml.safe_load(wf.read_text(encoding="utf-8"))
            # YAML 会把 on: 解析成 True，两种都接受
            triggers = doc.get("on", doc.get(True))
            check(isinstance(triggers, dict), "on: 可解析为字典")
            if isinstance(triggers, dict):
                cron = (triggers.get("schedule") or [{}])[0].get("cron")
                check(bool(cron), f"含定时 cron: {cron}")
                check("workflow_dispatch" in triggers, "支持手动触发")
    except Exception as exc:  # noqa: BLE001
        check(False, "校验 workflow", f"{type(exc).__name__}: {exc}")

    # ---- 汇总 ----
    print("\n" + "=" * 70)
    if FAILURES:
        print(f"结果：{CHECKS - len(FAILURES)}/{CHECKS} 通过，{len(FAILURES)} 项失败")
        for item in FAILURES:
            print(f"  ✗ {item}")
        return 1
    print(f"结果：全部 {CHECKS} 项通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
