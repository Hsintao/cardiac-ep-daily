"""数据源注册表。

每个源暴露统一的 ``fetch(cfg, days, today=None, client=None) -> List[Paper]``。
采集器按 ``config.enabled_sources()`` 的顺序调用，单个源抛异常只记日志、
不影响其他源（见 ``dedup``/``collect`` 的失败隔离逻辑）。
"""

from __future__ import annotations

from typing import Callable, Dict

from . import europepmc, pubmed

#: 源名 -> fetch 函数
SOURCES: Dict[str, Callable] = {
    "pubmed": pubmed.fetch,
    "europepmc": europepmc.fetch,
}

__all__ = ["SOURCES", "pubmed", "europepmc"]
