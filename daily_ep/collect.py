"""采集层 CLI 入口。

    python -m daily_ep.collect --days 2 --out data/2026-10-19.jsonl

行为约定：
  - **单源失败隔离**：某个源抛异常只记 warning，其余源继续；
    只有「所有源都失败」才以非 0 退出码结束，让 GitHub Actions 能报警。
  - **不写网络调用到测试里**：所有解析逻辑都在 ``sources/*.py`` 的纯函数里，
    本模块只做编排。
  - 输出 JSONL 每行一个论文对象，字段与 ``schema.Paper.to_dict()`` 一致，
    且 JSONL 默认 ``ensure_ascii=False``，中文标签在文件里可读。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import dedup as dedup_mod
from .config import Config
from .schema import Paper

logger = logging.getLogger("daily_ep.collect")


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------

def collect_from_sources(
    cfg,
    days: int,
    today: Optional[date] = None,
    sources: Optional[Sequence[str]] = None,
    client_factory=None,
    return_stats: bool = False,
):
    """按优先级逐个源采集，返回 ``(去重后的论文, 各源状态)``。

    ``client_factory`` 用于测试注入假客户端：
    ``client_factory(source_name) -> HttpClient``。
    ``return_stats=True`` 时额外返回相关性过滤统计（CLI 用它打印过滤前后篇数）。
    """
    from .sources import SOURCES

    names = list(sources) if sources is not None else cfg.enabled_sources()
    names = [n for n in names if n in SOURCES]

    collected: List[Paper] = []
    status: List[Tuple[str, int, Optional[str]]] = []

    for name in names:
        try:
            fetcher = SOURCES[name]
            kwargs: Dict[str, object] = {"days": days, "today": today}
            if client_factory is not None:
                kwargs["client"] = client_factory(name)
            papers = fetcher(cfg, **kwargs)  # type: ignore[arg-type]
            collected.extend(papers)
            status.append((name, len(papers), None))
            logger.info("源 %s 采集到 %d 篇", name, len(papers))
        except Exception as exc:  # noqa: BLE001 —— 失败隔离是刻意设计
            status.append((name, 0, str(exc)))
            logger.warning("源 %s 采集失败（已隔离，继续其他源）: %s", name, exc)

    papers = dedup_mod.deduplicate(collected, cfg)
    # 非研究性文献过滤放在**最前面**：勘误/撤稿类即使主题对口也毫无内容，
    # 且不受期刊白名单豁免（白名单期刊也会出勘误）。必须先于 relevance，
    # 否则白名单直通会把勘误放回来。
    papers, nonresearch_stats = filter_non_research(papers)
    # 相关性过滤放在去重**之后**：先去重能避免同一篇被两个源各算一次
    papers, relevance_stats = apply_relevance_filter(papers, cfg)
    papers = dedup_mod.sort_papers(papers)
    papers = dedup_mod.top_n(papers, getattr(cfg, "max_papers", 0))
    if return_stats:
        return papers, status, relevance_stats, nonresearch_stats
    return papers, status


def filter_non_research(papers: Sequence[Paper]):
    """过滤勘误/撤稿/无内容指针类文献。返回 ``(保留的论文, 统计)``。

    实现在 :mod:`daily_ep.pubtypes` —— 判的是「有没有内容」，与相关性无关。
    """
    from .pubtypes import filter_non_research as _filter

    return _filter(papers)


def apply_relevance_filter(papers: Sequence[Paper], cfg=None):
    """按 config.yaml 的 ``relevance`` 段过滤。返回 ``(保留的论文, 统计)``。

    阈值与开关来自 :class:`daily_ep.relevance_config.RelevanceConfig`，
    不改动被锁定的 ``config.Config``。
    """
    from .relevance import filter_papers
    from .relevance_config import RelevanceConfig

    rc = RelevanceConfig.load()
    return filter_papers(
        papers,
        threshold=rc.threshold,
        journals=getattr(cfg, "journals", None) if rc.whitelist_bypass else None,
        enabled=rc.enabled,
        require_cardiac_context=rc.require_cardiac_context,
    )


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def default_output_path(target: Optional[date] = None) -> Path:
    target = target or date.today()
    return Path("data") / f"{target.isoformat()}.jsonl"


def write_jsonl(papers: Sequence[Paper], path: Path | str) -> Path:
    """写 JSONL。父目录自动创建；覆盖写（同一天重跑幂等）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for paper in papers:
            handle.write(json.dumps(paper.to_dict(), ensure_ascii=False) + "\n")
    return path


def read_jsonl(path: Path | str) -> List[Paper]:
    """读回 JSONL（供 AI 层/渲染层与测试使用）。"""
    papers: List[Paper] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            papers.append(Paper.from_dict(json.loads(line)))
    return papers


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m daily_ep.collect",
        description="采集心脏电生理相关论文，输出符合 daily_ep.schema 契约的 JSONL。",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="回溯天数（今天 + 前几天，默认取 config.lookback_days+1）",
    )
    parser.add_argument("--out", default=None, help="输出路径，默认 data/YYYY-MM-DD.jsonl")
    parser.add_argument("--config", default=None, help="配置文件路径，默认仓库根 config.yaml")
    parser.add_argument(
        "--sources",
        default=None,
        help="逗号分隔的源名，覆盖配置（如 pubmed,europepmc）",
    )
    parser.add_argument("--date", default=None, help="把「今天」固定成某天 YYYY-MM-DD（补数据用）")
    parser.add_argument("--verbose", "-v", action="store_true", help="输出 DEBUG 日志")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = Config.load(args.config)
    today = date.fromisoformat(args.date) if args.date else date.today()

    # config.lookback_days=1 表示「除今天外再往前 1 天」→ 窗口长度 2
    days = args.days if args.days is not None else max(1, cfg.lookback_days + 1)
    sources = [s.strip() for s in args.sources.split(",") if s.strip()] if args.sources else None

    window_start = today - timedelta(days=days - 1)
    logger.info(
        "开始采集：days=%d（%s ~ %s）sources=%s",
        days,
        window_start.isoformat(),
        today.isoformat(),
        sources or cfg.enabled_sources(),
    )
    papers, status, relevance_stats, nonresearch_stats = collect_from_sources(
        cfg, days, today=today, sources=sources, return_stats=True
    )
    logger.info("源状态: %s", dedup_mod.summarize_sources(status))

    out_path = Path(args.out) if args.out else default_output_path(today)
    write_jsonl(papers, out_path)

    print(f"采集完成：{len(papers)} 篇 → {out_path}")
    for name, count, error in status:
        print(f"  - {name}: {count} 篇" + (f"（失败: {error}）" if error else ""))
    print()
    if nonresearch_stats.dropped:
        print(nonresearch_stats.summary())
        for title, reason, types in nonresearch_stats.dropped:
            print(f"    ✗ {title[:74]}")
            print(f"          {reason} [{types}]")
    print(relevance_stats.summary())
    if relevance_stats.dropped:
        print(f"  丢弃明细（阈值未达标 {relevance_stats.dropped_count} 篇）：")
        for title, score, reason in relevance_stats.dropped:
            print(f"    ✗ [{score:.2f}] {title[:74]}")
            print(f"          原因: {reason}")
    if papers:
        print("\n样例（最多 3 条）:")
        for paper in papers[:3]:
            print(f"  · {paper.title[:90]}")
            print(f"    {paper.journal or '-'} | PMID {paper.pmid or '-'} | {paper.date or '-'}")
    # 只有全部源失败才算失败；部分失败由 Actions 侧看日志决定是否关注
    if status and all(error for _, _, error in status):
        logger.error("所有数据源均失败")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
