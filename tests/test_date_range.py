"""「日期区间选择」测试：模板 / CSS / 构建产物静态契约 + app.js 纯函数行为。

功能背景
--------
站点是单页静态页，``assets/app.js`` 会被内联进 ``index.html``。新增的
「日期区间」能力允许用户选择单日或一个日期区间，区间内所有论文聚合展示、
跨日去重（同一条论文只保留最新收录日），并支持 ``#YYYY-MM-DD`` 与
``#YYYY-MM-DD~YYYY-MM-DD`` 两种 URL hash。

测试分三层（全部离线，不联网）：
  1. **静态契约**：模板里的 id / data-*、CSS 类名、app.js 的小节标记与函数名；
  2. **构建集成**：``build_site`` 产出的 HTML 里确实内联了区间结构与 JS，
     ``site-config`` 的 ``days`` 覆盖临时数据目录里的所有日期；
  3. **纯函数行为**（需要 node，无 node 自动跳过）：用契约规定的小节注释
     标记从 app.js 抽出「日期区间（纯函数）」小节，在 node 里执行后把结果
     以 JSON 回传，Python 侧断言行为，而不是复述实现细节。

设计约束：
  - 本文件自包含（自带 make_paper / write_day / build），不 import 其它测试模块；
  - 不修改 ``assets/app.js``、模板、CSS、``index.html`` —— 只测真实产物；
  - 断言针对行为（归一化、去重保留首次、日期倒序等），不绑定内部实现
    （例如不假设 dedupeKey 的具体字符串格式）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from render import build_html  # noqa: E402

FIXED_TIME = datetime(2026, 10, 19, 21, 30)
NODE = shutil.which("node")

APP_JS_PATH = BASE_DIR / "assets" / "app.js"
STYLE_PATH = BASE_DIR / "assets" / "style.css"
TEMPLATE_PATH = BASE_DIR / "render" / "templates" / "index.html.j2"

# 契约规定的两个小节标记（逐字匹配）。纯函数小节必须位于「数据加载」之前。
RANGE_MARKER = "/* ------------------------------------------------------ 日期区间（纯函数） */"
LOAD_MARKER = "/* ---------------------------------------------------------- 数据加载 */"

PURE_FUNCTIONS = (
    "isDayString",
    "parseHash",
    "daysBetween",
    "rangeLabel",
    "dayShort",
    "dedupeKey",
    "dedupePapers",
    "sortRangePapers",
)

# 区间渲染 / 加载逻辑必须存在的能力入口（行为契约，不绑定内部函数拆分：
# 多日加载既可以在 switchRange 里做，也可以抽成 loadDays）。
RUNTIME_FUNCTIONS = ("switchRange", "updateHash")


# ---------------------------------------------------------------------------
# 自包含辅助函数（不依赖其它测试模块）
# ---------------------------------------------------------------------------

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
    """把 days_map（day -> papers）写成 JSONL，再构建站点壳，返回 HTML。"""
    data_dir = tmp_path / "data"
    for day, papers in days_map.items():
        write_day(data_dir, day, papers)
    out = tmp_path / "index.html"
    build_html.build_site(data_dir, out, generated_at=FIXED_TIME)
    return out.read_text(encoding="utf-8")


def _assert_id(source: str, ident: str) -> None:
    assert re.search(rf'id="{re.escape(ident)}"', source), f"缺少 id={ident}"


def _assert_class(source: str, name: str) -> None:
    assert re.search(
        rf'class="[^"]*\b{re.escape(name)}\b[^"]*"', source
    ), f"缺少 class={name}"


def _read_app_js() -> str:
    return APP_JS_PATH.read_text(encoding="utf-8")


def pure_section() -> str:
    """抽取 app.js 的「日期区间（纯函数）」小节源码。

    缺失标记时 pytest.fail 并给出明确提示（实现未落地 / 标记被改动），
    而不是抛 IndexError 让失败原因难以定位。
    """
    src = _read_app_js()
    start = src.find(RANGE_MARKER)
    assert start >= 0, (
        "assets/app.js 缺少纯函数小节标记，逐字应为：\n" + RANGE_MARKER
    )
    tail = src[start + len(RANGE_MARKER):]
    end = tail.find(LOAD_MARKER)
    assert end >= 0, (
        "assets/app.js 的纯函数小节之后缺少原有标记：\n" + LOAD_MARKER
    )
    return tail[:end]


def run_node_pure(body: str) -> dict:
    """在 node 中执行「纯函数小节 + body」，body 通过 __out 回传结果。

    body 内不要写 process.stdout —— 统一由本函数把 __out 序列化成 JSON。
    纯函数抛异常时会被捕获进 __out.__error，Python 侧转成清晰的断言失败。
    """
    section = pure_section()
    code = (
        section
        + "\nvar __out = {};\n"
        + "try {\n"
        + textwrap.indent(textwrap.dedent(body).strip(), "    ")
        + '\n} catch (e) { __out.__error = String((e && e.stack) || e); }\n'
        + "process.stdout.write(JSON.stringify(__out));\n"
    )
    proc = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (
        "node 执行 app.js 纯函数小节失败（语法错误？）：\n" + (proc.stderr or proc.stdout)
    )
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            "node 输出不是 JSON：" + proc.stdout[:2000] + "\nstderr=" + proc.stderr[:2000]
        )
    if "__error" in out:
        raise AssertionError(f"纯函数执行抛异常：{out['__error']}")
    return out


# ---------------------------------------------------------------------------
# 1) 静态契约：模板
# ---------------------------------------------------------------------------

class TestTemplateContract:
    """模板必须包含区间选择所需的全部挂载点（否则 JS 无从绑定）。"""

    @staticmethod
    def _tpl() -> str:
        return TEMPLATE_PATH.read_text(encoding="utf-8")

    def test_mode_switch_present(self):
        """单日 / 区间模式切换：#modeSingle、#modeRange、.date-mode-btn、data-mode。"""
        tpl = self._tpl()
        _assert_id(tpl, "modeSingle")
        _assert_id(tpl, "modeRange")
        _assert_class(tpl, "date-mode-btn")
        assert 'data-mode="single"' in tpl
        assert 'data-mode="range"' in tpl

    def test_range_bar_and_controls(self):
        """区间工具条及其提示 / 清除按钮 / 计数挂载点。"""
        tpl = self._tpl()
        _assert_id(tpl, "dateRangeBar")
        _assert_id(tpl, "dateRangeHint")
        _assert_id(tpl, "clearRange")
        _assert_id(tpl, "dateToggleCount")
        _assert_class(tpl, "date-mode-switch")
        _assert_class(tpl, "date-range-bar")
        _assert_class(tpl, "date-range-hint")
        _assert_class(tpl, "date-range-clear")

    def test_quick_presets(self):
        """快捷区间：data-preset="7" / data-preset="14"。"""
        tpl = self._tpl()
        assert re.search(r'data-preset="7"', tpl), '缺少 data-preset="7"'
        assert re.search(r'data-preset="14"', tpl), '缺少 data-preset="14"'
        _assert_class(tpl, "date-range-preset")

    def test_chip_days_present(self):
        """顶栏统计新增「本次区间天数」chip：再窄也不能丢。"""
        tpl = self._tpl()
        _assert_id(tpl, "chipDaysWrap")
        _assert_id(tpl, "chipDays")

    def test_date_cells_keep_data_day(self):
        """区间端点靠 data-day 识别，日期格子必须继续带 data-day。"""
        tpl = self._tpl()
        assert 'class="date-cell' in tpl
        assert "data-day=" in tpl


