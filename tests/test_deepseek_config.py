"""DeepSeek 配置 / 重试策略 / 降级路径的测试。

全部离线：不创建真实客户端（``runtime.create_client`` 只在有 Key 时才会
import openai 并联网），不发任何 HTTP 请求，不需要 API Key。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai import enhance, runtime  # noqa: E402
from ai.structure import AI_FIELDS, Structure, is_meaningful, normalized_ai_dict  # noqa: E402


# ==========================================================================
# 一、配置契约：base_url 不带 /v1、模型名为 deepseek-flash
# ==========================================================================


def test_base_url_has_no_v1_suffix():
    """官方文档规定 base_url 为 https://api.deepseek.com（不带 /v1）。"""
    assert runtime.OPENAI_BASE_URL == "https://api.deepseek.com"
    assert "/v1" not in runtime.OPENAI_BASE_URL
    assert not runtime.OPENAI_BASE_URL.endswith("/")


def test_model_name_is_deepseek_flash():
    assert runtime.MODEL_NAME == "deepseek-flash"
    # deepseek-chat 已不是现行模型名，不能再作为默认值出现
    assert runtime.MODEL_NAME != "deepseek-chat"


def test_default_config_uses_official_values():
    config = runtime.resolve_config({})
    assert config["base_url"] == "https://api.deepseek.com"
    assert "/v1" not in config["base_url"]
    assert config["model_name"] == "deepseek-flash"


def test_config_env_override_still_normalizes_v1():
    """CI 里误写成 /v1 也要被收敛回官方值。"""
    config = runtime.resolve_config(
        {"OPENAI_API_KEY": "sk-test", "OPENAI_BASE_URL": "https://api.deepseek.com/v1"}
    )
    assert config["base_url"] == "https://api.deepseek.com"


def test_config_env_with_trailing_slash():
    config = runtime.resolve_config(
        {"OPENAI_API_KEY": "sk-test", "OPENAI_BASE_URL": "https://api.deepseek.com/"}
    )
    assert config["base_url"] == "https://api.deepseek.com"


def test_normalize_base_url_leaves_other_hosts_alone():
    """自建网关可能真的需要 /v1，不能被误伤。"""
    assert (
        runtime.normalize_base_url("https://my-gateway.example.com/v1")
        == "https://my-gateway.example.com/v1"
    )
    assert runtime.normalize_base_url("https://api.openai.com/v1") == "https://api.openai.com/v1"


def test_normalize_base_url_handles_empty_and_garbage():
    assert runtime.normalize_base_url("") == runtime.OPENAI_BASE_URL
    assert runtime.normalize_base_url("   ") == runtime.OPENAI_BASE_URL
    assert runtime.normalize_base_url(None) == runtime.OPENAI_BASE_URL


def test_request_path_is_chat_completions():
    """SDK 会在 base_url 后拼 /chat/completions，拼出来必须是文档端点。"""
    config = runtime.resolve_config({"OPENAI_API_KEY": "sk-test"})
    kwargs = runtime.build_client_kwargs(config)
    assert kwargs["base_url"] + "/chat/completions" == "https://api.deepseek.com/chat/completions"


def test_build_client_kwargs_has_no_v1():
    config = runtime.resolve_config({"OPENAI_API_KEY": "sk-test", "OPENAI_BASE_URL": "https://api.deepseek.com/v1"})
    kwargs = runtime.build_client_kwargs(config)
    assert "/v1" not in kwargs["base_url"]
    # 重试由 runtime.invoke_with_retry 统一管理，SDK 自身重试必须关掉
    assert kwargs["max_retries"] == 0
    assert kwargs["timeout"] > 0


def test_request_kwargs_enable_json_mode_and_disable_thinking():
    kwargs = runtime.build_request_kwargs(runtime.MODEL_NAME, [{"role": "user", "content": "hi"}])
    assert kwargs["model"] == "deepseek-flash"
    # DeepSeek JSON 模式的硬性要求之一
    assert kwargs["response_format"] == {"type": "json_object"}
    # 关闭思考模式降本提速
    assert kwargs["reasoning_effort"] == "none"
    assert kwargs["max_tokens"] > 0


