"""渲染层：把采集/AI 层产出的 JSONL 变成可直接双击打开的静态日报。

对外只暴露一个入口 :func:`render.build_html.build_pages`（以及 CLI）。
契约来源是 ``daily_ep.schema.Paper.to_dict()``，本层只读不写。
"""

from __future__ import annotations

__all__ = ["build_html"]
