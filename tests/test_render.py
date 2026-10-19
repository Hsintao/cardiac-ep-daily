"""渲染层测试。

覆盖任务要求的四类情况 + 契约相关的防御性用例：
  - HTML 转义（XSS 防护，最高优先级）
  - 空数据集
  - no_abstract=True
  - 无 AI 解读
  - 分组顺序、CLI、链接降级、离线自包含（无外链）

全部使用虚构数据，不依赖采集层 / AI 层的真实输出。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from daily_ep.schema import TOPIC_ORDER  # noqa: E402
from render import build_html  # noqa: E402

FIXED_TIME = datetime(2026, 10, 19, 21, 30)


# ---------------------------------------------------------------------------
# 虚构样例数据工厂
# ---------------------------------------------------------------------------

def make_paper(**overrides):
    """一条字段完整的虚构论文（契约见 daily_ep.schema.Paper.to_dict）。"""
    paper = {
        "id": "pmid-40000001",
        "title": "Pulsed Field Ablation Versus Thermal Ablation for Paroxysmal Atrial Fibrillation",
        "authors": ["Zhang Wei", "Li Ming", "Chen Hua"],
        "summary": "Background: Pulsed field ablation is a novel non-thermal energy source. "
                   "Methods: We randomized 320 patients. Results: Freedom from AF was 78% vs 65%.",
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
        "pub_types": ["Randomized Controlled Trial"],
        "is_open_access": False,
        "topic": "房颤与消融",
        "no_abstract": False,
        "AI": {
            "tldr": "脉冲场消融在阵发性房颤中优于热消融。",
            "motivation": "比较两种能量方式的疗效与安全性。",
            "method": "多中心随机对照试验，320 例患者，随访 12 个月。",
            "result": "12 个月无房颤复发率 78% vs 65%（p=0.003）。",
            "conclusion": "脉冲场消融可作为阵发性房颤的一线选择。",
        },
    }
    paper.update(overrides)
    return paper


def write_jsonl(path: Path, items) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    return path


@pytest.fixture
def sample_path(tmp_path):
    return write_jsonl(tmp_path / "2026-10-19_AI_enhanced_Chinese.jsonl", [make_paper()])


def render_one(tmp_path, papers, **kwargs):
    """渲染一组虚构数据，返回 (index_html, archive_html)。"""
    data = write_jsonl(tmp_path / "2026-10-19_AI_enhanced_Chinese.jsonl", papers)
    out = tmp_path / "index.html"
    written = build_html.build_pages(data, out, generated_at=FIXED_TIME, **kwargs)
    return (
        Path(written["index"]).read_text(encoding="utf-8"),
        Path(written["archive"]).read_text(encoding="utf-8"),
    )


# ---------------------------------------------------------------------------
# 1. HTML 转义 / XSS —— 核心安全用例
# ---------------------------------------------------------------------------

class TestEscaping:
    """摘要里的特殊字符绝不能破坏页面结构，更不能变成可执行脚本。"""

    XSS_PAYLOAD = "<script>alert('xss')</script>"
    IMG_PAYLOAD = '<img src=x onerror="alert(1)">'

    def test_title_is_escaped(self, tmp_path):
        html, _ = render_one(tmp_path, [make_paper(title="P<W>ave & QT 'interval' study")])
        assert "P&lt;W&gt;ave &amp; QT &#39;interval&#39; study" in html
        assert "P<W>ave & QT 'interval' study" not in html

    def test_summary_script_tag_is_escaped(self, tmp_path):
        html, _ = render_one(tmp_path, [make_paper(summary=f"Study text {self.XSS_PAYLOAD} more")])
        assert "<script>alert('xss')</script>" not in html
        assert "&lt;script&gt;alert(&#39;xss&#39;)&lt;/script&gt;" in html

    def test_summary_img_onerror_is_escaped(self, tmp_path):
        html, _ = render_one(tmp_path, [make_paper(summary=f"Abstract {self.IMG_PAYLOAD}")])
        assert "<img src=x onerror=" not in html
        assert "&lt;img src=x onerror=" in html

    def test_ai_fields_are_escaped(self, tmp_path):
        html, _ = render_one(tmp_path, [make_paper(AI={
            "tldr": self.XSS_PAYLOAD,
            "motivation": "a < b & c > d",
            "method": self.IMG_PAYLOAD,
            "result": "100% & 200%",
            "conclusion": "</div><script>alert(2)</script>",
        })])
        assert "<script>alert(" not in html
        # 转义后 onerror= 只是inert 文本，关键是它没有落在真实属性位置上
        assert "<img src=x" not in html
        assert "&lt;img src=x onerror=&#34;alert(1)&#34;&gt;" in html
        assert "&lt;/div&gt;&lt;script&gt;alert(2)&lt;/script&gt;" in html
        assert "100% &amp; 200%" in html

    def test_author_and_journal_are_escaped(self, tmp_path):
        html, _ = render_one(tmp_path, [make_paper(
            authors=[self.XSS_PAYLOAD, "O'Brien J"],
            journal="Heart <Rhythm> & Co",
        )])
        assert "<script>alert(" not in html
        assert "Heart &lt;Rhythm&gt; &amp; Co" in html
        assert "O&#39;Brien J" in html

    def test_pub_types_are_escaped(self, tmp_path):
        html, _ = render_one(tmp_path, [make_paper(pub_types=[self.XSS_PAYLOAD])])
        assert "<script>alert(" not in html

    def test_topic_is_escaped(self, tmp_path):
        """未知 topic 会落到「其他」，但即便被渲染也必须转义。"""
        html, _ = render_one(tmp_path, [make_paper(topic=self.XSS_PAYLOAD)])
        assert "<script>alert(" not in html
        assert "其他" in html  # 未知 topic 归入默认分组

    def test_abstract_never_breaks_structure(self, tmp_path):
        """标题里的引号/尖括号不会提前闭合标签或属性。"""
        html, _ = render_one(tmp_path, [make_paper(
            title='"><script>alert(1)</script>',
            summary="</p></details></div><script>alert(3)</script>",
        )])
        # 页面固定带两个内联脚本：主题初始化 + 筛选/主题切换；
        # 关键断言是下面这条 —— 用户数据里的脚本绝不能出现。
        assert html.count("<script>") == 2
        assert "<script>alert" not in html


class TestUrlWhitelist:
    """URL 只放行 http/https/mailto，阻断 javascript: 伪协议。"""

    def test_javascript_url_dropped(self, tmp_path):
        html, _ = render_one(tmp_path, [make_paper(
            url="javascript:alert(1)", abs="javascript:alert(1)", doi="", pmid="",
        )])
        assert "javascript:alert" not in html

    def test_data_url_dropped(self, tmp_path):
        html, _ = render_one(tmp_path, [make_paper(url="data:text/html,<script>alert(1)</script>")])
        assert "data:text/html" not in html

    def test_doi_cannot_break_out_of_url(self, tmp_path):
        """doi 字段被拼进 https://doi.org/，必须阻断伪协议、路径穿越与引号逃逸。"""
        html, _ = render_one(tmp_path, [
            make_paper(doi="javascript:alert(1)"),
            make_paper(id="p2", title="Second", doi="../../../../etc/passwd"),
            make_paper(id="p3", title="Third", doi='x"><script>alert(9)</script>'),
        ])
        # 伪协议：冒号被编码，无法形成 scheme
        assert "doi.org/javascript:" not in html
        assert 'href="javascript:' not in html
        # 路径穿越：.. 段被剔除
        assert "doi.org/" not in html or ".." not in [
            s for s in html.split('href="https://doi.org/')[1:]
        ][0].split('"')[0]
        # 引号被编码，无法闭合 href 属性
        assert '"><script>' not in html
        assert html.count("<script>") == 2  # 主题初始化 + 筛选/切换，均为自带脚本

    def test_doi_sanitizer_unit(self):
        assert build_html._doi_path("10.1016/j.hrthm.2026.10.001") == "10.1016/j.hrthm.2026.10.001"
        assert build_html._doi_path("javascript:alert(1)") == "javascript%3Aalert%281%29"
        assert build_html._doi_path("../../etc/passwd") == "etc/passwd"
        assert ".." not in build_html._doi_path("a/../../b")
        assert "<" not in build_html._doi_path('x"><script>')

    def test_pmid_cannot_break_out_of_url(self, tmp_path):
        html, _ = render_one(tmp_path, [make_paper(
            pmid='1/../../evil"><script>alert(1)</script>', url="", abs="",
        )])
        assert '"><script>' not in html
        assert 'href="https://pubmed.ncbi.nlm.nih.gov/1/' not in html
        assert html.count("<script>") == 2  # 主题初始化 + 筛选/切换，均为自带脚本

    def test_pmid_sanitizer_unit(self):
        assert build_html._url_segment("40000001") == "40000001"
        assert "/" not in build_html._url_segment("1/2")
        assert '"' not in build_html._url_segment('a"b')

    def test_safe_url_filter_directly(self):
        assert build_html.safe_url("https://pubmed.ncbi.nlm.nih.gov/1/") == \
            "https://pubmed.ncbi.nlm.nih.gov/1/"
        assert build_html.safe_url("http://example.org/a.pdf") == "http://example.org/a.pdf"
        assert build_html.safe_url("mailto:a@b.org") == "mailto:a@b.org"
        assert build_html.safe_url("javascript:alert(1)") == ""
        assert build_html.safe_url("  vbscript:msgbox  ") == ""
        assert build_html.safe_url(None) == ""
        assert build_html.safe_url("") == ""


