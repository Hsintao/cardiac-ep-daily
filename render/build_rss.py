"""生成 RSS 2.0 订阅源（feed.xml），供手机 RSS 阅读器接收每日更新。

用法::

    python -m render.build_rss --data-dir data --out feed.xml \
        --base-url https://<用户名>.github.io/<仓库名>

设计要点
--------
1. **一天一个 item**：日报是日更聚合内容，item 标题含日期与篇数，
   描述是按亚专业分组的论文清单（标题 + 期刊 + AI 一句话结论 + 链接）。
2. **base_url 必须是 Pages 的绝对地址**：RSS 阅读器在站外打开链接，
   相对路径会失效。workflow 里由仓库名推导，本地可用 --base-url 覆盖。
3. **复用渲染层的数据约定**：扫描 data/ 的 ``*_AI_enhanced_*.jsonl``，
   日期解析直接复用 build_html.date_from_filename，两处绝不漂移。

本模块不修改也不依赖采集层的内部实现，只读 data/ 目录。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape

# 契约（分组顺序）的唯一来源；本项目不复制定义，直接引用。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from daily_ep.schema import TOPIC_ORDER  # noqa: E402
from render.build_html import date_from_filename, safe_url  # noqa: E402

FEED_TITLE = "心脏电生理每日论文检索"
FEED_DESCRIPTION = "每天自动聚合心脏电生理（EP）领域最新文献，附 AI 中文解读。"

# item 的发布时间：workflow 在北京 06:00 跑完，条目时间与之对齐。
_BEIJING = timezone(timedelta(hours=8))
_ITEM_HOUR = 6

# 其他协议一律不出现在阅读器里（与页面同一白名单，safe_url 直接复用）。
_DOI_PREFIX = "https://doi.org/"


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------

def _load_days(data_dir: Path, max_days: int) -> List[tuple[str, List[Dict[str, Any]]]]:
    """扫描 data/，返回 [(日期, 论文列表)]，按日期倒序、最多 max_days 天。

    0 篇的日期跳过（空日报对订阅者只是噪声）。
    """
    days: List[tuple[str, List[Dict[str, Any]]]] = []
    for path in sorted(data_dir.glob("*_AI_enhanced_*.jsonl"), reverse=True):
        day = date_from_filename(path)
        if not day:
            continue
        if any(d == day for d, _ in days):
            continue
        papers: List[Dict[str, Any]] = []
        try:
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        papers.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
        if papers:
            days.append((day, papers))
        if len(days) >= max_days:
            break
    return days


def _paper_link(paper: Dict[str, Any]) -> str:
    """论文链接优先级：原文 url → 摘要页 → DOI。都不存在时返回空串。"""
    for key in ("url", "abs"):
        link = safe_url(paper.get(key))
        if link:
            return link
    doi = paper.get("doi")
    if isinstance(doi, str) and doi.strip():
        return _DOI_PREFIX + doi.strip()
    return ""


def _day_description(day: str, papers: List[Dict[str, Any]], base_url: str) -> str:
    """单个 item 的描述 HTML：按亚专业分组，列出标题 / 期刊 / tldr / 链接。

    返回的是 HTML 源码；ElementTree 序列化时会再做一层 XML 转义，
    阅读器解开一层后得到的恰是这段 HTML（双层转义是 RSS 的标准做法）。
    """
    page_url = f"{base_url}/#{day}" if base_url else f"#{day}"
    parts = [
        f'<p>共 {len(papers)} 篇。<a href="{escape(page_url)}">在日报页面阅读完整解读 →</a></p>'
    ]

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for paper in papers:
        topic = paper.get("topic") or paper.get("category") or "其他"
        if not isinstance(topic, str) or not topic.strip():
            topic = "其他"
        groups.setdefault(topic, []).append(paper)

    ordered = [t for t in TOPIC_ORDER if t in groups]
    ordered += sorted(t for t in groups if t not in TOPIC_ORDER)

    for topic in ordered:
        items = groups[topic]
        parts.append(f"<h3>{escape(topic)}（{len(items)}）</h3>")
        parts.append("<ul>")
        for paper in items:
            title = escape(str(paper.get("title") or "（无标题）"))
            link = _paper_link(paper)
            if link:
                title_html = f'<a href="{escape(link)}">{title}</a>'
            else:
                title_html = title
            meta_bits = []
            journal = paper.get("journal")
            if isinstance(journal, str) and journal.strip():
                meta_bits.append(escape(journal.strip()))
            pub_date = paper.get("date")
            if isinstance(pub_date, str) and pub_date.strip():
                meta_bits.append(escape(pub_date.strip()))
            meta = f'<br><span>{" · ".join(meta_bits)}</span>' if meta_bits else ""
            ai = paper.get("AI")
            tldr = ai.get("tldr") if isinstance(ai, dict) else None
            tldr_html = f"<br>{escape(tldr.strip())}" if isinstance(tldr, str) and tldr.strip() else ""
            parts.append(f"<li>{title_html}{meta}{tldr_html}</li>")
        parts.append("</ul>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------

def build_feed(
    data_dir: os.PathLike | str,
    out_path: os.PathLike | str = "feed.xml",
    *,
    base_url: str = "",
    max_days: int = 30,
    generated_at: Optional[datetime] = None,
) -> Dict[str, str]:
    """生成 RSS 2.0 订阅源，返回 {"feed": 输出路径}。

    base_url 去掉末尾斜杠后作为站点根地址；为空时链接退化为相对路径
    （仅本地预览可用，阅读器里打不开，会打警告）。
    """
    data_dir = Path(data_dir)
    days = _load_days(data_dir, max_days)
    if not days:
        raise SystemExit(f"[rss] {data_dir} 下没有可用的 *_AI_enhanced_*.jsonl 数据")

    base = base_url.strip().rstrip("/")
    if not base:
        print("[rss] ⚠️ 未提供 base_url，feed 内链接为相对路径，阅读器中无法打开", file=sys.stderr)

    now = generated_at or datetime.now(_BEIJING)

    ET.register_namespace("atom", "http://www.w3.org/2005/Atom")
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = FEED_TITLE
    ET.SubElement(channel, "link").text = base or "/"
    ET.SubElement(channel, "description").text = FEED_DESCRIPTION
    ET.SubElement(channel, "language").text = "zh-cn"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(now)
    if base:
        ET.SubElement(
            channel,
            "{http://www.w3.org/2005/Atom}link",
            {"href": f"{base}/feed.xml", "rel": "self", "type": "application/rss+xml"},
        )

    for day, papers in days:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = f"{day} 心脏电生理日报（{len(papers)} 篇）"
        ET.SubElement(item, "link").text = f"{base}/#{day}" if base else f"#{day}"
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = f"cardiac-ep-daily-{day}"
        pub_dt = datetime.strptime(day, "%Y-%m-%d").replace(
            hour=_ITEM_HOUR, minute=0, tzinfo=_BEIJING
        )
        ET.SubElement(item, "pubDate").text = format_datetime(pub_dt)
        ET.SubElement(item, "description").text = _day_description(day, papers, base)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")
    tree.write(out_path, encoding="utf-8", xml_declaration=True)
    print(f"[rss] 订阅源 -> {out_path}（{len(days)} 天，最新 {days[0][0]}）")
    return {"feed": str(out_path)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m render.build_rss",
        description="生成 RSS 2.0 订阅源（feed.xml），随 data 分支发布到 GitHub Pages。",
    )
    parser.add_argument(
        "--data-dir", default="data",
        help="包含 YYYY-MM-DD_AI_enhanced_*.jsonl 的目录（默认 data）",
    )
    parser.add_argument("--out", default="feed.xml", help="输出路径（默认 feed.xml）")
    parser.add_argument(
        "--base-url", default=os.environ.get("SITE_URL", ""),
        help="站点根地址，如 https://<用户名>.github.io/<仓库名>（默认取环境变量 SITE_URL）",
    )
    parser.add_argument(
        "--days", type=int, default=30,
        help="feed 中最多保留最近几天（默认 30）",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        print(f"[rss] 找不到数据目录：{data_dir}", file=sys.stderr)
        return 2
    build_feed(data_dir, args.out, base_url=args.base_url, max_days=args.days)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
