#!/usr/bin/env bash
#
# 心脏电生理每日论文检索 —— 本地一键运行
#
# 流程：采集（daily_ep.collect）→ AI 中文解读 + 摘要翻译（ai/enhance.py）→ 渲染（render.build_html）→ RSS（render.build_rss）
# 完全不依赖 GitHub，产出可直接双击打开的 index.html。
#
# 用法：
#   ./run.sh                    # 抓最近 2 天，带 AI 解读
#   ./run.sh --days 7           # 抓最近 7 天
#   ./run.sh --no-ai            # 跳过 AI（不需要 API Key，也省 token）
#   ./run.sh --date 2026-10-19  # 以 2026-10-19 为基准日采集（补历史数据用），同时决定输出文件名
#   ./run.sh --help
#
# 环境变量（AI 步骤需要）：
#   OPENAI_API_KEY   必填（除非 --no-ai）
#   OPENAI_BASE_URL  默认 https://api.deepseek.com  ← 注意：不带 /v1 后缀
#   MODEL_NAME       默认 deepseek-flash
#   LANGUAGE         默认 Chinese
#   NCBI_API_KEY     可选，把 PubMed 速率从 3/秒 提到 10/秒
#   NCBI_EMAIL       可选，NCBI 建议带上联系方式

set -euo pipefail

# ---------------------------------------------------------------------------
# 路径：始终以脚本所在目录为基准，从任何位置调用都能跑
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

DATA_DIR="${SCRIPT_DIR}/data"
ASSETS_DIR="${SCRIPT_DIR}/assets"

# ---------------------------------------------------------------------------
# 默认参数
# ---------------------------------------------------------------------------
DAYS=2
NO_AI=0
TODAY="$(date -u "+%Y-%m-%d")"   # UTC，与 workflow 的 $(date -u) 保持一致
RUN_DATE=""

PYTHON_BIN="${PYTHON_BIN:-python3}"

usage() {
  # 只打印头部注释块：跳过 shebang，遇到第一条非注释行（set -euo pipefail）就停。
  awk 'NR==1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"
  exit 0
}

# ---------------------------------------------------------------------------
# 解析参数
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --days)
      # 缺值时用 ${2:?} 直接报错退出，避免把空字符串当成天数
      DAYS="${2:?--days 需要一个数字，例如 --days 7}"
      shift 2
      ;;
    --days=*)
      DAYS="${1#*=}"
      shift
      ;;
    --no-ai)
      NO_AI=1
      shift
      ;;
    --date)
      RUN_DATE="${2:?--date 需要 YYYY-MM-DD}"
      shift 2
      ;;
    --date=*)
      RUN_DATE="${1#*=}"
      shift
      ;;
    -h|--help)
      usage
      ;;
    *)
      echo "❌ 未知参数：$1" >&2
      echo "   用 ./run.sh --help 查看用法" >&2
      exit 2
      ;;
  esac
done

# 天数必须是正整数：非数字会让 collect 参数解析失败或产生怪异查询
case "${DAYS}" in
  ''|*[!0-9]*)
    echo "❌ --days 必须是正整数，收到：${DAYS}" >&2
    exit 2
    ;;
esac
if [ "${DAYS}" -lt 1 ]; then
  echo "❌ --days 至少为 1" >&2
  exit 2
fi

if [ -z "${RUN_DATE}" ]; then
  RUN_DATE="${TODAY}"
fi
case "${RUN_DATE}" in
  [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;
  *)
    echo "❌ --date 格式必须是 YYYY-MM-DD，收到：${RUN_DATE}" >&2
    exit 2
    ;;
esac

# ---------------------------------------------------------------------------
# 固定配置（勿改回参考项目旧值）
#   OPENAI_BASE_URL 不带 /v1 —— DeepSeek 官方文档规定
#   MODEL_NAME = deepseek-flash —— deepseek-chat 已不是现行模型名
# ---------------------------------------------------------------------------
export LANGUAGE="${LANGUAGE:-Chinese}"
export MODEL_NAME="${MODEL_NAME:-deepseek-flash}"
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-https://api.deepseek.com}"

