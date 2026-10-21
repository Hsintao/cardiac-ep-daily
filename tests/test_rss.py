"""RSS 订阅源（render.build_rss）测试。

验证点：
  - 频道元数据（title / link / atom:self）
  - 一天一个 item，倒序，标题含日期与篇数，pubDate 是合法 RFC 822
  - description 是双层转义的 HTML：XML 解一层后得到合法 HTML 源码，
    其中论文标题里的 <script> / & 必须是 HTML 实体（XSS 防护）
  - 链接全部走 safe_url 白名单 + Pages 绝对地址
"""

from __future__ import annotations

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from render import build_rss  # noqa: E402

BASE_URL = "https://example.github.io/repo"
FIXED_TIME = datetime(2026, 10, 21, 8, 0, tzinfo=build_rss._BEIJING)


def make_paper(**overrides):
    paper = {
        "id": "pmid-40000001",
        "title": "Pulsed Field Ablation for Atrial Fibrillation",
        "authors": ["Zhang Wei"],
        "summary": "Background: PFA is novel.",
        "url": "https://pubmed.ncbi.nlm.nih.gov/40000001/",
        "abs": "https://pubmed.ncbi.nlm.nih.gov/40000001/",
        "pdf": "",
        "date": "2026-10-19",
        "categories": ["房颤与消融"],
        "category": "房颤与消融",
        "source": "pubmed",
        "journal": "Heart Rhythm",
        "doi": "10.1016/j.hrthm.2026.10.001",
        "pmid": "40000001",
        "pub_types": ["Journal Article"],
        "is_open_access": False,
        "topic": "房颤与消融",
        "no_abstract": False,
        "AI": {"tldr": "脉冲场消融优于热消融。"},
    }
    paper.update(overrides)
    return paper


def write_day(data_dir: Path, day: str, papers) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{day}_AI_enhanced_Chinese.jsonl"
    path.write_text(
        "\n".join(json.dumps(p, ensure_ascii=False) for p in papers) + "\n",
        encoding="utf-8",
    )
    return path


def build(tmp_path: Path, days_map, **kwargs) -> ET.Element:
    """构造 data 目录并生成 feed，返回解析后的 rss 根元素。"""
    data_dir = tmp_path / "data"
    for day, papers in days_map.items():
        write_day(data_dir, day, papers)
    out = tmp_path / "feed.xml"
    opts = {"base_url": BASE_URL, "generated_at": FIXED_TIME}
    opts.update(kwargs)
    build_rss.build_feed(data_dir, out, **opts)
    return ET.parse(out).getroot()


def items(root: ET.Element):
    return root.find("channel").findall("item")


# ---------------------------------------------------------------------------
# 频道
# ---------------------------------------------------------------------------

class TestChannel:
    def test_metadata(self, tmp_path):
        root = build(tmp_path, {"2026-10-19": [make_paper()]})
        channel = root.find("channel")
        assert root.attrib["version"] == "2.0"
        assert channel.find("title").text == build_rss.FEED_TITLE
        assert channel.find("link").text == BASE_URL
        assert channel.find("language").text == "zh-cn"
        # lastBuildDate 必须是合法 RFC 822
        parsedate_to_datetime(channel.find("lastBuildDate").text)

    def test_atom_self_link(self, tmp_path):
        root = build(tmp_path, {"2026-10-19": [make_paper()]})
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        self_link = root.find("channel").find("atom:link", ns)
        assert self_link is not None
        assert self_link.attrib["href"] == f"{BASE_URL}/feed.xml"
        assert self_link.attrib["rel"] == "self"


# ---------------------------------------------------------------------------
# item
# ---------------------------------------------------------------------------

