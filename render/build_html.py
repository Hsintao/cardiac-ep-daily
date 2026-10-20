"""JSONL -> HTML 渲染层。

用法::

    python -m render.build_html --data data/2026-10-19_AI_enhanced_Chinese.jsonl --out index.html

设计要点
--------
1. **离线自包含**：样式与脚本全部内联进 HTML，不引用任何 CDN / 外链资源，
   双击打开即可，断网也不掉样式。
2. **安全**：论文文本（标题、摘要、作者、AI 解读…）全部经 Jinja2 ``autoescape``
   转义，URL 额外做协议白名单（只允许 http/https/mailto），防止 ``javascript:``。
3. **降级**：无 AI 解读只显示摘要；``no_abstract`` 显示「暂无摘要」；空数据集显示空状态。
4. **分组**：按 ``daily_ep.schema.TOPIC_ORDER`` 分组，未知 topic 归入「其他」。

本模块不修改也不依赖采集层的内部实现，只依赖 ``Paper.to_dict()`` 的字段契约。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

# 契约（字段顺序 / 分组顺序）的唯一来源；本项目不复制定义，直接引用。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from daily_ep.schema import DEFAULT_TOPIC, TOPIC_ORDER  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
STYLE_PATH = BASE_DIR / "assets" / "style.css"

# AI 解读五段：键 -> (中文小标题, 配色 class)
AI_SECTIONS: List[Tuple[str, str, str]] = [
    ("tldr", "一句话结论", "tldr"),
    ("motivation", "研究动机", "motivation"),
    ("method", "方法", "method"),
    ("result", "结果", "result"),
    ("conclusion", "结论与意义", "conclusion"),
]

# 只有这些协议允许出现在 href 里。
_SAFE_SCHEMES = ("http://", "https://", "mailto:")

# 生成时间格式（页面上展示用，本地时区）
_TIME_FMT = "%Y-%m-%d %H:%M"


# ---------------------------------------------------------------------------
# 规范化 / 安全
# ---------------------------------------------------------------------------

def safe_url(value: Any) -> str:
    """过滤 URL：只放行 http/https/mailto，其余一律返回空串。

    摘要或标题里混进 ``javascript:alert(1)`` 这类内容时不能变成可点击链接。
    返回空串后模板里的 ``{% if %}`` 会直接不渲染该链接。
    """
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered.startswith(_SAFE_SCHEMES):
        return text
    return ""


def _url_segment(value: str) -> str:
    """把值编码成单个 URL 路径片段（不含 ``/``）。

    PMID 之类的字段被拼进固定前缀后，必须保证它无法改变路径结构：
    斜杠、引号、尖括号、冒号全部百分号编码。
    """
    return quote(value, safe="")


def _doi_path(value: str) -> str:
    """编码 DOI 路径。

    DOI 本身以 ``/`` 分隔（如 ``10.1016/j.hrthm.2026.10.001``），所以斜杠必须保留，
    否则合法链接会被破坏。其余字符一律编码，阻断伪协议与引号逃逸；
    ``..`` 路径段额外剔除，避免拼出意外路径。
    """
    cleaned = "/".join(seg for seg in value.split("/") if seg not in ("..", "."))
    return quote(cleaned, safe="/")


def _text(value: Any) -> str:
    """把任意 JSON 值收敛成字符串（None -> ""，数字/布尔 -> str）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return str(value).strip()


def _string_list(value: Any) -> List[str]:
    """把字段收敛成字符串列表，容忍 None / 单个字符串 / 混杂类型。"""
    if value is None:
        return []
    if isinstance(value, str):
        parts = [value]
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        parts = [value]
    out: List[str] = []
    for item in parts:
        text = _text(item)
        if text:
            out.append(text)
    return out


