#!/bin/bash
#
# WeChat 群聊每日摘要脚本
# 直接从微信本地数据库提取聊天记录，调用 LLM 做结构化总结，保存为 Markdown + PDF
#
# 产出文件（以 2026-04-09 为例）：
#   2026-04-09-我的群-聊天记录.md  — 原始聊天记录
#   2026-04-09-我的群-摘要.md      — 结构化摘要（需配置 LLM_CMD）
#   2026-04-09-我的群-摘要.pdf     — PDF 版摘要（需配置 LLM_CMD + pandoc + Chrome）
#
# 用法：
#   ./wechat-digest.sh                    # 总结昨天的记录
#   ./wechat-digest.sh 2026-04-01         # 总结指定日期的记录
#   GROUP_NAME="其他群" ./wechat-digest.sh # 总结其他群
#
# LLM 配置（通过环境变量 LLM_CMD）：
#   LLM_CMD 应为一个命令，从 stdin 读取 prompt+聊天记录，输出摘要到 stdout。
#   示例：
#     export LLM_CMD="claude -p"                              # Claude Code CLI
#     export LLM_CMD="openai chat -m gpt-4o"                  # OpenAI CLI (github.com/openai/openai-python)
#     export LLM_CMD="llm -m gpt-4o"                          # Simon Willison's llm CLI (github.com/simonw/llm)
#     export LLM_CMD="ollama run qwen2.5"                     # 本地模型 via Ollama
#   如果不设置 LLM_CMD，脚本只会提取聊天记录，不做总结和 PDF。
#
# 前置条件：
#   1. 已运行 sudo python3 init-keys.py（提取微信数据库密钥）
#   2. pip3 install -r requirements.txt
#   3. （可选）配置 LLM_CMD 环境变量（用于 AI 总结）
#   4. （可选）pandoc + Google Chrome（用于生成 PDF）
#
# ============ 踩坑记录 ============
#
# 1. wechat-cli history 命令不稳定
#    wechat-cli history 有时返回 0 条消息（原因不明，可能跟微信重启有关）。
#    所以本脚本不依赖 wechat-cli history，而是用 extract-messages.py 直接
#    解密读取微信的 SQLite 数据库。
#
# 2. 微信消息类型是复合值
#    微信数据库的 local_type 字段是复合类型，低 32 位才是真实消息类型。
#    比如链接消息 type=49，但数据库里存的可能是 244813135921。
#    查询时必须用 (local_type & 0xFFFFFFFF) = 49，不能直接 local_type = 49。
#
# 3. 消息内容可能是 zstd 压缩的
#    当 WCDB_CT_message_content = 4 时，message_content 是 zstd 压缩的，
#    需要先解压才能读取。这是 WCDB 的特性。
#
# 4. launchd/cron 环境的 PATH 问题
#    macOS launchd 启动的任务环境变量很少，PATH 里没有 homebrew 和 anaconda。
#    如果 python3 和依赖库装在 anaconda/homebrew 里，需要在 launchd plist 的
#    EnvironmentVariables 中显式设置 PATH，包含这些路径。
#    示例：/Users/你的用户名/opt/anaconda3/bin:/opt/homebrew/bin:/usr/local/bin:...
#
# 5. LLM 可能篡改消息数量
#    即使在 prompt 中明确写了"消息总数：510 条"，LLM 有时会自作主张改成
#    "约 280 条"之类的估算值。解决方法：在 prompt 中加注释强调"这个数字是
#    精确统计，请原样使用，不要修改"。
#
# 6. 微信必须先启动同步
#    脚本读的是微信本地数据库文件。如果昨晚关了微信，凌晨 0-2 点的消息不在
#    本地数据库里。必须等微信启动并同步完消息后再跑脚本。
#    本脚本用 pgrep 检测微信进程，检测到后再等 2 分钟让消息同步。
#
# 7. pandoc 生成的 HTML 有多余标题
#    pandoc 会自动生成 <title> 和 <header id="title-block-header"> 块，
#    导致 PDF 出现重复标题。解决方法：用 regex 删掉这两个元素。
#
# 8. Chrome headless PDF 的页眉页脚
#    Chrome 旧版 --print-to-pdf-no-header 不一定生效。
#    用 --headless=new 搭配 --no-pdf-header-footer 才能彻底去掉。
#
# ====================================

set -euo pipefail

# ============ 等待微信启动 ============
# 微信需要启动并同步消息后数据库才有最新数据
# 手动运行时可 Ctrl+C 跳过等待
MAX_WAIT=1800  # 最多等 30 分钟
WAIT_INTERVAL=30
waited=0
echo "$(date '+%Y-%m-%d %H:%M:%S') 等待微信启动..."
while ! pgrep -x "WeChat" > /dev/null 2>&1; do
    if [[ $waited -ge $MAX_WAIT ]]; then
        echo "等待超时（${MAX_WAIT}秒），微信未启动，跳过本次摘要"
        exit 0
    fi
    sleep $WAIT_INTERVAL
    waited=$((waited + WAIT_INTERVAL))
    echo "  已等待 ${waited}s..."
