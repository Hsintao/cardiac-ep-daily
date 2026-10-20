"""DeepSeek 运行时配置：固定 base_url / 模型名 + 重试策略。

⚠️ 这里的两个常量是本任务的硬性契约，**不要**改回参考项目的旧值：

  OPENAI_BASE_URL = https://api.deepseek.com
      注意**不带 /v1 后缀**。DeepSeek 官方文档给出的 base_url 就是
      ``https://api.deepseek.com``，OpenAI SDK 会在其后拼 ``/chat/completions``。
      带 ``/v1`` 虽然在部分网关下也能通，但官方文档未如此规定，
      实测 ``POST https://api.deepseek.com/chat/completions`` 返回 401
      （= 端点正确、仅缺 Key），所以按官方值固定。
      即使环境变量传了 ``https://api.deepseek.com/v1``，也会被 :func:`normalize_base_url`
      收敛回官方值。

  MODEL_NAME = deepseek-flash
      ``deepseek-chat`` 已不是现行模型名；当前可用的是 ``deepseek-flash``
      与 ``deepseek-v4-pro``。

除这两个值外，本模块与参考实现 runtime.py 的差别：
  - 面向**无 Key 降级**：配置缺失不抛异常，而是返回 is_ready=False，
    由上层自动切换 --no-ai（未提供 Key 的场景必须能跑通）
  - 走 openai SDK 的 ``chat.completions`` + ``response_format=json_object``，
    不用 function_calling，因此关闭思考模式用的是 ``reasoning_effort="none"``
"""

from __future__ import annotations

import os
import random
import re
import time
from typing import Callable, Optional, Tuple
from urllib.parse import urlparse

import httpx

# --------------------------------------------------------------------------
# DeepSeek 固定配置（契约，勿改）
# --------------------------------------------------------------------------

OPENAI_BASE_URL = "https://api.deepseek.com"   # 不带 /v1 后缀
MODEL_NAME = "deepseek-flash"                  # deepseek-chat 已不是现行模型名
ALT_MODEL_NAME = "deepseek-v4-pro"

API_KEY_ENV = "OPENAI_API_KEY"
BASE_URL_ENV = "OPENAI_BASE_URL"
MODEL_NAME_ENV = "MODEL_NAME"

# 官方文档：不建议用特别大的 max_tokens；五段式中文解读 2048 足够，
# 但**摘要翻译**是长输出（英文摘要最长近 5000 字符），加上模型内部的
# token 消耗，4096 实测会触发 finish_reason=length（截断），所以默认给
# 8192；仍被截断时 enhance.py 会按次翻倍重试（上限 32768）。
# 可被 MAX_TOKENS 覆盖。
DEFAULT_MAX_TOKENS = 8192

# 并发上限：官方文档给的账号级并发是 2500，但这是**账号总量**；
# 单日几十到两百篇论文用 4-8 个线程足够，再高只会更容易触发限流。
DEFAULT_MAX_WORKERS = 6
MIN_MAX_WORKERS = 1
MAX_MAX_WORKERS = 16

# --------------------------------------------------------------------------
# 重试策略（默认值与参考实现一致：4 次尝试、2s 起、30s 封顶、±50% 抖动）
# --------------------------------------------------------------------------

DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BASE_DELAY = 2.0
DEFAULT_MAX_DELAY = 30.0
DEFAULT_JITTER = 0.5

MAX_RETRIES_LIMIT = 10
MAX_DELAY_LIMIT = 300.0
MAX_SCHEDULE_LENGTH = 64
# 环境变量能造成的最坏单篇等待（11 次尝试 × 300s × 2 抖动系数）
MAX_WORST_CASE_PAPER_WAIT = MAX_RETRIES_LIMIT * MAX_DELAY_LIMIT * 2.0

