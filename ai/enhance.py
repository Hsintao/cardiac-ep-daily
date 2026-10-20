"""AI 增强主逻辑 + CLI。

读入采集层产出的 ``data/YYYY-MM-DD.jsonl``，为每篇论文生成中文五段式解读，
写出 ``data/YYYY-MM-DD_AI_enhanced_Chinese.jsonl``（与参考项目 daily-arXiv
的产物命名一致，渲染层可以直接吃）。

三层保障（对应任务里最容易出错的地方）：

  1. **配置层**：base_url 固定 ``https://api.deepseek.com``（不带 /v1）、
     模型固定 ``deepseek-flash``；没有 API Key 时**自动降级为 --no-ai**，
     而不是崩掉。
  2. **解析层**：JSON 模式的响应可能带散文、LaTeX 非法转义、尾随逗号，
     或被 max_tokens 截断 —— 全部交给 ``ai.json_repair`` 的纯函数恢复。
  3. **降级层**：空 content / 解析失败会重试；重试用尽后该篇**整个 AI 字段
     不写**（并默认把该篇从输出中剔除），绝不写
     "Summary generation failed" 这类占位符伪装成真摘要。

CLI::

    python3 -m ai.enhance --data data/2026-10-19.jsonl
    python3 -m ai.enhance --data data/2026-10-19.jsonl --no-ai
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 允许 `python3 ai/enhance.py` 直接运行（此时 __package__ 为空）
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import json_repair, runtime  # noqa: E402
from ai.structure import (  # noqa: E402
    AI_FIELDS,
    Structure,
    find_truncated_fields,
    is_meaningful,
    normalized_ai_dict,
)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
DEFAULT_LANGUAGE = "Chinese"
OUTPUT_SUFFIX = f"_AI_enhanced_{DEFAULT_LANGUAGE}.jsonl"


# --------------------------------------------------------------------------
# 提示词
# --------------------------------------------------------------------------


def load_prompt(name: str) -> str:
    """读取提示词文件（system.txt / user.txt）。"""
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def ensure_json_mode_contract(system_text: str, user_text: str) -> None:
    """校验 DeepSeek JSON 模式的硬性要求。

    官方文档明确：使用 ``response_format={'type':'json_object'}`` 时，
    prompt 里**必须含 "json" 字样，并且必须给出输出格式样例**，
    否则服务端可能返回错误或空内容。这里做的是启动时自检，
    改坏提示词会在跑之前就报错，而不是跑完一天才发现全是空响应。
    """
    combined = f"{system_text}\n{user_text}".lower()
    if "json" not in combined:
        raise ValueError(
            "JSON 模式要求 prompt 中含 'json' 字样（DeepSeek 官方文档规定），"
            f"请检查 {PROMPTS_DIR}"
        )
    if not has_json_format_sample(combined):
        raise ValueError(
            "JSON 模式要求 prompt 中给出**输出格式样例**（DeepSeek 官方文档规定）："
            "需要一段形如 {\"字段\": \"值\"} 的示例，"
            f"注意 {{abstract}} 这类模板占位符不算样例。请检查 {PROMPTS_DIR}"
        )


def has_json_format_sample(text: str) -> bool:
    """判断 prompt 里是否真的给了输出格式样例。

    仅仅检查「有没有花括号」是不够的：本项目的 user.txt 里本来就有
    ``{abstract}`` / ``{title}`` 这类模板占位符，任何包含它们的 prompt
    都会「通过」格式样例检查 —— 这种校验等于没校验，而 DeepSeek 在
    缺少格式样例时会返回错误或空 content，属于必须提前拦住的失败。

    所以要求是：存在一个花括号片段，里面含有**带引号的键**
    （``"tldr":`` 这种），即一段真正的 JSON 示例对象。
    """
    if not isinstance(text, str) or not text:
        return False
    lowered = text.lower()
    # 引号键名 + 冒号，例如 "tldr":  或 'tldr':
    return re.search(r'["\'][a-z_][a-z0-9_]*["\']\s*:', lowered) is not None


def build_messages(
    item: Dict,
    system_text: Optional[str] = None,
    user_text: Optional[str] = None,
) -> List[Dict[str, str]]:
    """按论文条目构造 messages。"""
    system_text = system_text if system_text is not None else load_prompt("system.txt")
    user_text = user_text if user_text is not None else load_prompt("user.txt")

    # 摘要字段名在不同来源里不一致：采集层写 summary，部分数据源写 abstract
    abstract = (item.get("summary") or item.get("abstract") or "").strip()
    rendered_user = (
        user_text.replace("{journal}", str(item.get("journal") or "未提供"))
        .replace("{topic}", str(item.get("topic") or item.get("category") or "未标注"))
        .replace("{title}", str(item.get("title") or ""))
        .replace("{abstract}", abstract)
    )
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": rendered_user},
    ]


# --------------------------------------------------------------------------
# 单篇处理
# --------------------------------------------------------------------------


def extract_content(response) -> str:
    """从 openai SDK 的响应里取文本内容，缺失时返回空串。

    空 content 是 DeepSeek JSON 模式的已知行为（文档明确警告），
    所以这里不做任何「补默认值」，一律返回空串，由上层触发重试。
    """
    try:
        choices = getattr(response, "choices", None)
        if not choices:
            return ""
        message = getattr(choices[0], "message", None)
        if message is None:
            return ""
        content = getattr(message, "content", None)
        return content if isinstance(content, str) else ""
    except Exception:
        return ""


def parse_response_text(text: str) -> Optional[Dict]:
    """把模型响应文本解析成 dict；解析不出返回 ``None``。

    ``None`` 的语义是「需要重试」：既覆盖空 content，也覆盖截断到无法修复
    的响应。纯逻辑，可单测。
    """
    if json_repair.looks_like_empty_content(text):
        return None
    return json_repair.parse_json_object(text)


def coerce_ai_fields(parsed: Dict) -> Optional[Dict[str, str]]:
    """把解析出的 dict 收敛成五段式；无实质内容时返回 ``None``。

    ``None`` 表示「这篇拿不到真摘要」，调用方据此丢弃该篇而不是写占位符。
    """
    if not isinstance(parsed, dict) or not parsed:
        return None
    try:
        model = Structure(**{k: v for k, v in parsed.items() if k in AI_FIELDS})
    except Exception:
        # 严格校验失败（例如字段类型完全无法转文本）时退一步：
        # 直接从 dict 里挑出可用字段，仍然拒绝占位符。
        fallback = {
            name: parsed.get(name)
            for name in AI_FIELDS
            if isinstance(parsed.get(name), str)
        }
        if not is_meaningful(fallback):
            return None
        return normalized_ai_dict(fallback)

    values = model.to_ai_dict()
    if not is_meaningful(values):
        return None
    return normalized_ai_dict(values)


def call_model(client, config: Dict, messages: List[Dict[str, str]]) -> Dict[str, str]:
    """请求一次模型并解析；不可用时抛 :class:`runtime.EmptyResponseError`。

    这里刻意不吞异常：重试与降级都在 :func:`enhance_item` 里统一决策。
    """

    def _once():
        response = client.chat.completions.create(
            **runtime.build_request_kwargs(config["model_name"], messages)
        )
        content = extract_content(response)
        parsed = parse_response_text(content)
        if parsed is None:
            # 区分「空 content」与「有内容但解析不出」，只为日志更准
            if json_repair.looks_like_empty_content(content):
                raise runtime.EmptyResponseError(
                    "模型返回空 content（DeepSeek JSON 模式已知行为，将重试）"
                )
            raise runtime.UnparseableResponseError(
                f"响应无法解析为 JSON（前 200 字符：{content[:200]!r}）"
            )
        return content, parsed

    def _attempt():
        content, parsed = _once()
        values = coerce_ai_fields(parsed)
        if values is None:
            # 解析成功但五段式全空 —— 与空 content 同样处理：重试
            raise runtime.EmptyResponseError(
                f"响应解析成功但无有效内容（前 200 字符：{content[:200]!r}）"
            )
        # 单字段被 max_tokens 截断时（如 motivation 只剩 "HFrEF……"），
        # is_meaningful 会因其它字段完整而放行，汇总仍显示「失败 0 篇」，
        # 但残缺字段比缺失更误导读者。
        #
        # 处理方式与「部分响应保留可用字段」的既有契约保持一致：
        # **剔除**被截断的字段（置空串），保留完整字段，而不是整条重试/丢弃。
        # 渲染层对空串字段不渲染，读者看到的只是少一段，不会看到半句话。
        truncated = find_truncated_fields(values)
        if truncated:
            values = dict(values)
            for name in truncated:
                values[name] = ""
            print(
                f"  剔除疑似截断字段：{', '.join(truncated)}",
                file=sys.stderr,
            )
            if not is_meaningful(values):
                raise runtime.EmptyResponseError("剔除截断字段后已无有效内容，重试")
        return values

    return runtime.invoke_with_retry(
        _attempt,
        on_retry=lambda attempt, delay, error: print(
            f"  重试第 {attempt} 次，{delay:.1f}s 后（{type(error).__name__}: {error}）",
            file=sys.stderr,
        ),
    )


def enhance_item(
    client,
    config: Dict,
    item: Dict,
    system_text: Optional[str] = None,
    user_text: Optional[str] = None,
) -> Tuple[Optional[Dict], Optional[str]]:
    """处理一篇论文。

    返回 ``(item, error)``：
      - 成功：``(带 AI 字段的 item, None)``
      - 该跳过（如无摘要）：``(item, None)`` 且 item 上带 ``ai_skipped`` 标记
      - 失败：``(None, 失败原因)`` —— 调用方**不要**把占位符写进去

    纯逻辑（除了 ``client`` 的调用），不读文件系统，便于单测。
    """
    # 无摘要论文（社论/评论/勘误）跳过 AI，避免污染页面
    if item.get("no_abstract"):
        return item, None
    abstract = (item.get("summary") or item.get("abstract") or "").strip()
    if not abstract:
        item = dict(item)
        item.setdefault("no_abstract", True)
        return item, None

    if client is None:
        return None, "未配置 API Key，且未启用 --no-ai"

    messages = build_messages(item, system_text, user_text)
    try:
        values = call_model(client, config, messages)
    except Exception as error:  # noqa: BLE001 - 这里就是降级决策点
        return None, runtime.describe_failure(str(item.get("id", "unknown")), error)

    result = dict(item)
    result["AI"] = values
    return result, None


# --------------------------------------------------------------------------
# 批量处理
# --------------------------------------------------------------------------


def load_jsonl(path: Path) -> List[Dict]:
    """读 JSONL，跳过坏行（一条坏行不该让整天的数据作废）。"""
    items: List[Dict] = []
    if not path.exists():
        return items
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError as error:
                print(f"跳过无法解析的第 {line_no} 行：{error}", file=sys.stderr)
    return items


def dedupe_by_id(items: List[Dict]) -> List[Dict]:
    """按 id 去重（保留首次出现）。"""
    seen = set()
    unique: List[Dict] = []
    for item in items:
        key = item.get("id")
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def write_jsonl(path: Path, items: List[Dict]) -> None:
    """写 JSONL（``ensure_ascii=False``：中文原样输出，便于人工核对）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")