# ---------------------------------------------------------------------------
# 2) 静态契约：CSS
# ---------------------------------------------------------------------------

class TestStyleContract:
    """新结构必须有对应样式，否则页面会「结构在、样式无」。"""

    @staticmethod
    def _css() -> str:
        return STYLE_PATH.read_text(encoding="utf-8")

    def test_mode_switch_classes(self):
        css = self._css()
        for name in (".date-mode-switch", ".date-mode-btn", ".date-toggle-count"):
            assert name in css, f"assets/style.css 缺少 {name}"

    def test_range_bar_view(self):
        css = self._css()
        assert ".date-range-bar" in css
        # 关键契约：隐藏态必须由 [hidden] 控制，不能用 display:flex 覆盖掉
        assert ".date-range-bar[hidden]" in css
        for name in (".date-range-hint", ".date-range-preset", ".date-range-clear"):
            assert name in css, f"assets/style.css 缺少 {name}"

    def test_cell_range_states(self):
        css = self._css()
        for name in (
            ".date-cell.in-range",
            ".date-cell.range-start",
            ".date-cell.range-end",
            ".date-cell.preview",
        ):
            assert name in css, f"assets/style.css 缺少 {name}"

    def test_paper_day_tag_and_warnings(self):
        css = self._css()
        for name in (".tag-day", ".load-warning"):
            assert name in css, f"assets/style.css 缺少 {name}"

    def test_hidden_chips_and_controls_are_explicit(self):
        """带 display 的组件必须显式压回 [hidden]。

        ``.header-stats .chip`` 与 ``.date-range-bar`` 都设置了 display，浏览器
        ``[hidden]`` 自带的 display:none 会被作者样式覆盖；漏写就会出现
        「单日视图仍显示『覆盖 — 天』」这类可见 bug（E2E 曾实测到）。
        """
        css = self._css()
        for selector in (
            r"\.header-stats\s+\.chip\[hidden\]",
            r"\.date-range-bar\[hidden\]",
            r"\.date-toggle-count\[hidden\]",
        ):
            assert re.search(selector, css), f"assets/style.css 缺少显式隐藏规则 {selector}"