_RETRYABLE_MESSAGE_MARKERS = (
    "too many requests",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "connection error",
    "connect error",
    "server disconnected",
    "remoteprotocolerror",
    "temporarily unavailable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "internal server error",
    "overloaded",
    "try again",
    "stream error",
    # DeepSeek 返回空 content 时的现象之一：服务端偶发不返回 body
    "empty response",
    "empty content",
)

_RETRYABLE_PROSE_429_PATTERN = re.compile(r"(?<!\d)429(?!\d)")

_RETRYABLE_STATUS_PATTERN = re.compile(
    r"(?:error\s*code|status(?:\s*code)?|http)\s*[:\s]\s*(429|5\d\d)\b",
    re.IGNORECASE,
)

# 先于可重试标记检查：鉴权失败的响应体里也可能带 "429"
_PERMANENT_MESSAGE_MARKERS = (
    "insufficient balance",
    "insufficient_quota",
    "quota exhausted",
    "exceeded your current quota",
    "invalid api key",
    "incorrect api key",
    "authentication",
    "unauthorized",
    "forbidden",
    "context length",
    "context_length_exceeded",
    "maximum context",
    "too many tokens",
    "model not found",
    "model_not_found",
    "does not exist",
)

_PERMANENT_STATUS_PATTERN = re.compile(
    r"(?:error\s*code|status(?:\s*code)?|http)\s*[:\s]\s*(4(?!29)\d\d)\b",
    re.IGNORECASE,
)

# 用类名匹配而非 import，保证本模块在没装 openai 时也能被导入（测试需要）
_RETRYABLE_EXCEPTION_NAMES = frozenset(
    {
        "APITimeoutError",
        "APIConnectionError",
        "RateLimitError",
        "InternalServerError",
        "APIStatusError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "ReadError",
        "WriteTimeout",
        "WriteError",
        "PoolTimeout",
        "RemoteProtocolError",
        "TimeoutException",
        "Timeout",
        "ConnectionError",
        "ConnectionResetError",
        "ConnectionAbortedError",
        "IncompleteReadError",
        "OSError",
        "ServiceUnavailableError",
    }
)

_PERMANENT_EXCEPTION_NAMES = frozenset(
    {
        "AuthenticationError",
        "PermissionDeniedError",
        "NotFoundError",
        "BadRequestError",
        "UnprocessableEntityError",
    }
)


# --------------------------------------------------------------------------
# 配置读取
# --------------------------------------------------------------------------


def normalize_base_url(url: str) -> str:
    """把 base_url 收敛到官方规定的形式。

    容忍用户/CI 写成 ``https://api.deepseek.com/v1`` 或带尾斜杠的形式，
    统一去掉尾斜杠与 ``/v1`` 后缀 —— 官方文档给的是不带 /v1 的地址，
    而 SDK 会自己拼 ``/chat/completions``，多一层 /v1 会变成
    ``/v1/chat/completions``（DeepSeek 上并非文档端点）。
    仅当主机是 deepseek 时才剥 /v1，避免误伤自建网关。
    """
    if not isinstance(url, str):
        return OPENAI_BASE_URL
    cleaned = url.strip().rstrip("/")
    if not cleaned:
        return OPENAI_BASE_URL
    parsed = urlparse(cleaned)
    hostname = (parsed.hostname or "").lower()
    if hostname == "api.deepseek.com" or hostname.endswith(".deepseek.com"):
        if cleaned.endswith("/v1"):
            cleaned = cleaned[: -len("/v1")].rstrip("/")
    return cleaned