# ---------------------------------------------------------------------------
# 2. 空数据集
# ---------------------------------------------------------------------------

class TestEmptyDataset:
    def test_empty_jsonl_produces_friendly_page(self, tmp_path):
        html, archive = render_one(tmp_path, [])
        assert "<!DOCTYPE html>" in html
        assert "今天没有匹配到论文" in html
        assert "<strong>0</strong>" in html  # 论文总数 0
        # 空数据不应渲染任何论文卡片 / 分组（样式表里的 .paper-card 定义不算）
        assert '<article class="paper-card"' not in html
        assert '<section class="topic-group"' not in html

    def test_empty_archive_page(self, tmp_path):
        _, archive = render_one(tmp_path, [])
        assert "该日期没有论文" in archive
        assert "<!DOCTYPE html>" in archive

    def test_only_blank_lines(self, tmp_path):
        path = tmp_path / "2026-10-19_AI_enhanced_Chinese.jsonl"
        path.write_text("\n\n   \n", encoding="utf-8")
        out = tmp_path / "index.html"
        build_html.build_pages(path, out, generated_at=FIXED_TIME)
        html = out.read_text(encoding="utf-8")
        assert "今天没有匹配到论文" in html

    def test_malformed_line_does_not_crash(self, tmp_path, capsys):
        path = tmp_path / "2026-10-19_AI_enhanced_Chinese.jsonl"
        path.write_text(
            json.dumps(make_paper(), ensure_ascii=False) + "\n"
            + "{ this is not json\n"
            + json.dumps(make_paper(id="p2", title="Second paper"), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        out = tmp_path / "index.html"
        build_html.build_pages(path, out, generated_at=FIXED_TIME)
        html = out.read_text(encoding="utf-8")
        assert "Second paper" in html
        assert "跳过 1 行" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 3. no_abstract=True
# ---------------------------------------------------------------------------

class TestNoAbstract:
    def test_shows_placeholder(self, tmp_path):
        html, _ = render_one(tmp_path, [make_paper(summary="", no_abstract=True)])
        assert "暂无摘要" in html

    def test_empty_summary_auto_flagged(self, tmp_path):
        """summary 为空但 no_abstract 未置位时，也应降级为提示而非空白。"""
        html, _ = render_one(tmp_path, [make_paper(summary="", no_abstract=False)])
        assert "暂无摘要" in html

    def test_no_empty_details_block(self, tmp_path):
        html, _ = render_one(tmp_path, [make_paper(summary="", no_abstract=True)])
        assert 'class="abstract-body"' not in html

    def test_header_counts_no_abstract(self, tmp_path):
        html, _ = render_one(tmp_path, [
            make_paper(summary="", no_abstract=True),
            make_paper(id="p2", title="Has abstract"),
        ])
        assert "无摘要" in html


# ---------------------------------------------------------------------------
# 4. 无 AI 解读（优雅降级）
# ---------------------------------------------------------------------------

class TestNoAI:
    def test_falls_back_to_abstract_only(self, tmp_path):
        html, _ = render_one(tmp_path, [make_paper(AI=None)])
        assert "未生成 AI 解读" in html
        # 摘要仍然完整呈现
        assert "Pulsed field ablation is a novel non-thermal energy source" in html

    def test_no_ai_panel_and_no_empty_headings(self, tmp_path):
        html, _ = render_one(tmp_path, [make_paper(AI=None)])
        assert 'class="ai-panel"' not in html
        assert 'class="ai-label' not in html
        for label in ["一句话结论", "研究动机", "方法", "结果", "结论与意义"]:
            assert label not in html

    def test_partial_ai_only_renders_present_sections(self, tmp_path):
        html, _ = render_one(tmp_path, [make_paper(AI={"tldr": "只有一句话结论。"})])
        assert "只有一句话结论。" in html
        assert "一句话结论" in html
        assert "研究动机" not in html
        assert "结论与意义" not in html
        assert "未生成 AI 解读" not in html

    def test_empty_ai_dict_is_treated_as_missing(self, tmp_path):
        html, _ = render_one(tmp_path, [make_paper(AI={})])
        assert "未生成 AI 解读" in html
        assert 'class="ai-panel"' not in html

    def test_blank_ai_values_ignored(self, tmp_path):
        html, _ = render_one(tmp_path, [make_paper(AI={"tldr": "   ", "method": ""})])
        assert "一句话结论" not in html
        assert "方法" not in html
        assert "未生成 AI 解读" in html


# ---------------------------------------------------------------------------
# 5. 分组顺序与内容结构
# ---------------------------------------------------------------------------

class TestGrouping:
    def test_groups_follow_topic_order(self, tmp_path):
        papers = [
            make_paper(id=f"p{i}", title=f"Paper {topic}", topic=topic, category=topic,
                       categories=[topic])
            for i, topic in enumerate(reversed(TOPIC_ORDER))
        ]
        html, _ = render_one(tmp_path, papers)
        positions = [html.index(f'data-group-topic="{t}"') for t in TOPIC_ORDER]
        assert positions == sorted(positions), "分组顺序必须与 TOPIC_ORDER 一致"

    def test_all_six_topics_present(self, tmp_path):
        papers = [make_paper(id=f"p{i}", title=f"Paper{i}", topic=t, category=t, categories=[t])
                  for i, t in enumerate(TOPIC_ORDER)]
        html, _ = render_one(tmp_path, papers)
        for topic in TOPIC_ORDER:
            assert f'data-group-topic="{topic}"' in html

    def test_empty_groups_are_omitted(self, tmp_path):
        html, _ = render_one(tmp_path, [make_paper(topic="器械治疗")])
        assert 'data-group-topic="器械治疗"' in html
        assert 'data-group-topic="遗传与基础"' not in html

    def test_group_count_labels(self, tmp_path):
        html, _ = render_one(tmp_path, [
            make_paper(id="a", topic="室性心律失常"),
            make_paper(id="b", topic="室性心律失常"),
        ])
        assert "2 篇" in html

    def test_unknown_topic_falls_back_to_other(self, tmp_path):
        html, _ = render_one(tmp_path, [make_paper(topic="不存在的主题", category="不存在的主题",
                                                   categories=["不存在的主题"])])
        assert 'data-group-topic="其他"' in html

    def test_missing_topic_uses_category(self, tmp_path):
        paper = make_paper()
        paper.pop("topic")
        html, _ = render_one(tmp_path, [paper])
        assert 'data-group-topic="房颤与消融"' in html


# ---------------------------------------------------------------------------
# 6. 页面结构与离线自包含
# ---------------------------------------------------------------------------

class TestPageStructure:
    def test_header_shows_time_and_total(self, tmp_path):
        html, _ = render_one(tmp_path, [
            make_paper(id="a"), make_paper(id="b", title="Second"),
        ])
        assert "2026-10-19 21:30" in html
        assert "论文总数" in html
        assert "<strong>2</strong>" in html
        assert "2026-10-19" in html

    def test_no_cdn_or_external_resources(self, tmp_path):
        html, archive = render_one(tmp_path, [make_paper()])
        for page in (html, archive):
            assert "cdn." not in page.lower()
            assert "http://" not in page.replace("http://www.w3.org", "")  # 允许 SVG 命名空间
            assert 'src="http' not in page
            assert "<link" not in page  # 没有外部样式表

    def test_style_is_inlined(self, tmp_path):
        html, _ = render_one(tmp_path, [make_paper()])
        assert "<style>" in html
        assert ".paper-card" in html

    def test_links_only_when_present(self, tmp_path):
        html, _ = render_one(tmp_path, [make_paper(doi="", pmid="", pdf="", is_open_access=False)])
        assert ">PubMed<" not in html
        assert ">DOI<" not in html
        assert ">PDF<" not in html

    def test_all_links_rendered_when_present(self, tmp_path):
        html, _ = render_one(tmp_path, [make_paper(pdf="https://example.org/a.pdf")])
        assert "pubmed.ncbi.nlm.nih.gov/40000001" in html
        assert "https://doi.org/10.1016/j.hrthm.2026.10.001" in html
        assert "https://example.org/a.pdf" in html

    def test_open_access_badge(self, tmp_path):
        html, _ = render_one(tmp_path, [make_paper(is_open_access=True, pdf="https://e.org/a.pdf")])
        assert "开放获取" in html

    def test_ai_five_sections_rendered(self, tmp_path):
        html, _ = render_one(tmp_path, [make_paper()])
        for label in ["AI 中文解读", "一句话结论", "研究动机", "方法", "结果", "结论与意义"]:
            assert label in html

    def test_filter_hooks_present(self, tmp_path):
        html, _ = render_one(tmp_path, [
            make_paper(id="a"), make_paper(id="b", journal="Europace"),
        ])
        assert 'data-filter-topic="all"' in html
        assert 'data-filter-journal="all"' in html
        assert 'id="textSearch"' in html
        assert "Europace" in html
        assert html.count("data-journal=") >= 2

    def test_abstract_is_collapsible(self, tmp_path):
        html, _ = render_one(tmp_path, [make_paper()])
        assert "<details" in html
        assert "<summary>" in html


# ---------------------------------------------------------------------------
# 7. 归档页
# ---------------------------------------------------------------------------

class TestArchive:
    def test_archive_written_next_to_index(self, tmp_path):
        data = write_jsonl(tmp_path / "2026-10-19_AI_enhanced_Chinese.jsonl", [make_paper()])
        written = build_html.build_pages(data, tmp_path / "index.html", generated_at=FIXED_TIME)
        assert Path(written["archive"]).name == "2026-10-19.html"
        assert Path(written["archive"]).parent.name == "archive"
        assert Path(written["archive"]).exists()

    def test_archive_links_back_to_index(self, tmp_path):
        _, archive = render_one(tmp_path, [make_paper()])
        assert 'href="../index.html"' in archive

    def test_archive_lists_all_papers(self, tmp_path):
        _, archive = render_one(tmp_path, [
            make_paper(id="a", title="Alpha study"),
            make_paper(id="b", title="Beta study"),
        ])
        assert "Alpha study" in archive
        assert "Beta study" in archive


# ---------------------------------------------------------------------------
# 8. 输入健壮性 / 工具函数
# ---------------------------------------------------------------------------

class TestRobustness:
    def test_missing_fields_do_not_crash(self, tmp_path):
        html, _ = render_one(tmp_path, [{"id": "minimal", "title": "只有标题"}])
        assert "只有标题" in html

    def test_non_dict_items_skipped(self, tmp_path):
        path = tmp_path / "2026-10-19_AI_enhanced_Chinese.jsonl"
        path.write_text(
            '["not", "a", "dict"]\n' + json.dumps(make_paper(), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        out = tmp_path / "index.html"
        build_html.build_pages(path, out, generated_at=FIXED_TIME)
        assert "Pulsed Field Ablation" in out.read_text(encoding="utf-8")

    def test_wrong_types_tolerated(self, tmp_path):
        html, _ = render_one(tmp_path, [{
            "id": 12345, "title": "Numeric id",
            "authors": "Single Author String",
            "summary": 42, "pub_types": None, "AI": "not a dict",
            "date": 20261019,
        }])
        assert "Numeric id" in html
        assert "Single Author String" in html

    def test_jinja_undefined_raises_loudly(self):
        """StrictUndefined：模板里写错变量名要在测试期就炸，而不是静默空白。"""
        with pytest.raises(Exception):
            build_html._env().get_template("index.html.j2").render(style="")

    def test_infer_date_from_data(self, tmp_path):
        items = [make_paper(date="2026-10-18"), make_paper(id="b", date="2026-10-18"),
                 make_paper(id="c", date="2026-10-17")]
        assert build_html.infer_date(items) == "2026-10-18"

    def test_date_from_filename(self):
        assert build_html.date_from_filename("data/2026-10-19_AI_enhanced_Chinese.jsonl") == "2026-10-19"
        assert build_html.date_from_filename("whatever.jsonl") == ""

    def test_normalize_converts_lists(self):
        paper = build_html.normalize_paper({
            "id": "x", "title": "t", "authors": ["A", "", None, "B"], "pub_types": ["Review", None],
        })
        assert paper["authors"] == ["A", "B"]
        assert paper["pub_types"] == ["Review"]


# ---------------------------------------------------------------------------
# 9. CLI
# ---------------------------------------------------------------------------

class TestCLI:
    def test_main_renders(self, tmp_path, capsys):
        data = write_jsonl(tmp_path / "2026-10-19_AI_enhanced_Chinese.jsonl", [make_paper()])
        out = tmp_path / "out" / "index.html"
        rc = build_html.main(["--data", str(data), "--out", str(out)])
        assert rc == 0
        assert out.exists()
        assert (out.parent / "archive" / "2026-10-19.html").exists()

    def test_main_missing_file_returns_2(self, tmp_path):
        assert build_html.main(["--data", str(tmp_path / "nope.jsonl")]) == 2

    def test_cli_module_entrypoint(self, sample_path, tmp_path):
        """python -m render.build_html 的模块入口存在且可调用。"""
        out = tmp_path / "cli.html"
        rc = build_html.main(["--data", str(sample_path), "--out", str(out), "--date", "2026-10-19"])
        assert rc == 0
        html = out.read_text(encoding="utf-8")
        assert "2026-10-19" in html
        assert "Pulsed Field Ablation" in html


# ---------------------------------------------------------------------------
# 10. 完整快照式冒烟（虚构数据的端到端）
# ---------------------------------------------------------------------------

def test_full_sample_smoke(tmp_path):
    """一条完整数据 + 一条降级数据混排，页面结构完整且无模板残留。"""
    papers = [
        make_paper(),
        make_paper(
            id="pmid-40000002",
            title="Leadless Pacemaker Implantation: A Single-Center Experience",
            topic="器械治疗", category="器械治疗", categories=["器械治疗"],
            journal="Europace", summary="", no_abstract=True, AI=None,
            pmid="40000002", doi="", url="https://pubmed.ncbi.nlm.nih.gov/40000002/",
            abs="https://pubmed.ncbi.nlm.nih.gov/40000002/", pub_types=["Review"],
            is_open_access=True, pdf="https://europepmc.org/a.pdf",
        ),
    ]
    html, archive = render_one(tmp_path, papers)

    # 无未渲染的模板语法残留
    assert "{{" not in html and "{%" not in html
    assert "{{" not in archive and "{%" not in archive

    # 两篇都在，两个分组都在
    assert "Pulsed Field Ablation Versus" in html
    assert "Leadless Pacemaker Implantation" in html
    assert 'data-group-topic="房颤与消融"' in html
    assert 'data-group-topic="器械治疗"' in html

    # 降级路径同时生效
    assert "暂无摘要" in html
    assert "未生成 AI 解读" in html
    assert "AI 中文解读" in html

    # 计数正确
    assert "<strong>2</strong>" in html