# ---------------------------------------------------------------------------
# 3) 静态契约：app.js（标记 / 函数名）
# ---------------------------------------------------------------------------

class TestAppScriptContract:
    def test_pure_section_markers_in_order(self):
        src = _read_app_js()
        start = src.find(RANGE_MARKER)
        assert start >= 0, "缺少纯函数小节标记，逐字应为：\n" + RANGE_MARKER
        end = src.find(LOAD_MARKER, start + len(RANGE_MARKER))
        assert end > start, "纯函数小节之后必须紧跟：\n" + LOAD_MARKER

    def test_pure_functions_defined_in_section(self):
        section = pure_section()
        for name in PURE_FUNCTIONS:
            assert re.search(rf"\bfunction\s+{name}\b", section), (
                f"纯函数小节里缺少 function {name}"
            )

    def test_runtime_functions_present(self):
        src = _read_app_js()
        for name in RUNTIME_FUNCTIONS:
            assert re.search(rf"\b{re.escape(name)}\b", src), (
                f"assets/app.js 缺少区间运行时函数 {name}"
            )
        assert "__day" in src, "assets/app.js 缺少跨日去重用的 __day 标记"


# ---------------------------------------------------------------------------
# 4) 构建集成：临时 data 目录 -> 单页 HTML
# ---------------------------------------------------------------------------