def normalize_paper(raw: Dict[str, Any]) -> Dict[str, Any]:
    """把一条原始 JSON 记录收敛成模板可直接使用的结构。

    对缺失/类型错误的字段全部做兜底，渲染层不因为脏数据抛异常。
    """
    raw = raw if isinstance(raw, dict) else {}

    topic = _text(raw.get("topic")) or _text(raw.get("category"))
    if topic not in TOPIC_ORDER:
        # 优先接受 categories[0]，再退回默认主题
        cats = _string_list(raw.get("categories"))
        topic = cats[0] if cats and cats[0] in TOPIC_ORDER else DEFAULT_TOPIC

    # 链接：url 与 abs 兼容同一个概念，谁有用谁。
    abs_url = safe_url(raw.get("abs")) or safe_url(raw.get("url"))
    summary = _text(raw.get("summary"))
    no_abstract = bool(raw.get("no_abstract")) or not summary

    ai_raw = raw.get("AI")
    ai_sections: List[Dict[str, str]] = []
    if isinstance(ai_raw, dict):
        for key, label, css in AI_SECTIONS:
            body = _text(ai_raw.get(key))
            if body:
                ai_sections.append({"key": key, "label": label, "css": css, "text": body})

    doi = _text(raw.get("doi"))
    pmid = _text(raw.get("pmid"))

    links: List[Dict[str, str]] = []

    def _add_link(label: str, href: str, css: str) -> None:
        if href:
            links.append({"label": label, "href": href, "css": css})

    _add_link("PubMed", safe_url(raw.get("pubmed_url"))
              or (f"https://pubmed.ncbi.nlm.nih.gov/{_url_segment(pmid)}/" if pmid else ""), "pubmed")
    _add_link("DOI", f"https://doi.org/{_doi_path(doi)}" if doi else "", "doi")
    _add_link("PDF", safe_url(raw.get("pdf")), "pdf")
    _add_link("原文", abs_url, "source")

    return {
        "id": _text(raw.get("id")),
        "title": _text(raw.get("title")) or "（无标题）",
        "authors": _string_list(raw.get("authors")),
        "summary": summary,
        "summary_zh": _text(raw.get("summary_zh")),
        "no_abstract": no_abstract,
        "abstract_url": abs_url,
        "date": _text(raw.get("date")),
        "journal": _text(raw.get("journal")),
        "source": _text(raw.get("source")),
        "doi": doi,
        "pmid": pmid,
        "pub_types": _string_list(raw.get("pub_types")),
        "is_open_access": bool(raw.get("is_open_access")),
        "topic": topic,
        "ai_sections": ai_sections,
        "has_ai": bool(ai_sections),
        "links": links,
    }


