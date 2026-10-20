"""渲染层测试（单页 fetch 架构）。

架构：`build_site` 只生成一个 HTML 壳，论文数据由前端 `assets/app.js`
按日期 fetch `data/<date>_AI_enhanced_<lang>.jsonl` 后就地渲染。

因此测试重点随之转移：
  - 壳的完整性（日期列表、内嵌脚本、无归档页链接）
  - **前端脚本的转义逻辑**（XSS 防护现在发生在 JS 里，必须有测试）
  - 日期切换、筛选、空状态等前端行为（用 node 跑纯函数，或静态检查）

「HTML 转义」不再能靠 Jinja2 autoescape 覆盖，所以这里用 node 直接执行
app.js 中的关键函数来验证，若本机没有 node 则跳过该部分。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from daily_ep.schema import TOPIC_ORDER  # noqa: E402
from render import build_html  # noqa: E402

FIXED_TIME = datetime(2026, 10, 19, 21, 30)
NODE = shutil.which("node")


def make_paper(**overrides):
    paper = {
        "id": "pmid-40000001",
        "title": "Pulsed Field Ablation Versus Thermal Ablation for Atrial Fibrillation",
        "authors": ["Zhang Wei", "Li Ming", "Chen Hua"],
        "summary": "Background: Pulsed field ablation is novel. Results: Freedom from AF was 78%.",
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
        "AI": {
            "tldr": "脉冲场消融优于热消融。",
            "motivation": "比较两种能量方式。",
            "method": "随机对照试验 320 例。",
            "result": "12 个月无复发率 78% vs 65%。",
            "conclusion": "PFA 可降低复发。",
        },
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


def build(tmp_path: Path, days_map) -> str:
    """构造 data 目录并生成站点壳，返回 HTML。"""
    data_dir = tmp_path / "data"
    for day, papers in days_map.items():
        write_day(data_dir, day, papers)
    out = tmp_path / "index.html"
    build_html.build_site(data_dir, out, generated_at=FIXED_TIME)
    return out.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 壳的完整性
# ---------------------------------------------------------------------------

class TestShell:
    def test_single_page_no_archive_links(self, tmp_path):
        """不再生成每天一个归档页：页面里不应出现 archive/ 链接。"""
        html = build(tmp_path, {"2026-10-19": [make_paper()]})
        assert "archive/" not in html

    def test_date_list_rendered(self, tmp_path):
        html = build(tmp_path, {
            "2026-10-19": [make_paper()],
            "2026-10-01": [make_paper(id="p2")],
            "2026-09-20": [make_paper(id="p3")],
        })
        cells = re.findall(r'data-day="(\d{4}-\d{2}-\d{2})"', html)
        assert cells == ["2026-10-19", "2026-10-01", "2026-09-20"]

    def test_month_grouping(self, tmp_path):
        html = build(tmp_path, {
            "2026-10-19": [make_paper()],
            "2026-09-20": [make_paper(id="p2")],
        })
        assert "2026 年 10 月" in html
        assert "2026 年 9 月" in html

    def test_default_date_is_latest(self, tmp_path):
        html = build(tmp_path, {
            "2026-10-01": [make_paper()],
            "2026-10-19": [make_paper(id="p2")],
        })
        assert '"defaultDate": "2026-10-19"' in html

    def test_app_script_inlined(self, tmp_path):
        html = build(tmp_path, {"2026-10-19": [make_paper()]})
        # 前端渲染脚本必须内联，否则离线打开无法工作
        assert "escapeHtml" in html
        assert "loadDay" in html

    def test_offline_no_external_resources(self, tmp_path):
        html = build(tmp_path, {"2026-10-19": [make_paper()]})
        external = re.findall(
            r'<(?:script|link|img|iframe)[^>]*(?:src|href)="https?://[^"]+"', html
        )
        assert external == []

    def test_theme_init_before_style(self, tmp_path):
        """防闪烁脚本必须在 <style> 之前执行。"""
        html = build(tmp_path, {"2026-10-19": [make_paper()]})
        assert html.index("ep-digest-theme") < html.index("<style>")

    def test_empty_data_dir_raises(self, tmp_path):
        with pytest.raises(SystemExit):
            build_html.build_site(tmp_path / "empty", tmp_path / "out.html")


# ---------------------------------------------------------------------------
# 前端转义（XSS 防护）—— 现在由 JS 负责，用 node 实测
# ---------------------------------------------------------------------------

_jscore = pytest.mark.skipif(NODE is None, reason="需要 node 执行前端脚本")


@_jscore
class TestFrontendEscaping:
    """直接执行 assets/app.js 里的转义函数，验证恶意输入不会变成标签。"""

    @staticmethod
    def _run(expr: str) -> str:
        script_path = BASE_DIR / "assets" / "app.js"
        src = script_path.read_text(encoding="utf-8")
        # 抽出 escapeHtml / safeUrl 两个纯函数供测试
        harness = src.split("/* ---------------------------------------------------------------- 工具 */")[1]
        harness = harness.split("/* ---------------------------------------------------------- 数据加载 */")[0]
        code = harness + f"\nprocess.stdout.write(String({expr}));"
        result = subprocess.run(
            [NODE, "-e", code], capture_output=True, text=True, timeout=30
        )
        return result.stdout

    def test_script_tag_is_escaped(self):
        out = self._run('escapeHtml("<script>alert(1)</script>")')
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_quotes_escaped(self):
        out = self._run('escapeHtml(\'"><img src=x onerror=alert(1)>\')')
        assert '"><img' not in out
        assert "&quot;" in out

    def test_javascript_url_rejected(self):
        assert self._run('safeUrl("javascript:alert(1)")') == ""

    def test_data_url_rejected(self):
        assert self._run('safeUrl("data:text/html,<script>")') == ""

    def test_https_url_allowed(self):
        out = self._run('safeUrl("https://pubmed.ncbi.nlm.nih.gov/1/")')
        assert out.startswith("https://pubmed.ncbi.nlm.nih.gov/")

    def test_mailto_url_allowed(self):
        """协议白名单与 Python 端 _SAFE_SCHEMES 对齐：mailto 也放行。"""
        assert self._run('safeUrl("mailto:ep@example.com")') == "mailto:ep@example.com"


# ---------------------------------------------------------------------------
# 摘要折叠块：英文摘要 + 中文摘要（AI 翻译）
# ---------------------------------------------------------------------------

@_jscore
class TestFrontendAbstracts:
    """用 node 执行 app.js 里的 renderAbstracts，验证折叠/转义行为。

    摘要区被注释标记切成独立小节，可以直接在 node 里执行；
    这样测的是真实前端代码，而不是在 Python 里重复一份渲染逻辑。
    """

    @staticmethod
    def _render(paper: dict) -> str:
        src = (BASE_DIR / "assets" / "app.js").read_text(encoding="utf-8")
        tools = src.split(
            "/* ---------------------------------------------------------------- 工具 */"
        )[1]
        tools = tools.split(
            "/* ---------------------------------------------------------- 数据加载 */"
        )[0]
        abstracts = src.split(
            "/* ---------------------------------------------------------- 摘要区 */"
        )[1]
        abstracts = abstracts.split(
            "/* ---------------------------------------------------------- AI 解读 */"
        )[0]
        code = tools + abstracts + (
            "\nprocess.stdout.write(renderAbstracts("
            + json.dumps(paper, ensure_ascii=False)
            + "));"
        )
        result = subprocess.run(
            [NODE, "-e", code], capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    def test_english_and_chinese_abstracts_both_collapsed(self):
        html = self._render(
            {
                "title": "T",
                "summary": "Background: PFA is novel.",
                "summary_zh": "背景：脉冲场消融是新技术。",
            }
        )
        assert html.count("<details") == 2
        assert "英文摘要" in html
        assert "中文摘要" in html
        # 两个折叠块默认都必须是收起的
        assert "<details open" not in html
        assert "Background: PFA is novel." in html
        assert "背景：脉冲场消融是新技术。" in html

    def test_chinese_abstract_is_escaped(self):
        html = self._render(
            {
                "summary": "English abstract.",
                "summary_zh": '<img src=x onerror=alert(1)>中文摘要内容。',
            }
        )
        assert "<img" not in html
        assert "&lt;img" in html

    def test_chinese_block_omitted_without_translation(self):
        """老数据 / 翻译失败时不能渲染空的中文摘要块。"""
        html = self._render({"summary": "English abstract."})
        assert html.count("<details") == 1
        assert "中文摘要" not in html

    def test_no_abstract_renders_message_only(self):
        html = self._render({"no_abstract": True, "summary": ""})
        assert "暂无摘要" in html
        assert "<details" not in html


@_jscore
class TestFrontendRenderPaper:
    """renderPaper / searchBlobFor 的 node 行为测试（XSS 回归 + 检索索引契约）。

    renderPaper 曾是唯一没有单测的渲染函数，也是唯一一处把未转义文本
    （authors）直接拼进 HTML 的地方 —— 这里把它固化成回归测试。
    """

    @staticmethod
    def _run(paper: dict) -> dict:
        src = (BASE_DIR / "assets" / "app.js").read_text(encoding="utf-8")
        tools = src.split(
            "/* ---------------------------------------------------------------- 工具 */"
        )[1].split(
            "/* ---------------------------------------------------------- 数据加载 */"
        )[0]
        abstracts = src.split(
            "/* ---------------------------------------------------------- 摘要区 */"
        )[1].split(
            "/* ---------------------------------------------------------- AI 解读 */"
        )[0]
        # AI 解读小节 -> renderPapers 之前：renderAI / searchBlobFor / renderPaper。
        # 用函数名锚点切，避免依赖小节注释的破折号数量。
        render, sep, _ = src.split(
            "/* ---------------------------------------------------------- AI 解读 */"
        )[1].partition("\n    function renderPapers")
        assert sep, "assets/app.js 缺少 function renderPapers"
        code = (
            # renderPaper 只读 state.rangeView；renderAI 读 cfg.aiSections（由
            # site-config 注入的常量）。其余渲染函数在本测试里不会被调用
            "var state = { rangeView: false };\n"
            'var cfg = { aiSections: [["tldr","一句话结论"],["motivation","研究动机"],'
            '["method","方法"],["result","结果"],["conclusion","结论与意义"]] };\n'
            + tools + abstracts + render
            + "\nvar paper = " + json.dumps(paper, ensure_ascii=False) + ";\n"
            + "process.stdout.write(JSON.stringify({"
              "html: renderPaper(paper, 1), blob: searchBlobFor(paper)}));\n"
        )
        result = subprocess.run(
            [NODE, "-e", code], capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def test_authors_are_escaped(self):
        """authors 原始拼接曾是注入口子：恶意作者名不能变成标签。"""
        out = self._run(make_paper(authors=["<img src=x onerror=alert(1)>", "Li Ming"]))
        assert "<img" not in out["html"]
        assert "&lt;img" in out["html"]

    def test_authors_escaped_with_more_span(self):
        out = self._run(make_paper(authors=["<script>alert(1)</script>", "b", "c", "d"]))
        assert "<script>" not in out["html"]
        assert "authors-more" in out["html"]   # 超 3 位作者的折叠提示仍在

    def test_authors_non_array_does_not_crash(self):
        """异常数据（authors 是字符串）静默兜底，不渲染作者行也不抛错。"""
        out = self._run(make_paper(authors="Zhang Wei"))
        assert 'class="paper-authors"' not in out["html"]

    def test_search_blob_uses_raw_fields(self):
        """检索串必须含完整作者名单 / PMID / DOI，且不含 HTML 标记。"""
        paper = make_paper(authors=["A", "B", "C", "Fourth Author"])
        blob = self._run(paper)["blob"]
        assert "fourth author" in blob          # 第 4 位之后的作者也搜得到
        assert paper["pmid"] in blob
        assert paper["doi"] in blob
        assert "authors-more" not in blob       # 不含页面标记，搜 "span" 不会假阳性
        assert "&quot;" not in blob


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCLI:
    def test_main_renders(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "data"
        write_day(data_dir, "2026-10-19", [make_paper()])
        out = tmp_path / "index.html"
        monkeypatch.setenv("LANGUAGE", "Chinese")
        rc = build_html.main(["--data-dir", str(data_dir), "--out", str(out)])
        assert rc == 0
        assert out.exists()
        assert "2026-10-19" in out.read_text(encoding="utf-8")

    def test_missing_data_dir_returns_2(self, tmp_path):
        rc = build_html.main(["--data-dir", str(tmp_path / "nope"), "--out", str(tmp_path / "o.html")])
        assert rc == 2

    def test_cli_module_entrypoint(self, tmp_path):
        data_dir = tmp_path / "data"
        write_day(data_dir, "2026-10-19", [make_paper()])
        out = tmp_path / "index.html"
        result = subprocess.run(
            [sys.executable, "-m", "render.build_html",
             "--data-dir", str(data_dir), "--out", str(out)],
            cwd=str(BASE_DIR), capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stderr
        assert out.exists()