class TestBuildIntegration:
    def test_site_config_days_cover_all_generated_days(self, tmp_path):
        """site-config 的 days 必须覆盖 data 目录里的所有日期（区间选择的数据源）。"""
        days_map = {
            "2026-10-19": [make_paper()],
            "2026-10-01": [make_paper(id="p2", pmid="40000002")],
            "2026-09-20": [make_paper(id="p3", pmid="40000003")],
        }
        html = build(tmp_path, days_map)
        match = re.search(
            r'<script[^>]*id="site-config"[^>]*>(.*?)</script>', html, re.S
        )
        assert match, '页面里找不到 <script id="site-config">'
        config = json.loads(match.group(1))
        assert config["days"] == sorted(days_map, reverse=True)
        assert set(config["days"]) == set(days_map)
        assert config["defaultDate"] == "2026-10-19"

    def test_inline_structure_present(self, tmp_path):
        """构建产物必须内联区间结构（模板被真实渲染，而不只是源码里有）。"""
        html = build(tmp_path, {"2026-10-19": [make_paper()]})
        _assert_id(html, "modeSingle")
        _assert_id(html, "modeRange")
        _assert_id(html, "dateRangeBar")
        _assert_id(html, "dateRangeHint")
        _assert_id(html, "clearRange")
        _assert_id(html, "chipDays")
        assert 'data-preset="7"' in html
        assert 'data-preset="14"' in html

    def test_inline_script_has_range_functions(self, tmp_path):
        """app.js 被内联：区间相关函数名必须出现在产出的 HTML 里。"""
        html = build(tmp_path, {"2026-10-19": [make_paper()]})
        for name in ("switchRange", "daysBetween", "dedupePapers", "parseHash", "__day"):
            assert name in html, f"构建后的 HTML 未内联 {name}（app.js 没被内联？）"


# ---------------------------------------------------------------------------
# 5) node 纯函数行为（无 node 时跳过）
# ---------------------------------------------------------------------------

_jscore = pytest.mark.skipif(NODE is None, reason="需要 node 执行前端纯函数")


@_jscore
class TestParseHash:
    """URL hash 解析：单日 / 区间 / 反向区间归一化 / 非法返回 null。"""

    def test_single_day(self):
        out = run_node_pure('__out.single = parseHash("#2026-10-01");')
        single = out.get("single")
        assert isinstance(single, dict), f"单日 hash 应返回对象，实际 {single!r}"
        assert single.get("mode") == "single"
        assert single.get("day") == "2026-10-01"

    def test_range(self):
        out = run_node_pure('__out.range = parseHash("#2026-10-01~2026-10-05");')
        rng = out.get("range")
        assert isinstance(rng, dict), f"区间 hash 应返回对象，实际 {rng!r}"
        assert rng.get("mode") == "range"
        assert rng.get("start") == "2026-10-01"
        assert rng.get("end") == "2026-10-05"

    def test_reversed_range_is_normalized(self):
        """用户可能先点较晚日期：a>b 必须归一化成 start 较早、end 较晚。"""
        out = run_node_pure('__out.rev = parseHash("#2026-10-05~2026-10-01");')
        rev = out.get("rev")
        assert isinstance(rev, dict), f"反向区间不应被当成非法，实际 {rev!r}"
        assert rev.get("start") == "2026-10-01"
        assert rev.get("end") == "2026-10-05"

    def test_invalid_hash_returns_null(self):
        out = run_node_pure(
            """
            __out.text = parseHash("#hello");
            __out.unpadded = parseHash("#2026-1-5");
            __out.slashed = parseHash("#2026/10/01");
            __out.multi = parseHash("#2026-10-01~2026-10-02~2026-10-03");
            __out.trailing = parseHash("#2026-10-01~");
            __out.empty = parseHash("");
            """
        )
        for key in ("text", "unpadded", "slashed", "multi", "trailing"):
            assert out.get(key) is None, f"{key} 非法 hash 应返回 null，实际 {out.get(key)!r}"
        # 空 hash 的表示允许 null 或 single + 空日期（由调用方回退到 defaultDate）
        empty = out.get("empty")
        assert empty is None or (
            isinstance(empty, dict) and empty.get("mode") == "single" and not empty.get("day")
        ), f"空 hash 应返回 null 或 single/空日期，实际 {empty!r}"


