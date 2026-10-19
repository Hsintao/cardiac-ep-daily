"""共享 HTTP 客户端：限速、超时、指数退避重试。

放在这里而不是各数据源里，是因为 NCBI/Europe PMC 的速率限制与重试策略
是全局约束，散落到各个源里很容易写漏（尤其是「忘记 sleep 导致 429」）。

设计要点：
  - 只用标准库 ``urllib.request``：本机 conda 环境依赖极简，requests 虽然在，
    但没有必要为了 GET + 重试引入；也避免云端 workflow 装包失败。
  - ``RateLimiter`` 按「最近 N 次请求的时间戳」做滑动窗口限速，
    而不是简单的固定 sleep —— 官方限制是「每秒不超过 3 次」，
    固定 1/3 秒 sleep 也够用，但滑动窗口能让突发请求更快完成。
  - 只对**可重试**的失败退避：429 / 5xx / 网络超时。
    4xx（除 429）是参数写错了，重试没有意义，直接抛出。
"""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

USER_AGENT = "daily-ep-paper-fetcher/1.0 (https://github.com/daily-ep; mailto:{email})"

# 视为可重试的 HTTP 状态码：限流 + 服务端错误
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class HttpError(RuntimeError):
    """HTTP 请求最终失败（已重试耗尽）。"""

    def __init__(self, message: str, status: Optional[int] = None, url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url


class RateLimiter:
    """滑动窗口限速器。

    ``max_calls`` 次调用 / ``period`` 秒。线程无关（本采集器是单线程顺序请求），
    但如果将来并行化，``acquire`` 用锁包一下即可。
    """

    def __init__(self, max_calls: int = 3, period: float = 1.0) -> None:
        self.max_calls = max(1, int(max_calls))
        self.period = float(period)
        self._calls: deque = deque()

    def acquire(self) -> None:
        now = time.monotonic()
        # 丢掉窗口外的时间戳
        while self._calls and now - self._calls[0] >= self.period:
            self._calls.popleft()
        if len(self._calls) >= self.max_calls:
            sleep_for = self.period - (now - self._calls[0])
            if sleep_for > 0:
                time.sleep(sleep_for)
            now = time.monotonic()
            while self._calls and now - self._calls[0] >= self.period:
                self._calls.popleft()
        self._calls.append(time.monotonic())


def ncbi_rate_limiter(api_key: str = "") -> RateLimiter:
    """NCBI 速率：无 key 每秒 3 次，有 key 每秒 10 次。

    留一点余量（3 而不是 3.0 次/秒的精确边界），避免时钟抖动触发 429。
    """
    return RateLimiter(max_calls=10 if api_key else 3, period=1.0)


class HttpClient:
    """带重试与限速的 GET 客户端。

    参数
    ----
    timeout:      单次请求超时（秒）
    max_retries:  失败后的额外重试次数（总尝试次数 = max_retries + 1）
    rate_limiter: 请求前调用的限速器，None 表示不限速
    user_agent:   UA 字符串
    """

    def __init__(
        self,
        timeout: int = 30,
        max_retries: int = 3,
        rate_limiter: Optional[RateLimiter] = None,
        user_agent: Optional[str] = None,
        backoff_base: float = 1.0,
        max_backoff: float = 30.0,
        sleeper=time.sleep,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.rate_limiter = rate_limiter
        self.user_agent = user_agent or "daily-ep-paper-fetcher/1.0"
        self.backoff_base = backoff_base
        self.max_backoff = max_backoff
        # 注入 sleep 便于测试：测试里不想真的等 1+2+4 秒
        self._sleep = sleeper

    # ------------------------------------------------------------------
    def build_url(self, base: str, params: Optional[Dict[str, Any]] = None) -> str:
        """拼接查询串。值为 None 的参数会被丢弃。"""
        if not params:
            return base
        clean = {k: v for k, v in params.items() if v is not None}
        separator = "&" if urllib.parse.urlparse(base).query else "?"
        return f"{base}{separator}{urllib.parse.urlencode(clean)}"

    def get(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> str:
        """GET 并返回文本。失败时抛 :class:`HttpError`。"""
        full_url = self.build_url(url, params)
        request_headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json, application/xml, text/xml, */*",
        }
        request_headers.update(headers or {})

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            if self.rate_limiter is not None:
                self.rate_limiter.acquire()
            try:
                req = urllib.request.Request(full_url, headers=request_headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    charset = response.headers.get_content_charset() or "utf-8"
                    return response.read().decode(charset, errors="replace")
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in RETRYABLE_STATUS:
                    raise HttpError(
                        f"HTTP {exc.code} for {full_url}", status=exc.code, url=full_url
                    ) from exc
                self._backoff(attempt, full_url, f"HTTP {exc.code}", exc.headers)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                self._backoff(attempt, full_url, f"{type(exc).__name__}: {exc}", None)

        raise HttpError(
            f"请求重试 {self.max_retries} 次后仍失败: {full_url} ({last_error})",
            url=full_url,
        )

    def get_json(
        self, url: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        text = self.get(url, params)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise HttpError(f"响应不是合法 JSON: {url}") from exc

    # ------------------------------------------------------------------
    def _backoff(
        self,
        attempt: int,
        url: str,
        reason: str,
        headers: Optional[Iterable] = None,
    ) -> None:
        """指数退避 + 抖动。

        如果服务端给了 ``Retry-After`` 就听它的（NCBI 限流时会明确告知），
        取两者较大值，避免「自以为退避够了」继续撞限流。
        """
        if attempt >= self.max_retries:
            return
        delay = min(self.max_backoff, self.backoff_base * (2**attempt))
        retry_after = _parse_retry_after(headers)
        if retry_after is not None:
            delay = max(delay, retry_after)
        delay += random.uniform(0, 0.25)  # 抖动，避免多进程同时重试
        logger.warning(
            "请求失败（%s），%.1fs 后重试 %d/%d: %s",
            reason,
            delay,
            attempt + 1,
            self.max_retries,
            url,
        )
        self._sleep(delay)


def _parse_retry_after(headers: Optional[Iterable]) -> Optional[float]:
    """从 HTTP 响应头里取 Retry-After（秒）。取不到返回 None。"""
    if headers is None:
        return None
    try:
        raw = headers.get("Retry-After")
    except AttributeError:
        return None
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def build_client(cfg, source: str = "pubmed", rate_limiter: Optional[RateLimiter] = None) -> HttpClient:
    """按配置构造客户端。

    ``source`` 只用于 UA 文案；速率限制器由调用方决定（PubMed 限制严于 Europe PMC）。
    """
    email = getattr(cfg, "ncbi_email", "") or "anonymous@example.com"
    return HttpClient(
        timeout=getattr(cfg, "timeout", 30),
        max_retries=getattr(cfg, "max_retries", 3),
        rate_limiter=rate_limiter,
        user_agent=USER_AGENT.format(email=email),
    )