def process_items(
    client,
    config: Dict,
    items: List[Dict],
    max_workers: int = runtime.DEFAULT_MAX_WORKERS,
    system_text: Optional[str] = None,
    user_text: Optional[str] = None,
) -> Tuple[List[Dict], List[str]]:
    """并行处理所有条目，返回 ``(成功列表, 失败原因列表)``。

    失败条目**从输出里剔除**（而不是写占位符）：渲染层只检查键是否存在，
    写占位符等于给读者一条假摘要。

    但「全部失败」是系统性问题（Key 无效 / 余额不足 / 服务中断），
    必须显式报错，不能静默产出一个空日报。
    """
    results: List[Optional[Dict]] = [None] * len(items)
    errors: List[str] = []

    workers = max(1, int(max_workers))
    if workers == 1:
        for index, item in enumerate(items):
            processed, error = enhance_item(client, config, item, system_text, user_text)
            results[index] = processed
            if error:
                errors.append(error)
                print(f"  ✗ {error}", file=sys.stderr)
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    enhance_item, client, config, item, system_text, user_text
                ): index
                for index, item in enumerate(items)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    processed, error = future.result()
                except Exception as failure:  # noqa: BLE001 - 线程内异常兜底
                    processed, error = None, f"处理第 {index} 条时线程内异常：{failure}"
                results[index] = processed
                if error:
                    errors.append(error)
                    print(f"  ✗ {error}", file=sys.stderr)

    if errors and not any(result is not None for result in results):
        raise RuntimeError(
            f"{len(errors)} 篇论文全部 AI 增强失败，疑似系统性问题"
            f"（Key/余额/服务中断）；首条原因：{errors[0]}"
        )

    return [result for result in results if result is not None], errors