RAW_FILE="${DATA_DIR}/${RUN_DATE}.jsonl"
AI_FILE="${DATA_DIR}/${RUN_DATE}_AI_enhanced_${LANGUAGE}.jsonl"
OUT_HTML="${SCRIPT_DIR}/index.html"

echo "=========================================================="
echo " 心脏电生理每日论文检索 —— 本地运行"
echo "=========================================================="
echo " 日期        : ${RUN_DATE}"
echo " 回溯天数    : ${DAYS}"
echo " AI 解读     : $([ "${NO_AI}" -eq 1 ] && echo '关闭 (--no-ai)' || echo '开启')"
echo " 模型        : ${MODEL_NAME}"
echo " BASE_URL    : ${OPENAI_BASE_URL}"
echo " 语言        : ${LANGUAGE}"
echo " 工作目录    : ${SCRIPT_DIR}"
echo "----------------------------------------------------------"

# ---------------------------------------------------------------------------
# 0. 环境自检
# ---------------------------------------------------------------------------
if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "❌ 找不到 ${PYTHON_BIN}，请安装 Python 3.9+ 或设置 PYTHON_BIN 环境变量" >&2
  exit 1
fi
echo "✅ Python: $(${PYTHON_BIN} -V 2>&1)"

# PyYAML 是 daily_ep.config 的硬依赖，缺了会在第一步就崩。
if ! "${PYTHON_BIN}" -c "import yaml" >/dev/null 2>&1; then
  echo "❌ 缺少 PyYAML，请先安装依赖：pip install -e ." >&2
  exit 1
fi