def resolve_config(env: Optional[dict] = None) -> dict:
    """读取运行配置。**不抛异常**：缺 Key 时返回 is_ready=False，由上层降级。

    这是「无 API Key 自动降级 --no-ai」的实现基础：配置缺失是一个
    可预期的正常状态（本地开发、PR 构建），不应该让进程崩掉。
    """
    source = os.environ if env is None else env
    api_key = (source.get(API_KEY_ENV) or "").strip()
    base_url = normalize_base_url(source.get(BASE_URL_ENV) or OPENAI_BASE_URL)
    model_name = (source.get(MODEL_NAME_ENV) or "").strip() or MODEL_NAME
    return {
        "api_key": api_key,
        "base_url": base_url,
        "model_name": model_name,
        # 只有拿到了 Key 才认为可用；其余两项都有官方默认值兜底
        "is_ready": bool(api_key),
        "missing": [] if api_key else [API_KEY_ENV],
    }


def _env_int(name: str, default: int, minimum: int = 0, maximum: Optional[int] = None) -> int:
    """读 int 环境变量；非法回退默认值，过大则夹紧到上限。

    夹紧而不是忽略：写了 ``AI_MAX_RETRIES=999999`` 的人意图是「多试几次」，
    静默回到默认值更令人意外；但绝不能把无上限的值放进重试循环。
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value < minimum:
        return default
    if maximum is not None and value > maximum:
        return maximum
    return value


def _env_float(name: str, default: float, minimum: float = 0.0, maximum: Optional[float] = None) -> float:
    """读 float 环境变量；非法/NaN/inf 回退默认值，过大则夹紧。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value != value or value in (float("inf"), float("-inf")):
        return default
    if value < minimum:
        return default
    if maximum is not None and value > maximum:
        return maximum
    return value


def retry_config() -> dict:
    """重试策略（可用环境变量覆盖，但每一项都有硬上限）。"""
    max_retries = _env_int(
        "AI_MAX_RETRIES", DEFAULT_MAX_ATTEMPTS - 1, minimum=0, maximum=MAX_RETRIES_LIMIT
    )
    base_delay = _env_float(
        "AI_RETRY_BASE_DELAY", DEFAULT_BASE_DELAY, minimum=0.0, maximum=MAX_DELAY_LIMIT
    )
    max_delay = _env_float(
        "AI_RETRY_MAX_DELAY", DEFAULT_MAX_DELAY, minimum=0.0, maximum=MAX_DELAY_LIMIT
    )
    # 上限低于基数会让退避失去意义，抬到基数
    if max_delay < base_delay:
        max_delay = base_delay
    jitter = _env_float("AI_RETRY_JITTER", DEFAULT_JITTER, minimum=0.0, maximum=1.0)
    return {
        # 语义：除首次尝试外**额外**重试几次
        "max_retries": max_retries,
        "base_delay": base_delay,
        "max_delay": max_delay,
        "jitter": jitter,
    }


def request_timeout() -> float:
    """单请求超时。SDK 默认 600s，会让一个挂死连接占住 worker 十分钟。"""
    return _env_float("AI_REQUEST_TIMEOUT", 300.0, minimum=1.0, maximum=1800.0)


def max_tokens() -> int:
    """输出上限。截断响应是五段式丢字段的主因，所以给足余量。"""
    return _env_int("MAX_TOKENS", DEFAULT_MAX_TOKENS, minimum=256, maximum=32768)


def max_workers() -> int:
    """并发线程数（官方账号级并发 2500，本任务 4-8 足够）。"""
    return _env_int(
        "AI_MAX_WORKERS",
        DEFAULT_MAX_WORKERS,
        minimum=MIN_MAX_WORKERS,
        maximum=MAX_MAX_WORKERS,
    )


def reasoning_effort() -> str:
    """思考模式开关。本任务只要结构化摘要，关掉可降本提速。

    DeepSeek 用 ``reasoning_effort="none"`` 表示不思考（OpenAI SDK 会把它
    作为顶层字段发出去）。部分 OpenAI 兼容网关不认识 ``none``，
    此时可用 ``AI_REASONING_EFFORT=off`` 让它降级为不传该字段。
    """
    raw = (os.environ.get("AI_REASONING_EFFORT") or "none").strip().lower()
    if raw in ("off", "omit", "skip", ""):
        return ""
    return raw