def group_by_topic(papers: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 TOPIC_ORDER 分组，只保留有内容的组，组内保持输入顺序。"""
    buckets: Dict[str, List[Dict[str, Any]]] = {t: [] for t in TOPIC_ORDER}
    for paper in papers:
        buckets.setdefault(paper["topic"], []).append(paper)
    groups: List[Dict[str, Any]] = []
    for topic in TOPIC_ORDER:
        items = buckets.get(topic) or []
        if items:
            groups.append({"topic": topic, "papers": items, "count": len(items)})
    # 理论上 normalize 已把 topic 收敛进 TOPIC_ORDER，这里防御性兜底。
    for topic, items in buckets.items():
        if topic not in TOPIC_ORDER and items:
            groups.append({"topic": topic, "papers": items, "count": len(items)})
    return groups


def journal_counts(papers: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """期刊 -> 篇数，用于前端筛选按钮（篇数多的排前面）。"""
    counts: Dict[str, int] = {}
    for paper in papers:
        key = paper["journal"] or "未知期刊"
        counts[key] = counts.get(key, 0) + 1
    return [
        {"name": name, "count": count}
        for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def discover_archives(archive_dir: os.PathLike | str, *, exclude: str = "") -> List[str]:
    """扫描 archive/ 目录，返回可用的历史日期（新日期在前）。

    页面是纯静态的，「往期」链接必须指向真实存在的文件；直接扫目录比维护
    一份清单更可靠 —— 清单会和实际文件不同步（例如补跑历史、手工删除）。
    """
    root = Path(archive_dir)
    if not root.is_dir():
        return []
    days = []
    for path in root.glob("*.html"):
        stem = path.stem
        # 只认真实日期，跳过 archive.html 之类的兜底文件名
        if len(stem) == 10 and stem[4] == "-" and stem[7] == "-":
            if stem != exclude:
                days.append(stem)
    return sorted(set(days), reverse=True)


_MONTH_LABELS = {m: f"{m} 月" for m in range(1, 13)}


def group_archives_by_month(days: Sequence[str]) -> List[Dict[str, Any]]:
    """把日期列表按月份分组，供日期选择器渲染。

    输入需已按时间倒序排列；输出保持该顺序（最新的月份在前），
    月份内的日期也保持倒序。形如::

        [{"label": "2026 年 10 月", "days": ["2026-10-20", ...]}, ...]
    """
    groups: List[Dict[str, Any]] = []
    for day in days:
        if len(day) < 7:
            continue
        year, month = day[:4], day[5:7]
        label = f"{year} 年 {int(month)} 月"
        if groups and groups[-1]["label"] == label:
            groups[-1]["days"].append(day)
        else:
            groups.append({"label": label, "days": [day]})
    return groups


def build_context(
    raw_items: Iterable[Dict[str, Any]],
    *,
    date: str = "",
    generated_at: Optional[datetime] = None,
    archives: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """把原始记录整理成模板上下文（主页面与归档页共用）。"""
    generated_at = generated_at or datetime.now()
    papers = [normalize_paper(item) for item in raw_items]
    groups = group_by_topic(papers)
    ai_count = sum(1 for p in papers if p["has_ai"])
    no_abstract_count = sum(1 for p in papers if p["no_abstract"])

    day = _text(date)
    return {
        "date": day,
        "archive_name": f"{day}.html" if day else "",
        "archives": list(archives or []),
        "date_groups": group_archives_by_month(list(archives or [])),
        "generated_at": generated_at.strftime(_TIME_FMT),
        "year": generated_at.year,
        "total": len(papers),
        "ai_count": ai_count,
        "no_abstract_count": no_abstract_count,
        "group_count": len(groups),
        "groups": groups,
        "journals": journal_counts(papers),
        "topics": list(TOPIC_ORDER),
    }


# ---------------------------------------------------------------------------
# 读取输入
# ---------------------------------------------------------------------------

def load_jsonl(path: os.PathLike | str) -> List[Dict[str, Any]]:
    """读 JSONL。空行跳过，坏行跳过并计数（不因为一行脏数据丢掉整天）。"""
    items: List[Dict[str, Any]] = []
    bad = 0
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if isinstance(obj, dict):
                items.append(obj)
            else:
                bad += 1
    if bad:
        print(f"[render] 跳过 {bad} 行无法解析的 JSON", file=sys.stderr)
    return items


def infer_date(items: Iterable[Dict[str, Any]], fallback: str = "") -> str:
    """从数据里推断日报日期：取出现最多的 date。"""
    counts: Dict[str, int] = {}
    for item in items:
        key = _text(item.get("date")) if isinstance(item, dict) else ""
        if key:
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return fallback
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def date_from_filename(path: os.PathLike | str) -> str:
    """从 ``2026-10-19_AI_enhanced_Chinese.jsonl`` 里抠出 ``2026-10-19``。"""
    prefix = Path(path).name.split("_")[0]
    if len(prefix) == 10 and prefix[4] == "-" and prefix[7] == "-" and prefix.replace("-", "").isdigit():
        return prefix
    return ""


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(default=True, default_for_string=True),
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
    )
    env.filters["safe_url"] = safe_url
    return env


APP_SCRIPT_PATH = BASE_DIR / "assets" / "app.js"


def read_app_script() -> str:
    """读 ``assets/app.js``（前端渲染脚本），内联进页面。

    内联而不是外链：保持「单文件、可离线双击打开」的特性，
    也让 Pages 部署不依赖额外的静态资源路径。
    """
    try:
        return APP_SCRIPT_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def read_style() -> str:
    """读 ``assets/style.css``；缺失时用一份最小兜底，保证页面仍然可用。"""
    try:
        return STYLE_PATH.read_text(encoding="utf-8")
    except OSError:
        return "body{font-family:-apple-system,'PingFang SC',sans-serif;margin:0;padding:24px}"


def render_index(context: Dict[str, Any], *, style: Optional[str] = None) -> str:
    """渲染主页面 HTML 字符串。"""
    template = _env().get_template("index.html.j2")
    return template.render(
        style=style if style is not None else read_style(),
        depth=0,  # 主页面在站点根目录
        **context,
    )


def build_site(
    data_dir: os.PathLike | str,
    out_path: os.PathLike | str = "index.html",
    *,
    generated_at: Optional[datetime] = None,
    default_date: str = "",
) -> Dict[str, str]:
    """生成**一个** HTML 壳，运行时按日期 fetch 对应的 JSONL。

    这是对齐参考项目 daily-arXiv-ai-enhanced 的架构：
      - 只有一个页面，没有「每天一个归档页」；
      - 日期列表来自 ``data/`` 目录（构建时扫描，直接写进页面）；
      - 用户选日期后，前端 ``fetch('data/YYYY-MM-DD_AI_enhanced_Chinese.jsonl')``，
        解析 JSONL 就地渲染。

    相比「31 个独立归档页」或「2MB 数据内嵌」，这个方案：
      - 页面恒定很小（几十 KB），首屏快；
      - 历史日期按需加载，不会拖慢打开速度；
      - **只有一个模板**，不存在两套模板风格漂移的问题（之前就因此出过 bug）。

    数据与页面同源（都发布在 data 分支 / Pages 根目录），所以用相对路径
    fetch 即可，没有 CORS 问题，也不需要配置仓库名。
    """
    data_dir = Path(data_dir)
    files = sorted(data_dir.glob("*_AI_enhanced_*.jsonl"))
    if not files:
        raise SystemExit(f"[render] {data_dir} 下没有 *_AI_enhanced_*.jsonl 数据")

    days = []
    for path in files:
        day = date_from_filename(path)
        if day:
            days.append(day)
    days = sorted(set(days), reverse=True)
    if not days:
        raise SystemExit("[render] 未能从数据文件名解析出任何日期")

    now = generated_at or datetime.now()
    default = default_date if default_date in days else days[0]

    context = {
        "days": days,
        "default_date": default,
        "date_groups": group_archives_by_month(days),
        "language": os.environ.get("LANGUAGE", "Chinese") or "Chinese",
        "generated_at": now.strftime(_TIME_FMT),
    }

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    template = _env().get_template("index.html.j2")
    out_path.write_text(
        template.render(style=read_style(), app_script=read_app_script(), **context),
        encoding="utf-8",
    )
    print(f"[render] 站点壳 -> {out_path}（{len(days)} 天可用，默认 {default}）")
    return {"index": str(out_path)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m render.build_html",
        description="生成日报站点壳：前端按日期 fetch data/*.jsonl 就地渲染。",
    )
    parser.add_argument(
        "--data-dir", default="data",
        help="包含 YYYY-MM-DD_AI_enhanced_*.jsonl 的目录（默认 data）",
    )
    parser.add_argument("--out", default="index.html", help="输出页面路径（默认 index.html）")
    parser.add_argument(
        "--default-date", default="",
        help="默认显示的日期（默认最新一天）",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        print(f"[render] 找不到数据目录：{data_dir}", file=sys.stderr)
        return 2
    build_site(data_dir, args.out, default_date=args.default_date)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