# 需要 AI 但没有 Key：自动降级，而不是报错 —— 与云端行为保持一致。
if [ "${NO_AI}" -eq 0 ] && [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "⚠️  未设置 OPENAI_API_KEY，自动降级为 --no-ai（只输出元数据，不做中文解读）"
  echo "    需要 AI 解读请先：export OPENAI_API_KEY=sk-xxxx"
  NO_AI=1
fi

mkdir -p "${DATA_DIR}" "${ASSETS_DIR}"

# ---------------------------------------------------------------------------
# 1. 采集
# ---------------------------------------------------------------------------
echo ""
echo "▶ [1/4] 采集论文（最近 ${DAYS} 天）"

# 今日/目标文件已存在时先删除再重新生成。
# 直接覆盖会因 collect 的追加语义产生重复记录（同一篇出现两次）。
if [ -f "${RAW_FILE}" ]; then
  echo "🗑️  发现已有文件，删除后重新生成：data/${RUN_DATE}.jsonl"
  rm -f "${RAW_FILE}"
fi
# 残留的临时文件同样清掉，避免半成品被当成有效数据
rm -f "${RAW_FILE}.tmp"

# --date 必须透传给 collect：否则用 --date 指定日期时，文件名是 RUN_DATE
# 但采集窗口仍按"今天"算，文件里装的是另一天的论文（文件名与实际内容不符）。
# 默认情况下 RUN_DATE 就等于今天，显式传与不传结果一致。
"${PYTHON_BIN}" -m daily_ep.collect --days "${DAYS}" --date "${RUN_DATE}" --out "${RAW_FILE}"

if [ ! -f "${RAW_FILE}" ]; then
  echo "❌ 采集失败：未生成 ${RAW_FILE}" >&2
  exit 1
fi

COUNT="$(wc -l < "${RAW_FILE}" | tr -d ' ')"
echo "✅ 采集完成：${COUNT} 篇 → data/${RUN_DATE}.jsonl"

# 0 篇不是失败（周末/节假日/入库延迟都合法），但值得显式提示
if [ "${COUNT}" -eq 0 ]; then
  echo "⚠️  本次采集到 0 篇：可能是周末/节假日，或 PubMed 入库尚未更新"
fi

# ---------------------------------------------------------------------------
# 2. AI 增强
# ---------------------------------------------------------------------------
echo ""
echo "▶ [2/4] AI 中文解读 + 摘要翻译"

# 上一次运行留下的 AI 产物先删掉：不删的话 enhance 可能复用过期的解读，
# 与刚重新采集的元数据对不上。
if [ -f "${AI_FILE}" ]; then
  echo "🗑️  删除旧的 AI 产物：data/${RUN_DATE}_AI_enhanced_${LANGUAGE}.jsonl"
  rm -f "${AI_FILE}"
fi

if [ "${NO_AI}" -eq 1 ]; then
  "${PYTHON_BIN}" ai/enhance.py --data "${RAW_FILE}" --no-ai
else
  # AI 失败不让整条流程挂掉：元数据仍然有价值，降级后继续渲染。
  if ! "${PYTHON_BIN}" ai/enhance.py --data "${RAW_FILE}"; then
    echo "⚠️  AI 增强失败，降级为 --no-ai 继续（页面将只显示摘要）"
    "${PYTHON_BIN}" ai/enhance.py --data "${RAW_FILE}" --no-ai
  fi
fi

# 兜底：enhance.py 在降级路径下若未产出 AI 文件，用原始文件顶上，
# 保证渲染步骤总有输入。
if [ ! -f "${AI_FILE}" ]; then
  echo "ℹ️  未生成 ${AI_FILE}，使用原始文件继续渲染"
  cp "${RAW_FILE}" "${AI_FILE}"
fi
echo "✅ AI 步骤完成：data/${RUN_DATE}_AI_enhanced_${LANGUAGE}.jsonl"

# ---------------------------------------------------------------------------
# 3. 渲染
# ---------------------------------------------------------------------------
echo ""
echo "▶ [3/4] 渲染 HTML"
# 单页架构：扫描 data/ 下所有日期生成站点壳，前端按需 fetch 各日 JSONL
"${PYTHON_BIN}" -m render.build_html --data-dir "${DATA_DIR}" --out "${OUT_HTML}"

if [ ! -f "${OUT_HTML}" ]; then
  echo "❌ 渲染失败：未生成 ${OUT_HTML}" >&2
  exit 1
fi
echo "✅ 渲染完成：index.html"

# ---------------------------------------------------------------------------
# 4. RSS 订阅源
# ---------------------------------------------------------------------------
echo ""
echo "▶ [4/4] 生成 RSS 订阅源"

# SITE_URL 决定 feed 里的链接。未显式给出时从 git remote 推导 Pages 地址
# （https://github.com/user/repo.git → https://user.github.io/repo）；
# 推导不出来就用相对路径生成，本地预览可用、阅读器里打不开。
SITE_URL="${SITE_URL:-}"
if [ -z "${SITE_URL}" ] && command -v git >/dev/null 2>&1; then
  ORIGIN_URL="$(git remote get-url origin 2>/dev/null || true)"
  case "${ORIGIN_URL}" in
    *github.com[:/]*)
      REPO_PATH="${ORIGIN_URL#*github.com[:/]}"
      REPO_PATH="${REPO_PATH%.git}"
      SITE_URL="https://${REPO_PATH%%/*}.github.io/${REPO_PATH#*/}"
      ;;
  esac
fi
"${PYTHON_BIN}" -m render.build_rss --data-dir "${DATA_DIR}" --out "${SCRIPT_DIR}/feed.xml" --base-url "${SITE_URL}"
echo "✅ RSS 完成：feed.xml（base_url=${SITE_URL:-相对路径}）"

# 重建数据清单，和 workflow 里做的是同一件事（本地方便查阅）
ls "${DATA_DIR}"/*.jsonl 2>/dev/null | sed 's|.*/data/||' | sort > "${ASSETS_DIR}/file-list.txt" || true

echo ""
echo "=========================================================="
echo " ✅ 全部完成"
echo "    论文数   : ${COUNT}"
echo "    页面     : ${OUT_HTML}"
echo "    RSS      : ${SCRIPT_DIR}/feed.xml"
echo "    原始数据 : ${RAW_FILE}"
echo "    打开方式 : 双击 index.html，或 open \"${OUT_HTML}\""
echo "=========================================================="