# --------------------------------------------------------------------------
# 客户端构造
# --------------------------------------------------------------------------


# NO_PROXY 里出现 ``[::1]`` 这类带方括号的 IPv6 写法时，httpx 会把它交给
# urlparse 解析端口，直接抛 ``InvalidURL: Invalid port: ':1]'``。
# 这是**在构造客户端阶段**就崩，连请求都发不出去，所以必须在建 client 前清理。
_IPV6_BRACKET_RE = re.compile(r"\[[0-9a-fA-F:.]+\]")


def sanitize_no_proxy(value: str) -> str:
    """把 NO_PROXY 里 httpx 无法解析的条目剔掉。

    保留 ``localhost``/``127.0.0.1`` 等有效条目，只移除带方括号的 IPv6 写法
    （``[::1]``、``[::]``）—— 它们对本地服务绕过代理通常并非必需，而会直接
    让 httpx 崩溃。
    """
    if not value:
        return value
    kept = []
    for part in value.split(","):
        item = part.strip()
        if not item:
            continue
        if _IPV6_BRACKET_RE.search(item):
            continue
        kept.append(item)
    return ",".join(kept)


def build_http_client() -> httpx.Client:
    """构造 httpx 客户端，并规避 NO_PROXY 的 IPv6 写法导致的构造期崩溃。

    这里显式读取 proxy 环境变量并自己解析，而不是让 httpx 隐式读：
    httpx 对 ``no_proxy`` 的解析在遇到 ``[::1]`` 时会抛 InvalidURL。
    """
    proxy_env = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or \
        os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or ""
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    clean_no_proxy = sanitize_no_proxy(no_proxy)

    kwargs: dict = {"timeout": request_timeout()}
    if proxy_env:
        kwargs["proxy"] = proxy_env
    # 把清理后的 no_proxy 放回环境：httpx 仍会读环境变量来判定绕过规则，
    # 但此时内容已不再含会让它崩溃的条目。
    if no_proxy:
        os.environ["NO_PROXY"] = clean_no_proxy
        os.environ["no_proxy"] = clean_no_proxy
    return httpx.Client(**kwargs)


def build_client_kwargs(config: dict) -> dict:
    """构造 openai SDK 客户端参数。

    注意 ``base_url`` 不带 ``/v1``：SDK 会拼成 ``{base_url}/chat/completions``，
    即 ``https://api.deepseek.com/chat/completions``。
    """
    return {
        "api_key": config["api_key"],
        "base_url": normalize_base_url(config["base_url"]),
        "timeout": request_timeout(),
        "max_retries": 0,  # 重试由本模块的 invoke_with_retry 统一管理
        # 显式给 http_client：避免 httpx 读取带 [::1] 的 NO_PROXY 时构造失败
        "http_client": build_http_client(),
    }


def _sdk_supports_reasoning_effort() -> bool:
    """当前安装的 openai SDK 是否接受 ``reasoning_effort`` 参数。

    ``reasoning_effort`` 是较新版本 SDK 才有的关键字参数。旧版本（如 1.51.x）
    的 ``Completions.create()`` 没有它，传进去会抛
    ``TypeError: got an unexpected keyword argument 'reasoning_effort'`` ——
    注意这是**本地签名校验**失败，请求根本没发出去，所以不会体现在 HTTP 状态上，
    表现为「所有论文都失败」而不是 4xx。

    因此这里做能力探测：支持才传。关闭思考模式只是省 token 的优化，
    缺了它并不影响功能正确性。
    """
    try:
        import inspect

        from openai.resources.chat.completions import Completions

        return "reasoning_effort" in inspect.signature(Completions.create).parameters
    except Exception:  # noqa: BLE001 - 探测失败时保守地不传
        return False