done
# 微信已启动，再等 2 分钟让消息同步完成
echo "微信已启动，等待 2 分钟同步消息..."
sleep 120

# ============ 配置 ============
GROUP_NAME="${GROUP_NAME:-你的群名}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="${OUTPUT_DIR:-$SCRIPT_DIR/output}"
HOUR_OFFSET=2  # 时间窗口：当天 02:00 ~ 次日 02:00（适合夜猫子群）
VOICE_ENGINE="${VOICE_ENGINE:-auto}"  # 语音转写引擎：auto/xfyun/whisper/none
# LLM_CMD: 通过环境变量配置，不设置则跳过总结（见文件头部说明）
# ==============================

# 日期：参数传入 或 默认昨天
if [[ $# -ge 1 ]]; then
    TARGET_DATE="$1"
else
    # macOS 用 -v-1d，Linux 用 -d "yesterday"
    if [[ "$(uname)" == "Darwin" ]]; then
        TARGET_DATE=$(date -v-1d +%Y-%m-%d)
    else
        TARGET_DATE=$(date -d "yesterday" +%Y-%m-%d)
    fi
fi

echo "正在获取「${GROUP_NAME}」${TARGET_DATE} 的聊天记录（${HOUR_OFFSET}:00 ~ 次日 ${HOUR_OFFSET}:00）..."

# 1. 从数据库直接提取消息（已包含 URL 提取）
#    不使用 wechat-cli history，因为该命令不稳定（见踩坑记录 #1）
ENRICHED=$(mktemp /tmp/wechat-enriched-XXXXXX.txt)
STDERR_TMP=$(mktemp /tmp/wechat-stderr-XXXXXX.txt)
trap "rm -f $ENRICHED $STDERR_TMP" EXIT

python3 "${SCRIPT_DIR}/extract-messages.py" "$GROUP_NAME" "$TARGET_DATE" \
    --hour-offset "$HOUR_OFFSET" --voice-engine "$VOICE_ENGINE" > "$ENRICHED" 2>"$STDERR_TMP"

total=$(grep -c "^\[" "$ENRICHED" || true)

if [[ $total -eq 0 ]]; then
    echo "${TARGET_DATE} 没有找到「${GROUP_NAME}」的聊天记录"
    cat "$STDERR_TMP"
    exit 0
fi

# 2. 保存聊天记录原文
mkdir -p "$OUTPUT_DIR"
CHAT_LOG="${OUTPUT_DIR}/${TARGET_DATE}-${GROUP_NAME}-聊天记录.md"
cp "$ENRICHED" "$CHAT_LOG"
echo "$(date '+%H:%M:%S') 共 ${total} 条消息，聊天记录已保存到：${CHAT_LOG}"

# 3. 调用 LLM 做总结
if [[ -z "${LLM_CMD:-}" ]]; then
    echo ""
    echo "未配置 LLM_CMD 环境变量，跳过 AI 总结和 PDF 生成。"
    echo "如需自动总结，请设置 LLM_CMD，例如："
    echo "  export LLM_CMD=\"claude -p\"        # Claude Code"
    echo "  export LLM_CMD=\"llm -m gpt-4o\"    # llm CLI"
    echo "  export LLM_CMD=\"ollama run qwen2.5\" # Ollama 本地模型"
    echo ""
    echo "你也可以手动总结："
    echo "  cat \"${CHAT_LOG}\" | your-llm-command < prompt-template.txt"
    exit 0
fi

echo "$(date '+%H:%M:%S') 正在用 LLM 生成摘要..."

OUTPUT_FILE="${OUTPUT_DIR}/${TARGET_DATE}-${GROUP_NAME}-摘要.md"

# 读取 prompt 模板并替换变量
PROMPT_TEMPLATE="${SCRIPT_DIR}/prompt-template.txt"
if [[ ! -f "$PROMPT_TEMPLATE" ]]; then
    echo "错误：找不到 prompt 模板文件 ${PROMPT_TEMPLATE}"
    exit 1
fi

PROMPT=$(sed \
    -e "s/{{GROUP_NAME}}/${GROUP_NAME}/g" \
    -e "s/{{TARGET_DATE}}/${TARGET_DATE}/g" \
    -e "s/{{TOTAL}}/${total}/g" \
    "$PROMPT_TEMPLATE")

# 将 prompt + 聊天记录一起发给 LLM
{
    echo "$PROMPT"
    echo ""
    echo "--- 以下是聊天记录 ---"
    echo ""
    cat "$ENRICHED"
} | $LLM_CMD > "$OUTPUT_FILE"

echo "$(date '+%H:%M:%S') Markdown 生成完毕"

# 4. 生成 PDF（pandoc 转 HTML -> 注入 CSS -> Chrome headless 打印）
#    如果 pandoc 或 Chrome 不可用，跳过 PDF 生成
if ! command -v pandoc &> /dev/null; then
    echo "未安装 pandoc，跳过 PDF 生成（brew install pandoc）"
    echo ""
    echo "全部完成"
    echo "摘要：${OUTPUT_FILE}"
    echo "聊天记录：${CHAT_LOG}"
    exit 0
fi

CHROME_PATH="${CHROME_PATH:-}"
if [[ -z "$CHROME_PATH" ]]; then
    # 自动检测 Chrome 路径
    if [[ "$(uname)" == "Darwin" ]]; then
        CHROME_PATH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    elif command -v google-chrome &> /dev/null; then
        CHROME_PATH="google-chrome"
    elif command -v chromium &> /dev/null; then
        CHROME_PATH="chromium"
    fi
fi

if [[ -z "$CHROME_PATH" ]] || ! [[ -x "$CHROME_PATH" || $(command -v "$CHROME_PATH" 2>/dev/null) ]]; then
    echo "未找到 Chrome，跳过 PDF 生成"
    echo ""
    echo "全部完成"
    echo "摘要：${OUTPUT_FILE}"
    echo "聊天记录：${CHAT_LOG}"
    exit 0
fi

PDF_FILE="${OUTPUT_DIR}/${TARGET_DATE}-${GROUP_NAME}-摘要.pdf"
echo "正在生成 PDF..."

HTML_TMP=$(mktemp /tmp/wechat-digest-XXXXXX.html)
pandoc "$OUTPUT_FILE" -f markdown -t html5 --standalone -o "$HTML_TMP" 2>/dev/null

# 注入中文友好 CSS，并删掉 pandoc 自动生成的重复标题（见踩坑记录 #7）
python3 - "$HTML_TMP" << 'PYCSS'
import sys, re
html_path = sys.argv[1]
html = open(html_path).read()

html = re.sub(r'<title>.*?</title>', '<title></title>', html)
html = re.sub(r'<header id="title-block-header">.*?</header>', '', html, flags=re.DOTALL)

css = """
<style>
  @page { size: A4; margin: 1.5cm 1.8cm; }
  @media print { @page { margin: 1.5cm 1.8cm; } }
  body {
    font-family: -apple-system, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    font-size: 14px; line-height: 1.9; color: #1a1a1a;
    max-width: 780px; margin: 0 auto; padding: 0 20px;
  }
  h1 {
    font-size: 22px; text-align: center; font-weight: 700;
    border-bottom: 2px solid #2563eb; padding-bottom: 12px; margin-top: 10px; margin-bottom: 28px; color: #111;
  }
  h2 {
    font-size: 17px; color: #1e40af; font-weight: 600;
    border-left: 4px solid #2563eb; padding-left: 12px; margin-top: 28px; margin-bottom: 12px;
  }
  h3 { font-size: 15px; color: #374151; margin-top: 18px; margin-bottom: 6px; }
  p { margin: 6px 0; }
  ul, ol { margin: 6px 0; padding-left: 24px; }
  li { margin: 4px 0; }
  strong { color: #111; }
  table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 13px; }
  th { background: #eff6ff; font-weight: 600; color: #1e40af; }
  th, td { border: 1px solid #d1d5db; padding: 8px 12px; text-align: left; vertical-align: top; }
  tr:nth-child(even) { background: #f9fafb; }
  a { color: #2563eb; text-decoration: none; }
  blockquote { background: #f8f9fa; border-left: 4px solid #9ca3af; padding: 10px 16px; margin: 12px 0; color: #4b5563; }
  hr { border: none; border-top: 1px solid #e5e7eb; margin: 20px 0; }
  code { background: #f3f4f6; padding: 2px 5px; border-radius: 3px; font-size: 13px; }
</style>
"""
html = html.replace('</head>', css + '</head>')
open(html_path, 'w').write(html)
PYCSS

# Chrome headless 生成 PDF（见踩坑记录 #8：必须用 --headless=new + --no-pdf-header-footer）
"$CHROME_PATH" \
  --headless=new --disable-gpu --no-sandbox \
  --print-to-pdf="$PDF_FILE" \
  --no-pdf-header-footer \
  "$HTML_TMP" 2>/dev/null

rm -f "$HTML_TMP"

echo ""
echo "$(date '+%H:%M:%S') 全部完成"
echo "摘要：${OUTPUT_FILE}"
echo "PDF：${PDF_FILE}"
echo "聊天记录：${CHAT_LOG}"
