"""pytest 共享 fixture。

⚠️ 所有测试**不得访问网络**：任何外部响应都以内联 fixture 形式写在测试里。
采集层的网络调用被隔离在 ``daily_ep/http.HttpClient``，测试通过注入
假 client（见 ``test_sources.py``）或直接测纯解析函数来避开真实请求。
"""

from __future__ import annotations

import pytest

from daily_ep.config import Config


@pytest.fixture()
def cfg() -> Config:
    """真实 config.yaml。

    刻意读生产配置而不是造假配置：期刊白名单、主题词、topic_rules 的顺序
    都是「实测结论」，用假配置测就等于没测。
    """
    return Config.load()