def build_request_kwargs(model_name: str, messages: list) -> dict:
    """构造 chat.completions 请求体。

    两个 DeepSeek JSON 模式的硬性要求之一在这里落地：
    使用 ``response_format={'type': 'json_object'}`` 时，prompt 里必须含
    "json" 字样并给出格式样例 —— 那是 prompt 的责任（见 ai/prompts/），
    这里只负责正确地打开 JSON 模式。
    """
    kwargs = {
        "model": model_name,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "max_tokens": max_tokens(),
        "temperature": _env_float("AI_TEMPERATURE", 0.3, minimum=0.0, maximum=2.0),
    }
    effort = reasoning_effort()
    if effort and _sdk_supports_reasoning_effort():
        kwargs["reasoning_effort"] = effort
    return kwargs


def create_client(config: dict):
    """按配置创建 openai 客户端。缺 Key 时返回 ``None``（调用方走降级）。"""
    if not config.get("api_key"):
        return None
    from openai import OpenAI

    return OpenAI(**build_client_kwargs(config))


# --------------------------------------------------------------------------
# 重试
# --------------------------------------------------------------------------


def _exception_names(error: BaseException) -> set:
    return {cls.__name__ for cls in type(error).__mro__}


def _exception_status(error: BaseException) -> Optional[int]:
    """尽力从 openai/httpx 风格的异常里取出 HTTP 状态码。"""
    for attribute in ("status_code", "http_status", "code"):
        value = getattr(error, attribute, None)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    return None


def is_retryable_error(error: BaseException) -> bool:
    """判断一个失败值不值得重试（纯函数）。

    可重试：429、5xx、超时、连接重置、服务端 "overloaded"、以及
    **空 content / 截断响应**（由 enhance.py 包装成 EmptyResponseError 抛出，
    属于 DeepSeek 文档明确警告的已知行为，重试是官方建议的做法）。

    不可重试：鉴权失败、Key 无效、余额/配额耗尽、模型名不存在、
    上下文超长、请求参数错误 —— 重试只会浪费配额和时间。
    """
    names = _exception_names(error)
    text = f"{type(error).__name__}: {error}".lower()

    # "insufficient balance" 在 DeepSeek 上可能以 402/400 或通用 APIStatusError
    # 出现，所以消息检查必须**先于**状态码/类名检查
    if _is_retryable_content_error(error):
        return True

    if any(marker in text for marker in _PERMANENT_MESSAGE_MARKERS):
        return False

    status = _exception_status(error)
    if status is not None:
        if status == 429 or 500 <= status < 600:
            return True
        if 400 <= status < 500:
            return False

    if _PERMANENT_STATUS_PATTERN.search(text):
        return False
    if names & _PERMANENT_EXCEPTION_NAMES:
        return False
    if names & _RETRYABLE_EXCEPTION_NAMES:
        return True
    if _RETRYABLE_STATUS_PATTERN.search(text):
        return True
    if any(marker in text for marker in _RETRYABLE_MESSAGE_MARKERS):
        return True
    return bool(_RETRYABLE_PROSE_429_PATTERN.search(text))


class EmptyResponseError(RuntimeError):
    """模型返回了空 content 或无法解析的内容。

    单独成一个异常类型，是因为它的处置方式与网络错误不同：
    两者都重试，但空响应属于「已知的模型行为」，日志里要能一眼分辨。
    """


class UnparseableResponseError(RuntimeError):
    """响应非空但完全解析不出 JSON 对象。"""


def _is_retryable_content_error(error: BaseException) -> bool:
    """空 content / 解析失败属于可重试的已知行为。"""
    return isinstance(error, (EmptyResponseError, UnparseableResponseError))


def _growth_is_capped(base_delay: float, max_delay: float, steps: int) -> bool:
    try:
        return base_delay <= 0.0 or base_delay * (2.0 ** min(steps, 64)) >= max_delay
    except (OverflowError, ValueError):
        return True