@_jscore
class TestDaysBetween:
    """区间 -> 真实存在的日期列表：含端点、跳过缺失、倒序。"""

    def test_keeps_only_existing_days_and_desc_order(self):
        out = run_node_pure(
            """
            var all = ["2026-10-05", "2026-10-03", "2026-10-01", "2026-09-28"];
            __out.subset = daysBetween("2026-10-01", "2026-10-05", all);
            """
        )
        assert out.get("subset") == ["2026-10-05", "2026-10-03", "2026-10-01"], (
            "区间内只保留真实存在的日期，含两端且倒序"
        )

    def test_missing_endpoints_skipped(self):
        out = run_node_pure(
            """
            var all = ["2026-10-05", "2026-10-03", "2026-10-01", "2026-09-28"];
            __out.partial = daysBetween("2026-09-30", "2026-10-02", all);
            """
        )
        assert out.get("partial") == ["2026-10-01"]

    def test_single_day_range(self):
        out = run_node_pure(
            """
            var all = ["2026-10-05", "2026-10-03", "2026-10-01"];
            __out.same = daysBetween("2026-10-03", "2026-10-03", all);
            """
        )
        assert out.get("same") == ["2026-10-03"]

    def test_range_without_data_is_empty(self):
        out = run_node_pure(
            """
            var all = ["2026-10-05", "2026-10-01"];
            __out.none = daysBetween("2026-11-01", "2026-11-05", all);
            """
        )
        assert out.get("none") == []


@_jscore
class TestLabels:
    def test_range_label_same_year_omits_year(self):
        out = run_node_pure('__out.label = rangeLabel("2026-10-01", "2026-10-05");')
        assert out.get("label") == "10-01 ~ 10-05"

    def test_range_label_cross_year_keeps_year(self):
        out = run_node_pure('__out.label = rangeLabel("2025-12-28", "2026-01-03");')
        assert out.get("label") == "2025-12-28 ~ 2026-01-03"

    def test_range_label_single_day_is_iso(self):
        out = run_node_pure('__out.label = rangeLabel("2026-10-01", "2026-10-01");')
        assert out.get("label") == "2026-10-01"

    def test_range_label_invalid_is_empty(self):
        out = run_node_pure('__out.label = rangeLabel("bad", "2026-10-05");')
        assert out.get("label") == ""

    def test_day_short(self):
        out = run_node_pure(
            """
            __out.ok = dayShort("2026-10-05");
            __out.bad = dayShort("abc");
            __out.empty = dayShort("");
            """
        )
        assert out.get("ok") == "10-05"
        assert out.get("bad") == ""
        assert out.get("empty") == ""


@_jscore
class TestDedupe:
    """跨日去重：key 的优先级与去重「保留首次出现」。"""

    def test_dedupe_key_priority(self):
        out = run_node_pure(
            """
            __out.pmid_with_doi = dedupeKey({pmid: "111", doi: "10.1/x", id: "i1", title: "T"});
            __out.pmid_only = dedupeKey({pmid: "111"});
            __out.pmid_other = dedupeKey({pmid: "222", doi: "10.1/x"});
            __out.doi_upper = dedupeKey({doi: "10.1/ABC"});
            __out.doi_lower = dedupeKey({doi: "10.1/abc"});
            __out.id_one = dedupeKey({id: "id-1"});
            __out.id_two = dedupeKey({id: "id-2"});
            __out.title_a = dedupeKey({title: "Pulsed Field Ablation", journal: "Heart Rhythm"});
            __out.title_b = dedupeKey({title: "pulsed field ablation", journal: "heart rhythm"});
            __out.title_c = dedupeKey({title: "Pulsed Field Ablation", journal: "Europace"});
            """
        )
        assert out.get("pmid_with_doi") == out.get("pmid_only"), "pmid 优先于 doi/id"
        assert out.get("pmid_with_doi") != out.get("pmid_other"), "不同 pmid 不能同 key"
        assert out.get("doi_upper") == out.get("doi_lower"), "doi 比较应忽略大小写"
        assert out.get("id_one") != out.get("id_two"), "id 兜底必须区分不同 id"
        assert out.get("title_a") == out.get("title_b"), "title+journal 指纹应忽略大小写"
        assert out.get("title_a") != out.get("title_c"), "journal 不同不应被判为同一篇"

    def test_dedupe_papers_keeps_first_occurrence_and_order(self):
        out = run_node_pure(
            """
            var list = [
                {pmid: "100", title: "First", doi: "10.1/a"},
                {pmid: "100", title: "Duplicated", doi: "10.1/a"},
                {doi: "10.1/B", title: "DoiOne"},
                {doi: "10.1/b", title: "DoiTwo"},
                {id: "id-9", title: "IdOne"},
                {id: "id-9", title: "IdTwo"},
                {title: "Same Title", journal: "Heart Rhythm"},
                {title: "same title", journal: "heart rhythm"}
            ];
            __out.titles = dedupePapers(list).map(function (p) { return p.title; });
            __out.empty = dedupePapers([]).length;
            __out.single = dedupePapers([{pmid: "1", title: "Only"}]).length;
            """
        )
        assert out.get("titles") == ["First", "DoiOne", "IdOne", "Same Title"], (
            "重复论文保留首次出现，且不改变其余论文的先后顺序"
        )
        assert out.get("empty") == 0
        assert out.get("single") == 1