def test_reasoning_effort_can_be_omitted(monkeypatch):
    monkeypatch.setenv("AI_REASONING_EFFORT", "off")
    kwargs = runtime.build_request_kwargs(runtime.MODEL_NAME, [])
    assert "reasoning_effort" not in kwargs


def test_model_name_env_override():
    config = runtime.resolve_config({"OPENAI_API_KEY": "sk-test", "MODEL_NAME": runtime.ALT_MODEL_NAME})
    assert config["model_name"] == runtime.ALT_MODEL_NAME


def test_max_workers_is_bounded():
    assert 1 <= runtime.max_workers() <= runtime.MAX_MAX_WORKERS


# ==========================================================================
# 二、无 Key 降级
# ==========================================================================


def test_missing_key_marks_config_not_ready():
    config = runtime.resolve_config({})
    assert config["is_ready"] is False
    assert "OPENAI_API_KEY" in config["missing"]
    # 关键：缺 Key 不抛异常，配置层不允许崩掉
    assert config["base_url"] == "https://api.deepseek.com"


def test_create_client_returns_none_without_key():
    """没 Key 时返回 None 而不是抛异常 —— 降级路径的基础。"""
    assert runtime.create_client({"api_key": "", "base_url": runtime.OPENAI_BASE_URL}) is None


def test_blank_key_is_treated_as_missing():
    assert runtime.resolve_config({"OPENAI_API_KEY": "   "})["is_ready"] is False


def test_cli_no_ai_downgrade_writes_metadata_only(tmp_path, monkeypatch):
    """无 Key 跑 CLI：自动降级 --no-ai，产出只有元数据的文件，且不崩。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    data = tmp_path / "2026-10-19.jsonl"
    items = [
        {
            "id": "pmid:1",
            "title": "Pulsed field ablation vs thermal ablation",
            "summary": "A randomized trial of 200 patients...",
            "journal": "Heart Rhythm",
            "topic": "房颤与消融",
            "date": "2026-10-19",
        },
        {
            "id": "pmid:2",
            "title": "Editorial on mapping",
            "summary": "",
            "no_abstract": True,
            "journal": "Europace",
        },
    ]
    data.write_text("\n".join(json.dumps(i, ensure_ascii=False) for i in items) + "\n", encoding="utf-8")

    exit_code = enhance.main(["--data", str(data)])
    assert exit_code == 0

    target = tmp_path / "2026-10-19_AI_enhanced_Chinese.jsonl"
    assert target.exists()
    written = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(written) == 2
    for entry in written:
        # 只输出元数据：不写任何 AI 字段，更不能写占位符
        assert entry.get("AI") is None
        assert entry["id"]
    assert written[0]["title"] == "Pulsed field ablation vs thermal ablation"


def test_cli_explicit_no_ai_works_with_key_present(tmp_path, monkeypatch):
    """即使有 Key，显式 --no-ai 也必须只输出元数据（绝不发请求）。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-a-real-key")
    data = tmp_path / "2026-10-19.jsonl"
    data.write_text(json.dumps({"id": "pmid:9", "title": "T", "summary": "S"}, ensure_ascii=False) + "\n", encoding="utf-8")

    assert enhance.main(["--data", str(data), "--no-ai"]) == 0
    target = tmp_path / "2026-10-19_AI_enhanced_Chinese.jsonl"
    entry = json.loads(target.read_text(encoding="utf-8").strip())
    assert entry["AI"] is None


def test_cli_missing_input_file_returns_error(tmp_path):
    assert enhance.main(["--data", str(tmp_path / "nope.jsonl")]) == 2