class TestItems:
    def test_one_item_per_day_latest_first(self, tmp_path):
        root = build(tmp_path, {
            "2026-10-19": [make_paper()],
            "2026-10-18": [make_paper(id="p2")],
        })
        its = items(root)
        assert [i.find("guid").text for i in its] == [
            "cardiac-ep-daily-2026-10-19",
            "cardiac-ep-daily-2026-10-18",
        ]

    def test_item_title_link_pubdate(self, tmp_path):
        root = build(tmp_path, {"2026-10-19": [make_paper(), make_paper(id="p2")]})
        (item,) = items(root)
        assert item.find("title").text == "2026-10-19 心脏电生理日报（2 篇）"
        assert item.find("link").text == f"{BASE_URL}/#2026-10-19"
        pub = parsedate_to_datetime(item.find("pubDate").text)
        assert (pub.year, pub.month, pub.day) == (2026, 10, 19)
        assert item.find("guid").attrib["isPermaLink"] == "false"

    def test_max_days_limit(self, tmp_path):
        days_map = {f"2026-10-{d:02d}": [make_paper(id=f"p{d}")] for d in range(1, 21)}
        root = build(tmp_path, days_map, max_days=5)
        its = items(root)
        assert len(its) == 5
        assert its[0].find("guid").text == "cardiac-ep-daily-2026-10-20"

    def test_empty_day_skipped(self, tmp_path):
        """0 篇的日期不产出 item（空日报对订阅者只是噪声）。"""
        root = build(tmp_path, {"2026-10-19": [make_paper()], "2026-10-18": []})
        assert len(items(root)) == 1

    def test_empty_data_dir_raises(self, tmp_path):
        with pytest.raises(SystemExit):
            build_rss.build_feed(tmp_path / "empty", tmp_path / "f.xml")


# ---------------------------------------------------------------------------
# description：双层转义的 HTML
# ---------------------------------------------------------------------------

class TestDescription:
    def _desc(self, root: ET.Element, idx: int = 0) -> str:
        # ET 解析已完成 XML 层反转义，这里拿到的是 HTML 源码
        return items(root)[idx].find("description").text

    def test_groups_tldr_and_links(self, tmp_path):
        root = build(tmp_path, {"2026-10-19": [make_paper()]})
        desc = self._desc(root)
        assert "<h3>房颤与消融（1）</h3>" in desc
        assert "脉冲场消融优于热消融。" in desc
        assert '<a href="https://pubmed.ncbi.nlm.nih.gov/40000001/">' in desc
        assert "Heart Rhythm" in desc
        assert f'<a href="{BASE_URL}/#2026-10-19">' in desc

    def test_topic_order_follows_schema(self, tmp_path):
        """分组顺序与 TOPIC_ORDER 一致，不是字母序。"""
        papers = [
            make_paper(id="p1", topic="器械治疗"),
            make_paper(id="p2", topic="房颤与消融"),
        ]
        desc = self._desc(build(tmp_path, {"2026-10-19": papers}))
        assert desc.index("房颤与消融") < desc.index("器械治疗")

    def test_html_in_title_is_escaped(self, tmp_path):
        """论文标题里的 HTML 在 description 里必须是实体（阅读器会渲染这段 HTML）。"""
        paper = make_paper(title="<script>alert(1)</script> A & B")
        desc = self._desc(build(tmp_path, {"2026-10-19": [paper]}))
        assert "<script>" not in desc
        assert "&lt;script&gt;" in desc
        assert "A &amp; B" in desc

    def test_javascript_url_dropped(self, tmp_path):
        """safe_url 白名单与页面一致：javascript: 链接不进入 feed。"""
        paper = make_paper(url="javascript:alert(1)", abs="", doi="")
        desc = self._desc(build(tmp_path, {"2026-10-19": [paper]}))
        assert "javascript:" not in desc

    def test_doi_fallback_link(self, tmp_path):
        paper = make_paper(url="", abs="", doi="10.1234/foo")
        desc = self._desc(build(tmp_path, {"2026-10-19": [paper]}))
        assert 'href="https://doi.org/10.1234/foo"' in desc

    def test_missing_tldr_omitted(self, tmp_path):
        paper = make_paper()
        paper.pop("AI")
        desc = self._desc(build(tmp_path, {"2026-10-19": [paper]}))
        assert paper["title"] in desc  # 标题仍在，只是没有 tldr 行


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCLI:
    def test_main(self, tmp_path):
        data_dir = tmp_path / "data"
        write_day(data_dir, "2026-10-19", [make_paper()])
        out = tmp_path / "feed.xml"
        rc = build_rss.main([
            "--data-dir", str(data_dir), "--out", str(out), "--base-url", BASE_URL,
        ])
        assert rc == 0
        assert out.exists()

    def test_missing_data_dir_returns_2(self, tmp_path):
        rc = build_rss.main(["--data-dir", str(tmp_path / "nope"), "--out", str(tmp_path / "f.xml")])
        assert rc == 2

    def test_cli_module_entrypoint(self, tmp_path):
        data_dir = tmp_path / "data"
        write_day(data_dir, "2026-10-19", [make_paper()])
        out = tmp_path / "feed.xml"
        result = subprocess.run(
            [sys.executable, "-m", "render.build_rss",
             "--data-dir", str(data_dir), "--out", str(out), "--base-url", BASE_URL],
            cwd=str(BASE_DIR), capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert out.exists()
