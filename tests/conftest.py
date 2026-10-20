"""pytest 共享 fixture。

⚠️ 所有测试**不得访问网络**：任何外部响应都以内联 fixture 形式写在测试里。
采集层的网络调用被隔离在 ``daily_ep/http.HttpClient``，测试通过注入
假 client（见 ``test_sources.py``）或直接测纯解析函数来避开真实请求。
"""

from __future__ import annotations

import pytest

import os

from daily_ep.config import Config


def pytest_configure(config):
    """测试进程里禁止加载 .env。

    开发者本地常有 .env（真实 DeepSeek Key）。若不屏蔽，像
    ``monkeypatch.delenv("OPENAI_API_KEY")`` 这种「模拟无 Key」的用例会被
    .env 中的 Key 复活：断言失败是小事，**真的调用线上 API**、消耗额度、
    让测试变慢且不确定，才是严重问题。
    """
    os.environ["EP_ENHANCE_NO_DOTENV"] = "1"


@pytest.fixture()
def cfg() -> Config:
    """真实 config.yaml。

    刻意读生产配置而不是造假配置：期刊白名单、主题词、topic_rules 的顺序
    都是「实测结论」，用假配置测就等于没测。
    """
    return Config.load()