def _capped_delay(steps: int, base_delay: float, max_delay: float) -> float:
    """``min(base_delay * 2**steps, max_delay)``，不会溢出。"""
    if base_delay <= 0.0:
        return 0.0
    grown = base_delay
    for _ in range(max(0, int(steps))):
        if grown >= max_delay:
            return max_delay
        grown *= 2.0
    return min(grown, max_delay)


def _sane_delay(value, fallback: float) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    if value != value or value in (float("inf"), float("-inf")):
        return fallback
    return value if value > 0.0 else 0.0


def retry_delays(
    max_retries: int = DEFAULT_MAX_ATTEMPTS - 1,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    jitter: float = DEFAULT_JITTER,
    random_fn: Callable[[float, float], float] = random.uniform,
) -> list:
    """退避时间表（不含抖动），长度与数值都有界。"""
    try:
        count = int(max_retries)
    except (TypeError, ValueError, OverflowError):
        return []
    count = max(0, min(count, MAX_SCHEDULE_LENGTH))
    base_delay = _sane_delay(base_delay, DEFAULT_BASE_DELAY)
    max_delay = _sane_delay(max_delay, DEFAULT_MAX_DELAY)
    return [_capped_delay(attempt, base_delay, max_delay) for attempt in range(count)]


def backoff_delay(
    attempt: int,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    jitter: float = DEFAULT_JITTER,
    random_fn: Callable[[float, float], float] = random.uniform,
) -> float:
    """第 ``attempt`` 次重试（1-based）前的等待时间，含 ± 抖动。

    抖动是为了避免并发 worker 在同一时刻撞上 429 后齐步重试（惊群）。
    """
    try:
        steps = int(attempt) - 1
    except (TypeError, ValueError, OverflowError):
        steps = 0
    base_delay = _sane_delay(base_delay, DEFAULT_BASE_DELAY)
    max_delay = _sane_delay(max_delay, DEFAULT_MAX_DELAY)
    jitter = min(_sane_delay(jitter, DEFAULT_JITTER), 1.0)

    delay = _capped_delay(steps, base_delay, max_delay)
    if jitter > 0 and delay > 0:
        offset = delay * jitter
        try:
            delay = random_fn(max(0.0, delay - offset), delay + offset)
        except (TypeError, ValueError, OverflowError):
            pass
    try:
        result = float(delay)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if result != result or result in (float("inf"), float("-inf")):
        return 0.0
    return max(0.0, result)


def _diagnose(error: BaseException, attempts: int, max_attempts: int) -> str:
    """给日志用的短诊断，指明下一步该改什么。"""
    text = f"{type(error).__name__}: {error}".lower()
    if "insufficient balance" in text or "insufficient_quota" in text:
        return "DeepSeek 账户余额/配额不足，请充值"
    if "invalid api key" in text or "incorrect api key" in text:
        return "API Key 无效，请检查 OPENAI_API_KEY 与 OPENAI_BASE_URL 是否匹配"
    if "authentication" in text or "unauthorized" in text or _exception_status(error) in (401, 403):
        return "鉴权失败，请检查 OPENAI_API_KEY secret"
    if "context length" in text or "maximum context" in text:
        return "输入超过模型上下文，请降低 MAX_TOKENS 或截断摘要"
    if "model not found" in text or "does not exist" in text or _exception_status(error) == 404:
        return f"模型/端点不存在，请检查 MODEL_NAME（应为 {MODEL_NAME}）与 OPENAI_BASE_URL"
    if "finish_reason=length" in text or "max_tokens 截断" in text:
        return (
            f"模型输出被 max_tokens 截断，已重试 {attempts} 次；"
            "可提高 MAX_TOKENS（翻译路径会自动翻倍重试）"
        )
    if isinstance(error, EmptyResponseError):
        return f"模型返回空 content（DeepSeek JSON 模式的已知行为），已重试 {attempts} 次"
    if isinstance(error, UnparseableResponseError):
        return f"响应无法解析为 JSON，已重试 {attempts} 次"
    if is_retryable_error(error):
        return (
            f"provider 瞬时故障，{attempts} 次尝试后放弃（上限 {max_attempts}）；"
            "可降低 --max_workers 或提高 AI_MAX_RETRIES"
        )
    return "provider 永久性失败，重试无益"


