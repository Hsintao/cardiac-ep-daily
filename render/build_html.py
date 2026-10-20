"""生成日报站点壳：单页架构，前端按日期 fetch JSONL 就地渲染。

用法::

    python -m render.build_html --data-dir data --out index.html

设计要点
--------
1. **离线自包含**：样式（assets/style.css）与脚本（assets/app.js）全部内联进
   HTML，不引用任何 CDN / 外链资源，双击打开即可，断网也不掉样式。
2. **安全**：论文文本的转义由前端 escapeHtml 负责（见 app.js），URL 协议
   白名单（http/https/mailto）前后端各一份且保持一致（safeUrl / safe_url）。
3. **单一数据源**：主题顺序与 AI 五段标签来自本模块（TOPIC_ORDER /
   AI_SECTIONS），经 site-config 传给前端，JS 侧不再另抄一份。

本模块不修改也不依赖采集层的内部实现，只扫描 data/ 目录的文件名日期。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

# 契约（分组顺序）的唯一来源；本项目不复制定义，直接引用。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from daily_ep.schema import TOPIC_ORDER  # noqa: E402

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
# 安全
# ---------------------------------------------------------------------------

def safe_url(value: Any) -> str:
    """过滤 URL：只放行 http/https/mailto，其余一律返回空串。

    作为 Jinja 过滤器注册（模板里 ``| safe_url``）；前端的 href 过滤
    在 app.js 的 safeUrl 里，协议白名单与本函数保持一致。
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
        # 主题顺序 / AI 五段标签的唯一来源在本模块，写进 site-config 供前端
        # 读取（app.js 的 TOPIC_ORDER / renderAI），避免 JS 侧再抄一份清单。
        "topics": list(TOPIC_ORDER),
        "ai_sections": [[key, label] for key, label, _css in AI_SECTIONS],
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