def test_cli_empty_input_file_returns_error(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    data = tmp_path / "2026-10-19.jsonl"
    data.write_text("", encoding="utf-8")
    assert enhance.main(["--data", str(data)]) == 1


def test_output_path_contract(tmp_path):
    assert (
        enhance.output_path_for(tmp_path / "2026-10-19.jsonl").name
        == "2026-10-19_AI_enhanced_Chinese.jsonl"
    )


def test_load_jsonl_skips_bad_lines(tmp_path):
    path = tmp_path / "d.jsonl"
    path.write_text('{"id": 1}\n这不是 json\n\n{"id": 2}\n', encoding="utf-8")
    assert [i["id"] for i in enhance.load_jsonl(path)] == [1, 2]


def test_dedupe_by_id_keeps_first():
    items = [{"id": "a", "n": 1}, {"id": "a", "n": 2}, {"id": "b"}]
    assert [i["n"] for i in enhance.dedupe_by_id(items) if i["id"] == "a"] == [1]
    assert len(enhance.dedupe_by_id(items)) == 2


# ==========================================================================
# 三、JSON 模式 prompt 契约
# ==========================================================================


def test_system_prompt_contains_json_keyword_and_format_sample():
    """"必须含 json 字样并给出格式样例" —— 官方硬性要求，不能靠记忆维护。"""
    system = enhance.load_prompt("system.txt")
    lowered = system.lower()
    assert "json" in lowered
    assert "{" in system and "}" in system
    for field in AI_FIELDS:
        assert field in system


def test_user_prompt_mentions_json():
    user = enhance.load_prompt("user.txt")
    assert "json" in user.lower()
    assert "{abstract}" in user and "{title}" in user


def test_ensure_json_mode_contract_passes_for_real_prompts():
    enhance.ensure_json_mode_contract(enhance.load_prompt("system.txt"), enhance.load_prompt("user.txt"))


def test_ensure_json_mode_contract_rejects_bad_prompt():
    # 连 "json" 字样都没有
    with pytest.raises(ValueError):
        enhance.ensure_json_mode_contract("请分析这篇摘要", "内容：{abstract}")
    # 有 "json" 字样，但只有 {abstract} 这种模板占位符，没有任何格式样例
    with pytest.raises(ValueError):
        enhance.ensure_json_mode_contract("输出 json", "内容：{abstract}")


def test_placeholder_braces_do_not_count_as_format_sample():
    """{} 占位符不是格式样例 —— 这是校验必须区分开的关键点。

    user.txt 本身含有 {abstract}/{title}，若只用「有没有花括号」判断，
    任何 prompt 都会通过校验，等于没校验。
    """
    assert enhance.has_json_format_sample('分析 {abstract} 内容') is False
    assert enhance.has_json_format_sample('输出 {title} {journal}') is False
    assert enhance.has_json_format_sample('请输出 json') is False
    # 真正的样例：带引号的键 + 冒号
    assert enhance.has_json_format_sample('{"tldr": "……"}') is True
    assert enhance.has_json_format_sample("{'tldr': '…'}") is True
    assert enhance.has_json_format_sample('{"motivation": "背景"}') is True
    assert enhance.has_json_format_sample("") is False
    assert enhance.has_json_format_sample(None) is False


def test_ensure_json_mode_contract_accepts_sample_without_braces():
    """样例用单引号或省略外层花括号时也应通过（只要给了键值样例）。"""
    enhance.ensure_json_mode_contract('请输出 json，键为 "tldr": "一句话结论"', "摘要：{abstract}")


def test_prompts_are_clinical_oriented():
    """提示词要面向临床研究：对象、样本量、干预、终点、统计结果。"""
    system = enhance.load_prompt("system.txt")
    for keyword in ("样本量", "终点", "统计", "置信区间", "干预"):
        assert keyword in system


def test_build_messages_renders_placeholders():
    messages = enhance.build_messages(
        {"title": "T", "summary": "S", "journal": "Heart Rhythm", "topic": "房颤与消融"},
        system_text="sys json {...}",
        user_text="json 标题:{title} 期刊:{journal} 主题:{topic} 摘要:{abstract}",
    )
    assert messages[0]["role"] == "system"
    assert "Heart Rhythm" in messages[1]["content"]
    assert "{abstract}" not in messages[1]["content"]
    assert "S" in messages[1]["content"]


def test_build_messages_handles_missing_fields():
    messages = enhance.build_messages({"id": "x"}, system_text="json {}", user_text="{journal}|{topic}|{title}|{abstract}")
    assert messages[1]["content"] == "未提供|未标注||"


# ==========================================================================
# 四、重试策略（纯函数，sleep 可注入，永不真等待）
# ==========================================================================


def _make_error(name: str, message: str = "boom", status: int = None):
    cls = type(name, (Exception,), {})
    error = cls(message)
    if status is not None:
        error.status_code = status
    return error


@pytest.mark.parametrize(
    "name,status,message",
    [
        ("RateLimitError", 429, "rate limit exceeded"),
        ("InternalServerError", 503, "server error"),
        ("APITimeoutError", None, "request timed out"),
        ("APIConnectionError", None, "connection reset by peer"),
        ("InternalServerError", None, "overloaded"),
    ],
)
def test_retryable_errors(name, status, message):
    assert runtime.is_retryable_error(_make_error(name, message, status)) is True


@pytest.mark.parametrize(
    "name,status,message",
    [
        ("AuthenticationError", 401, "invalid api key"),
        ("AuthenticationError", 401, "Authentication Fails"),
        ("PermissionDeniedError", 403, "forbidden"),
        ("NotFoundError", 404, "model not found"),
        ("BadRequestError", 400, "context length exceeded"),
        ("APIStatusError", 402, "insufficient balance"),
        ("APIStatusError", 400, "insufficient_quota"),
    ],
)
def test_permanent_errors(name, status, message):
    assert runtime.is_retryable_error(_make_error(name, message, status)) is False


def test_empty_response_is_retryable():
    """空 content 是 DeepSeek JSON 模式的已知行为，官方建议重试。"""
    assert runtime.is_retryable_error(runtime.EmptyResponseError("空 content")) is True
    assert runtime.is_retryable_error(runtime.UnparseableResponseError("无法解析")) is True


def test_auth_error_quoting_429_is_still_permanent():
    """鉴权失败的响应体里可能带 429，不能因此被当成限流去重试。"""
    error = _make_error("AuthenticationError", "invalid api key (error code: 429)", 401)
    assert runtime.is_retryable_error(error) is False


def test_prose_429_not_confused_with_ids():
    assert runtime.is_retryable_error(_make_error("APIError", "received 429 from provider")) is True
    assert runtime.is_retryable_error(_make_error("APIError", "paper id 1429 not found")) is False


def test_retry_delays_grow_then_cap():
    delays = runtime.retry_delays(5, base_delay=2.0, max_delay=30.0)
    assert delays == [2.0, 4.0, 8.0, 16.0, 30.0]
    assert all(d <= 30.0 for d in delays)


def test_retry_delays_are_bounded_for_absurd_input():
    """AI_MAX_RETRIES=999999 这类输入不能让退避表或等待时间失控。"""
    delays = runtime.retry_delays(10 ** 9, base_delay=2.0, max_delay=30.0)
    assert len(delays) == runtime.MAX_SCHEDULE_LENGTH
    assert max(delays) == 30.0


def test_retry_delays_never_overflows():
    """退避在极端 attempt 下不能抛 OverflowError，且必须落在有界区间内。

    注意抖动是**乘在封顶延迟之上的**：``backoff_delay`` 先算
    ``min(base * 2**(n-1), max_delay)``（=30s），再按 jitter=0.5 取
    ``[delay-offset, delay+offset]`` = [15s, 45s]。所以上界是 45s 而不是 30s，
    这是设计如此（抖动用来打散并发 worker 的重试时刻）；测试断言的是
    「不溢出 + 落在抖动区间内」，而不是把抖动误当成不生效。
    """
    assert runtime._capped_delay(100000, 2.0, 30.0) == 30.0
    upper = runtime.backoff_delay(100000, random_fn=lambda a, b: b)
    lower = runtime.backoff_delay(100000, random_fn=lambda a, b: a)
    capped = 30.0
    assert upper == capped * (1 + runtime.DEFAULT_JITTER)   # 45.0
    assert lower == capped * (1 - runtime.DEFAULT_JITTER)   # 15.0
    # 任何抖动取值都必须落在 [lower, upper] 内，且远低于环境变量能造成的硬上限
    for _ in range(200):
        delay = runtime.backoff_delay(100000)
        assert lower <= delay <= upper
    assert upper < runtime.MAX_WORST_CASE_PAPER_WAIT


def test_retry_delay_is_bounded_even_with_zero_jitter():
    """jitter=0 时退避严格等于封顶值，方便确定性测试与调优。"""
    assert runtime.backoff_delay(100000, jitter=0.0) == 30.0
    assert runtime.backoff_delay(100000, jitter=0.0, max_delay=7.0, base_delay=1.0) == 7.0


def test_retry_config_clamps_hostile_env(monkeypatch):
    monkeypatch.setenv("AI_MAX_RETRIES", "999999")
    monkeypatch.setenv("AI_RETRY_MAX_DELAY", "99999")
    monkeypatch.setenv("AI_RETRY_JITTER", "77")
    config = runtime.retry_config()
    assert config["max_retries"] == runtime.MAX_RETRIES_LIMIT
    assert config["max_delay"] == runtime.MAX_DELAY_LIMIT
    assert config["jitter"] == 1.0


def test_retry_config_ignores_invalid_env(monkeypatch):
    monkeypatch.setenv("AI_MAX_RETRIES", "abc")
    monkeypatch.setenv("AI_RETRY_BASE_DELAY", "nan")
    config = runtime.retry_config()
    assert config["max_retries"] == runtime.DEFAULT_MAX_ATTEMPTS - 1
    assert config["base_delay"] == runtime.DEFAULT_BASE_DELAY


def test_invoke_with_retry_retries_then_succeeds():
    attempts = {"n": 0}
    slept: list = []

    def call():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _make_error("RateLimitError", "429 too many requests", 429)
        return "ok"

    result = runtime.invoke_with_retry(
        call, max_retries=3, base_delay=0.01, jitter=0.0, sleep=slept.append
    )
    assert result == "ok"
    assert attempts["n"] == 3
    assert len(slept) == 2  # 只等了两次


def test_invoke_with_retry_gives_up_after_max_attempts():
    attempts = {"n": 0}

    def call():
        attempts["n"] += 1
        raise _make_error("RateLimitError", "429", 429)

    with pytest.raises(Exception) as excinfo:
        runtime.invoke_with_retry(call, max_retries=2, base_delay=0.0, jitter=0.0, sleep=lambda _s: None)
    assert attempts["n"] == 3
    # 诊断信息挂在异常上，便于日志直接引用
    assert excinfo.value.dsh_attempts == 3
    assert "重试" in excinfo.value.dsh_diagnosis or "瞬时" in excinfo.value.dsh_diagnosis


def test_invoke_with_retry_does_not_retry_permanent_errors():
    attempts = {"n": 0}

    def call():
        attempts["n"] += 1
        raise _make_error("AuthenticationError", "invalid api key", 401)

    with pytest.raises(Exception):
        runtime.invoke_with_retry(call, max_retries=5, sleep=lambda _s: None)
    assert attempts["n"] == 1  # 一次就放弃，不浪费配额


def test_invoke_with_retry_empty_content_is_retried():
    attempts = {"n": 0}

    def call():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise runtime.EmptyResponseError("空 content")
        return {"tldr": "ok"}

    assert runtime.invoke_with_retry(call, max_retries=2, base_delay=0.0, jitter=0.0, sleep=lambda _s: None) == {"tldr": "ok"}
    assert attempts["n"] == 2


def test_on_retry_callback_is_called_and_failures_ignored():
    calls: list = []

    def call():
        raise _make_error("RateLimitError", "429", 429)

    def broken_callback(attempt, delay, error):
        raise RuntimeError("callback 自己坏了不该影响主流程")

    with pytest.raises(Exception):
        runtime.invoke_with_retry(
            call, max_retries=1, base_delay=0.0, jitter=0.0,
            sleep=lambda _s: None, on_retry=broken_callback,
        )

    def good_callback(attempt, delay, error):
        calls.append(attempt)

    def call2():
        raise _make_error("RateLimitError", "429", 429)

    with pytest.raises(Exception):
        runtime.invoke_with_retry(
            call2, max_retries=2, base_delay=0.0, jitter=0.0,
            sleep=lambda _s: None, on_retry=good_callback,
        )
    assert calls == [1, 2]


def test_describe_failure_is_actionable():
    error = _make_error("AuthenticationError", "invalid api key", 401)
    message = runtime.describe_failure("pmid:123", error)
    assert "pmid:123" in message
    assert "API Key" in message or "鉴权" in message


# ==========================================================================
# 五、五段式结构与「禁止占位符」
# ==========================================================================


def test_structure_has_exactly_five_fields():
    assert set(Structure.model_fields) == set(AI_FIELDS)
    assert AI_FIELDS == ("tldr", "motivation", "method", "result", "conclusion")


def test_structure_to_ai_dict():
    model = Structure(tldr="A", motivation="B", method="C", result="D", conclusion="E")
    assert model.to_ai_dict() == {"tldr": "A", "motivation": "B", "method": "C", "result": "D", "conclusion": "E"}


def test_structure_coerces_non_string_values():
    """模型偶尔把字段写成数字/列表，不该因此丢掉整条响应。"""
    model = Structure(tldr=123, motivation=["a", "b"], method=None, result={"x": "y"}, conclusion="正常")
    values = model.to_ai_dict()
    assert values["tldr"] == "123"
    assert "a" in values["motivation"] and "b" in values["motivation"]
    assert values["method"] == ""
    assert values["result"] == "y"


def test_structure_strips_whitespace():
    model = Structure(tldr="  A  ", motivation=" B ", method="C", result="D", conclusion="E")
    assert model.to_ai_dict()["tldr"] == "A"


def test_is_meaningful_rejects_placeholder_like_garbage():
    assert is_meaningful({"tldr": "有效内容"}) is True
    assert is_meaningful({}) is False
    assert is_meaningful({"tldr": "", "method": "  "}) is False
    assert is_meaningful({"tldr": "."}) is False
    assert is_meaningful(None) is False
    # 五个字段里只有一个有效，也应判为可用（摘要里确实可能没报告统计结果）
    assert is_meaningful({"tldr": "有效", "result": "", "method": ""}) is True


def test_normalized_ai_dict_fills_missing_with_empty_not_placeholder():
    normalized = normalized_ai_dict({"tldr": "A"})
    assert set(normalized) == set(AI_FIELDS)
    for name in ("motivation", "method", "result", "conclusion"):
        assert normalized[name] == ""  # 空串，而不是 "生成失败" 之类占位符


# ==========================================================================
# 六、单篇处理：跳过 / 失败 / 不写占位符
# ==========================================================================


class _FakeClient:
    """最小的假客户端：按脚本依次返回 content，不发任何网络请求。"""

    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = 0

        class _Completions:
            def __init__(self, outer):
                self._outer = outer

            def create(self, **kwargs):
                self._outer.last_kwargs = kwargs
                index = min(self._outer.calls, len(self._outer.contents) - 1)
                content = self._outer.contents[index]
                self._outer.calls += 1
                message = type("M", (), {"content": content})()
                return type("R", (), {"choices": [type("C", (), {"message": message})()]})()

        self.chat = type("Chat", (), {"completions": _Completions(self)})()


GOOD_JSON = json.dumps(
    {
        "tldr": "脉冲场消融不劣于射频消融",
        "motivation": "既往证据不足",
        "method": "多中心 RCT，纳入 200 例",
        "result": "12 个月无复发率 78% vs 71%，P=0.04",
        "conclusion": "支持 PFA 作为一线选择",
    },
    ensure_ascii=False,
)


def _config():
    return runtime.resolve_config({"OPENAI_API_KEY": "sk-fake"})


def test_no_abstract_item_is_skipped_without_calling_model():
    client = _FakeClient([GOOD_JSON])
    item = {"id": "pmid:1", "title": "Editorial", "summary": "", "no_abstract": True}
    result, error = enhance.enhance_item(client, _config(), item)
    assert error is None
    assert result is not None
    assert result.get("AI") is None  # 没有 AI 字段，页面不会被污染
    assert client.calls == 0


def test_empty_summary_is_treated_as_no_abstract():
    client = _FakeClient([GOOD_JSON])
    result, error = enhance.enhance_item(client, _config(), {"id": "x", "title": "T", "summary": "   "})
    assert error is None
    assert result["no_abstract"] is True
    assert client.calls == 0


def test_successful_enhancement_writes_five_fields():
    client = _FakeClient([GOOD_JSON])
    result, error = enhance.enhance_item(
        client, _config(), {"id": "pmid:2", "title": "T", "summary": "Abstract text."}
    )
    assert error is None
    assert set(result["AI"]) == set(AI_FIELDS)
    assert all(result["AI"][f] for f in AI_FIELDS)
    assert client.calls == 1
    # 请求确实开了 JSON 模式
    assert client.last_kwargs["response_format"] == {"type": "json_object"}
    assert client.last_kwargs["model"] == "deepseek-flash"


def test_empty_content_then_success_is_retried():
    """文档明确警告可能返回空 content —— 必须重试并救回。"""
    client = _FakeClient(["", GOOD_JSON])
    result, error = enhance.enhance_item(client, _config(), {"id": "p", "title": "T", "summary": "S"})
    assert error is None
    assert result["AI"]["tldr"].startswith("脉冲场消融")
    assert client.calls == 2


def test_persistent_empty_content_fails_without_placeholder(monkeypatch):
    """重试用尽后必须**失败**，绝不能写占位符当摘要。"""
    monkeypatch.setenv("AI_MAX_RETRIES", "1")
    monkeypatch.setenv("AI_RETRY_BASE_DELAY", "0")
    client = _FakeClient(["", ""])
    result, error = enhance.enhance_item(client, _config(), {"id": "pmid:3", "title": "T", "summary": "S"})
    assert result is None
    assert error is not None
    assert "pmid:3" in error
    assert client.calls == 2  # 首次 + 1 次重试


def test_unparseable_response_is_retried_then_recovers():
    client = _FakeClient(["这不是 json", GOOD_JSON])
    result, error = enhance.enhance_item(client, _config(), {"id": "p", "title": "T", "summary": "S"})
    assert error is None
    assert result["AI"]["method"]
    assert client.calls == 2


def test_latex_response_is_repaired_not_retried():
    """LaTeX 非法转义应被纯函数修复，不该浪费一次重试。"""
    content = (
        r'{"tldr": "PVI 有效", "motivation": "背景", "method": "\mathcal{AF} 患者 120 例", '
        r'"result": "HR 0.62", "conclusion": "支持"}'
    )
    client = _FakeClient([content])
    result, error = enhance.enhance_item(client, _config(), {"id": "p", "title": "T", "summary": "S"})
    assert error is None
    assert r"\mathcal{AF}" in result["AI"]["method"]
    assert client.calls == 1


def test_all_empty_fields_response_is_rejected():
    client = _FakeClient([json.dumps({"tldr": "", "motivation": "", "method": "", "result": "", "conclusion": ""})])
    result, error = enhance.enhance_item(
        client, _config(), {"id": "pmid:4", "title": "T", "summary": "S"}
    )
    assert result is None
    assert error is not None


def test_extra_fields_from_model_are_ignored():
    content = json.dumps({**json.loads(GOOD_JSON), "code_url": "https://github.com/x/y", "unexpected": 1})
    client = _FakeClient([content])
    result, error = enhance.enhance_item(client, _config(), {"id": "p", "title": "T", "summary": "S"})
    assert error is None
    assert set(result["AI"]) == set(AI_FIELDS)


def test_partial_response_keeps_available_fields():
    """截断响应只拿到部分字段时，也要保留已有内容而不是整条丢弃。"""
    content = '{"tldr": "房颤消融", "motivation": "背景", "method": "RCT 纳入 120 例'
    client = _FakeClient([content])
    result, error = enhance.enhance_item(client, _config(), {"id": "p", "title": "T", "summary": "S"})
    assert error is None
    assert result["AI"]["tldr"] == "房颤消融"
    assert result["AI"]["conclusion"] == ""  # 缺失字段是空串，不是占位符


def test_client_none_returns_error_not_placeholder():
    result, error = enhance.enhance_item(None, _config(), {"id": "p", "title": "T", "summary": "S"})
    assert result is None
    assert error and "API Key" in error


def test_extract_content_handles_malformed_responses():
    assert enhance.extract_content(None) == ""
    assert enhance.extract_content(type("R", (), {"choices": []})()) == ""
    message = type("M", (), {"content": None})()
    response = type("R", (), {"choices": [type("C", (), {"message": message})()]})()
    assert enhance.extract_content(response) == ""


def test_process_items_drops_failures_but_keeps_successes(monkeypatch):
    monkeypatch.setenv("AI_MAX_RETRIES", "0")
    # 第二篇永远返回空内容 → 失败并被剔除；第一篇成功
    client = _FakeClient([GOOD_JSON, ""])
    items = [
        {"id": "a", "title": "T", "summary": "S"},
        {"id": "b", "title": "T2", "summary": "S2"},
    ]
    processed, errors = enhance.process_items(
        client, _config(), items, max_workers=1, system_text="json {}", user_text="{abstract}"
    )
    assert [entry["id"] for entry in processed] == ["a"]
    assert len(errors) == 1
    # 剔除而不是写占位符
    assert all(entry.get("AI") for entry in processed)


def test_process_items_raises_when_everything_fails(monkeypatch):
    """全部失败说明是系统性问题（Key/余额/服务），必须显式报错。"""
    monkeypatch.setenv("AI_MAX_RETRIES", "0")
    client = _FakeClient([""])
    items = [{"id": "a", "title": "T", "summary": "S"}, {"id": "b", "title": "T", "summary": "S"}]
    with pytest.raises(RuntimeError) as excinfo:
        enhance.process_items(
            client, _config(), items, max_workers=1, system_text="json {}", user_text="{abstract}"
        )
    assert "全部" in str(excinfo.value)


def test_process_items_parallel_path_uses_threads(monkeypatch):
    monkeypatch.setenv("AI_MAX_RETRIES", "0")
    client = _FakeClient([GOOD_JSON])
    items = [{"id": f"p{i}", "title": "T", "summary": "S"} for i in range(5)]
    processed, errors = enhance.process_items(
        client, _config(), items, max_workers=4, system_text="json {}", user_text="{abstract}"
    )
    assert errors == []
    assert len(processed) == 5
    assert all(set(entry["AI"]) == set(AI_FIELDS) for entry in processed)


def test_mark_metadata_only_does_not_add_ai_content():
    out = enhance.mark_metadata_only([{"id": "a", "title": "T"}])
    assert out[0]["AI"] is None
    assert out[0]["title"] == "T"