def invoke_with_retry(
    call,
    *,
    max_retries: Optional[int] = None,
    base_delay: Optional[float] = None,
    max_delay: Optional[float] = None,
    jitter: Optional[float] = None,
    sleep: Optional[Callable[[float], None]] = None,
    on_retry: Optional[Callable[[int, float, BaseException], None]] = None,
    random_fn: Callable[[float, float], float] = random.uniform,
):
    """调用 ``call()``，对**可重试**错误做有界退避重试。

    ``call`` 是一个零参数可调用对象（已经闭包好请求参数），这样测试里
    可以直接传一个计数器函数，不需要任何网络或 mock 框架。

    纯控制流：不 import provider SDK、不共享全局状态、线程安全
    （每次调用有自己的尝试计数）。``sleep`` 可注入，测试永不真正等待；
    不可重试的错误在第一次就抛出。

    ``sleep`` 默认在调用时解析 ``time.sleep``（而不是做默认参数），
    这样测试里 monkeypatch 才真的生效。
    """
    if sleep is None:
        sleep = time.sleep
    config = retry_config()
    if max_retries is None:
        max_retries = config["max_retries"]
    if base_delay is None:
        base_delay = config["base_delay"]
    if max_delay is None:
        max_delay = config["max_delay"]
    if jitter is None:
        jitter = config["jitter"]

    max_attempts = max(1, int(max_retries) + 1)

    for attempt in range(1, max_attempts + 1):
        try:
            return call()
        except BaseException as error:  # noqa: BLE001 - 下面会重新抛出
            retryable = is_retryable_error(error)
            if not retryable or attempt >= max_attempts:
                # 把诊断信息挂到异常上，方便上层日志直接引用
                try:
                    error.dsh_attempts = attempt
                    error.dsh_max_attempts = max_attempts
                    error.dsh_diagnosis = _diagnose(error, attempt, max_attempts)
                except Exception:
                    pass
                raise
            delay = backoff_delay(
                attempt,
                base_delay=base_delay,
                max_delay=max_delay,
                jitter=jitter,
                random_fn=random_fn,
            )
            if on_retry is not None:
                try:
                    on_retry(attempt, delay, error)
                except Exception:
                    pass
            sleep(delay)

    raise RuntimeError("retry loop made no attempt")


def describe_failure(item_id: str, error: BaseException) -> str:
    """构造给日志看的、可执行的单篇失败说明。"""
    attempts = getattr(error, "dsh_attempts", 1)
    max_attempts = getattr(error, "dsh_max_attempts", attempts)
    diagnosis = getattr(error, "dsh_diagnosis", None) or _diagnose(error, attempts, max_attempts)
    return (
        f"论文 {item_id} 的 AI 请求失败 "
        f"[{type(error).__name__}，第 {attempts}/{max_attempts} 次尝试]：{diagnosis}"
    )


__all__ = [
    "ALT_MODEL_NAME",
    "API_KEY_ENV",
    "BASE_URL_ENV",
    "DEFAULT_MAX_WORKERS",
    "EmptyResponseError",
    "MODEL_NAME",
    "MODEL_NAME_ENV",
    "OPENAI_BASE_URL",
    "UnparseableResponseError",
    "backoff_delay",
    "build_client_kwargs",
    "build_request_kwargs",
    "create_client",
    "describe_failure",
    "invoke_with_retry",
    "is_retryable_error",
    "max_tokens",
    "max_workers",
    "normalize_base_url",
    "reasoning_effort",
    "request_timeout",
    "resolve_config",
    "retry_config",
    "retry_delays",
]