def mark_metadata_only(items: List[Dict]) -> List[Dict]:
    """``--no-ai`` 路径：只输出元数据，**不写任何 AI 字段**。

    注意这里不写 ``"AI": null`` 之外的任何东西：渲染层以「键是否存在」
    判断有没有 AI 内容，写空字典会让它渲染出一片空白而看不出原因。
    """
    output: List[Dict] = []
    for item in items:
        entry = dict(item)
        entry["AI"] = None
        output.append(entry)
    return output


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def output_path_for(data_path: Path, language: str = DEFAULT_LANGUAGE) -> Path:
    """``data/2026-10-19.jsonl`` → ``data/2026-10-19_AI_enhanced_Chinese.jsonl``。"""
    suffix = f"_AI_enhanced_{language}.jsonl"
    name = data_path.name
    if name.endswith(".jsonl"):
        return data_path.with_name(name[: -len(".jsonl")] + suffix)
    return data_path.with_name(name + suffix)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="为心脏电生理论文生成中文五段式 AI 解读"
    )
    parser.add_argument("--data", type=str, required=True, help="输入的 jsonl 文件")
    parser.add_argument(
        "--max_workers",
        type=int,
        default=None,
        help=f"并发线程数（默认 {runtime.DEFAULT_MAX_WORKERS}，上限 {runtime.MAX_MAX_WORKERS}）",
    )
    parser.add_argument(
        "--no-ai",
        dest="no_ai",
        action="store_true",
        help="跳过 AI，只输出元数据（无 API Key 时自动启用）",
    )
    parser.add_argument(
        "--language",
        type=str,
        default=DEFAULT_LANGUAGE,
        help=f"输出文件的语言后缀（默认 {DEFAULT_LANGUAGE}）",
    )
    return parser.parse_args(argv)


