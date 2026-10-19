"""相关性配置的读取（不改动被锁定的 config.py）。

``config.Config.load()`` 已锁定，不包含 ``relevance`` 段。
为了让阈值/开关可在 config.yaml 里调，这里单独把这一段读出来，
并保持与 ``daily_ep.relevance`` 的默认值一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from .config import DEFAULT_CONFIG_PATH
from .relevance import DEFAULT_THRESHOLD


@dataclass
class RelevanceConfig:
    enabled: bool = True
    threshold: float = DEFAULT_THRESHOLD
    require_cardiac_context: bool = True
    whitelist_bypass: bool = True

    @classmethod
    def load(cls, path: Optional[Path | str] = None) -> "RelevanceConfig":
        path = Path(path) if path else DEFAULT_CONFIG_PATH
        raw: Dict[str, Any] = {}
        if path.exists():
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        block = raw.get("relevance") or {}
        if not isinstance(block, dict):
            block = {}
        return cls(
            enabled=bool(block.get("enabled", True)),
            threshold=float(block.get("threshold", DEFAULT_THRESHOLD)),
            require_cardiac_context=bool(block.get("require_cardiac_context", True)),
            whitelist_bypass=bool(block.get("whitelist_bypass", True)),
        )