@_jscore
class TestSortRangePapers:
    def test_sort_by_day_desc_then_topic_then_title(self):
        out = run_node_pure(
            """
            var topicOrder = ["房颤与消融", "室性心律失常", "器械治疗"];
            var papers = [
                {__day: "2026-10-01", topic: "房颤与消融", title: "B"},
                {__day: "2026-10-05", topic: "器械治疗", title: "C"},
                {__day: "2026-10-05", topic: "房颤与消融", title: "Zeta"},
                {__day: "2026-10-05", topic: "房颤与消融", title: "Alpha"},
                {__day: "2026-10-05", topic: "未知主题X", title: "D"},
                {__day: "2026-10-01", topic: "室性心律失常", title: "A"}
            ];
            var sorted = sortRangePapers(papers, topicOrder);
            __out.order = sorted.map(function (p) { return p.title; });
            __out.input_order = papers.map(function (p) { return p.title; });
            __out.is_copy = sorted !== papers;
            """
        )
        assert out.get("order") == ["Alpha", "Zeta", "C", "D", "B", "A"], (
            "排序应为：收录日倒序 -> 主题顺序（未知排最后）-> 标题"
        )
        assert out.get("input_order") == ["B", "C", "Zeta", "Alpha", "D", "A"], (
            "sortRangePapers 不能就地修改入参"
        )
        assert out.get("is_copy") is True, "sortRangePapers 应返回副本"


# ---------------------------------------------------------------------------
# 6) 语法检查（node --check）
# ---------------------------------------------------------------------------

@_jscore
class TestSyntax:
    def test_app_js_syntax(self):
        proc = subprocess.run(
            [NODE, "--check", str(APP_JS_PATH)], capture_output=True, text=True, timeout=60
        )
        assert proc.returncode == 0, "node --check assets/app.js 失败：\n" + proc.stderr

    def test_inlined_app_script_syntax(self, tmp_path):
        """内联进 HTML 的最后一个 <script> 块必须是合法 JS。"""
        html = build(tmp_path, {"2026-10-19": [make_paper()]})
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
        assert scripts, "构建产物里没有任何内联 <script> 块"
        inline = scripts[-1]
        assert "switchRange" in inline or "loadDay" in inline, (
            "最后一个 <script> 块看起来不是 app 脚本"
        )
        script_path = tmp_path / "inline_app.js"
        script_path.write_text(inline, encoding="utf-8")
        proc = subprocess.run(
            [NODE, "--check", str(script_path)], capture_output=True, text=True, timeout=60
        )
        assert proc.returncode == 0, "内联 app 脚本语法错误：\n" + proc.stderr