def build_config(env: Optional[dict] = None) -> Dict:
    """读取 DeepSeek 配置（缺 Key 不抛异常，只标记 is_ready=False）。"""
    return runtime.resolve_config(env)


def _load_dotenv_if_present() -> None:
    """加载项目根的 ``.env``（若存在）。

    本地开发时把 Key 放 .env 就不用每次 export；该文件已被 .gitignore 忽略，
    不会进仓库。云端用 GitHub Secrets，环境变量已就绪，这里读不到文件也无害。
    ``override=False``：环境变量优先于 .env，避免覆盖 CI 注入的凭据。
    """
    # EP_ENHANCE_NO_DOTENV=1 可显式关闭加载。测试里必须设置它 —— 否则
    # 「删除 OPENAI_API_KEY 模拟无 Key」的用例会被 .env 里的真实 Key 复活，
    # 既破坏测试隔离，又真的会调用线上 API（消耗额度）。
    if os.environ.get("EP_ENHANCE_NO_DOTENV", "").strip() in ("1", "true", "yes"):
        return
    try:
        from dotenv import load_dotenv
    except ImportError:  # python-dotenv 是可选依赖
        return
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
        if candidate.exists():
            load_dotenv(candidate, override=False)
            return


def main(argv: Optional[List[str]] = None) -> int:
    _load_dotenv_if_present()
    args = parse_args(argv)
    data_path = Path(args.data)

    if not data_path.exists():
        print(f"输入文件不存在：{data_path}", file=sys.stderr)
        return 2

    config = build_config()
    print(
        f"DeepSeek 配置：base_url={config['base_url']} "
        f"model={config['model_name']} key={'已设置' if config['is_ready'] else '未设置'}",
        file=sys.stderr,
    )

    # 无 Key 自动降级：这是「无 API Key 也能跑通」契约的落点
    no_ai = args.no_ai
    if not config["is_ready"] and not no_ai:
        print(
            f"未检测到 {runtime.API_KEY_ENV}，自动降级为 --no-ai（仅输出元数据）",
            file=sys.stderr,
        )
        no_ai = True

    items = dedupe_by_id(load_jsonl(data_path))
    if not items:
        print(f"输入文件没有可用数据：{data_path}", file=sys.stderr)
        return 1
    print(f"读入 {len(items)} 篇（已按 id 去重）", file=sys.stderr)

    target = output_path_for(data_path, args.language)

    if no_ai:
        output = mark_metadata_only(items)
        write_jsonl(target, output)
        print(f"已写出 {len(output)} 条元数据（未调用 AI）：{target}", file=sys.stderr)
        return 0

    workers = args.max_workers if args.max_workers is not None else runtime.max_workers()
    workers = max(runtime.MIN_MAX_WORKERS, min(workers, runtime.MAX_MAX_WORKERS))

    system_text = load_prompt("system.txt")
    user_text = load_prompt("user.txt")
    # 启动自检：JSON 模式要求 prompt 含 "json" 且有格式样例
    ensure_json_mode_contract(system_text, user_text)

    client = runtime.create_client(config)
    if client is None:
        # 理论上到不了这里（前面已降级），留作最后一道防线
        print("无法创建客户端，降级为 --no-ai", file=sys.stderr)
        write_jsonl(target, mark_metadata_only(items))
        return 0

    processed, errors = process_items(
        client, config, items, max_workers=workers,
        system_text=system_text, user_text=user_text,
    )
    write_jsonl(target, processed)

    skipped = sum(1 for item in items if item.get("no_abstract"))
    print(
        f"完成：{len(processed)}/{len(items)} 篇写出到 {target}"
        f"（跳过无摘要 {skipped} 篇，失败 {len(errors)} 篇）",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
