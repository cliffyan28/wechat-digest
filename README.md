# WeChat Group Digest

微信群聊每日摘要自动化工具。从微信本地加密数据库提取聊天记录，用 LLM 生成结构化摘要，输出 Markdown + PDF。支持任意 LLM（Claude、GPT、Gemini、本地模型等）。

## 效果

每天自动生成三个文件：
- `2026-04-09-群名-聊天记录.md` — 原始聊天记录（含链接 URL）
- `2026-04-09-群名-摘要.md` — 结构化摘要（适合AI调用）
- `2026-04-09-群名-摘要.pdf` — PDF 版摘要（适合跟别人分享）

## 原理

```
微信本地加密数据库 (SQLCipher 4)
        ↓ init-keys.py（从微信进程内存提取密钥）
解密密钥 (~/.wechat-digest/all_keys.json)
        ↓ extract-messages.py（解密 + 提取文本/链接消息）
格式化聊天记录（含 URL）
        ↓ LLM_CMD（任意 LLM 结构化总结）
Markdown 摘要
        ↓ pandoc + Chrome headless
PDF
```

## 快速开始

### 1. 安装依赖

```bash
# Python 依赖（必需）
pip3 install pycryptodome zstandard

# pandoc（可选，生成 PDF 用）
brew install pandoc  # macOS
# apt install pandoc  # Linux

# Google Chrome（可选，生成 PDF 用，通常已有）
```

### 2. 提取密钥

**确保微信已启动并登录**，然后运行：

```bash
sudo python3 init-keys.py
```

这会扫描微信进程内存，提取数据库加密密钥，保存到 `~/.wechat-digest/`。

> 如果你已经用 `wechat-cli init` 生成过密钥（在 `~/.wechat-cli/`），本项目会自动读取，无需重复操作。

### 3. 配置群名

编辑 `extract-messages.py`，在 `known` 字典中添加你的群：

```python
known = {
    '你的群名': '12345678901@chatroom',
}
```

群的 username（chatroom ID）可以通过以下方式获取：
- 如果安装了 wechat-cli：`wechat-cli sessions --limit 1000 | grep "群名"`
- 或者查看微信数据库目录下的文件名规律

### 4. 配置 LLM（可选）

通过环境变量 `LLM_CMD` 配置你使用的 LLM。`LLM_CMD` 应为一个命令，从 stdin 读入 prompt + 聊天记录，输出摘要到 stdout。

```bash
# 方式一：Claude Code CLI（推荐，Anthropic 官方）
# 安装：npm install -g @anthropic-ai/claude-code
export LLM_CMD="claude -p"

# 方式二：Simon Willison 的 llm CLI（支持多种模型）
# 安装：pip install llm && llm keys set openai
export LLM_CMD="llm -m gpt-4o"

# 方式三：OpenAI 官方 CLI
export LLM_CMD="openai chat -m gpt-4o"

# 方式四：本地模型 via Ollama（免费，离线可用）
# 安装：https://ollama.com
export LLM_CMD="ollama run qwen2.5"
```

> 如果不配置 `LLM_CMD`，脚本只会提取聊天记录（.md），不会生成摘要和 PDF。你可以把聊天记录手动粘贴到任何 AI 对话中总结。

### 5. 运行

```bash
# 手动运行（总结昨天）
bash wechat-digest.sh

# 指定日期
bash wechat-digest.sh 2026-04-09

# 指定语音转写引擎（默认 auto：讯飞 > Whisper > 跳过）
VOICE_ENGINE=whisper bash wechat-digest.sh

# 只提取聊天记录（不做总结）
python3 extract-messages.py "群名" 2026-04-09 --hour-offset 2

# 提取所有私聊记录（按联系人分组，显示备注名）
python3 extract-all-private.py 2026-04-09 --hour-offset 2
python3 extract-all-private.py 2026-04-09 --min-messages 5  # 只输出 ≥5 条消息的对话
python3 extract-all-private.py 2026-04-09 --voice-engine xfyun  # 指定语音引擎

# 语音转写（自动检测可用引擎：讯飞 > Whisper > 跳过）
python3 voice_to_text.py 2026-04-09 --hour-offset 2
python3 voice_to_text.py 2026-04-09 --engine whisper  # 强制用 Whisper

# 提取消息时同时转写语音（--voice-engine 可选 auto/xfyun/whisper/none）
python3 extract-messages.py "群名" 2026-04-09 --hour-offset 2 --voice-engine auto

# 查询公众号文章
python3 biz-articles.py 某公众号 --since 2026-04-01 --format md
python3 biz-articles.py --list  # 列出所有关注的公众号
```

### 6. 配置语音转写（可选）

语音消息会自动从数据库提取 SILK 音频并转为文字。支持两种引擎：

**方式一：讯飞一句话识别（推荐，中文最准）**

注册 [讯飞开放平台](https://www.xfyun.cn/)，创建应用，开通"语音听写"服务，然后设置环境变量：

```bash
export XFYUN_APP_ID="你的AppID"
export XFYUN_API_KEY="你的APIKey"
export XFYUN_API_SECRET="你的APISecret"
```

需要额外安装：`pip install websocket-client`

**方式二：Whisper 本地模型（免费，离线可用）**

```bash
pip install openai-whisper
```

首次运行会下载模型（base 约 140MB）。中文准确率不如讯飞，但无需联网。

**不配置**：语音消息会显示为 `[语音 8秒]`，不影响其他功能。

### 7. 设置每日自动运行（macOS launchd）

创建 `~/Library/LaunchAgents/com.wechat-digest.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.wechat-digest</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>/path/to/wechat-digest.sh</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>9</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>WorkingDirectory</key>
    <string>/path/to/wechat-digest-project</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <!-- 重要：必须包含 python3/pandoc/LLM CLI 所在的路径 -->
        <string>/your/anaconda/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key>
        <string>/Users/your-username</string>
        <key>LLM_CMD</key>
        <string>claude -p</string>
        <!-- 替换为你的 LLM 命令，如 "llm -m gpt-4o"、"ollama run qwen2.5" 等 -->
        <key>VOICE_ENGINE</key>
        <string>auto</string>
        <!-- 语音转写引擎：auto（讯飞>Whisper>跳过）/ xfyun / whisper / none -->
    </dict>
    <key>StandardOutPath</key>
    <string>/path/to/wechat-digest-project/logs/digest.log</string>
    <key>StandardErrorPath</key>
    <string>/path/to/wechat-digest-project/logs/digest-error.log</string>
</dict>
</plist>
```

加载：

```bash
mkdir -p logs
launchctl load ~/Library/LaunchAgents/com.wechat-digest.plist
```

脚本会自动等待微信启动后再运行（最多等 30 分钟）。

### Windows 用户说明

Python 脚本（`init-keys.py`、`extract-messages.py`、`biz-articles.py`）是跨平台的，Windows 可直接使用。密钥提取走的是 Windows API 内存扫描（`scanner_windows.py`），不需要 C 二进制。

`wechat-digest.sh` 是 bash 脚本，Windows 需要做以下适配：

1. **运行环境**：用 Git Bash 或 WSL 运行，或者改写为 `.bat`/PowerShell 脚本
2. **微信进程检测**：`pgrep -x "WeChat"` 改为 `tasklist /FI "IMAGENAME eq Weixin.exe"`
3. **昨天日期**：`date -v-1d` 改为 PowerShell 的 `(Get-Date).AddDays(-1).ToString("yyyy-MM-dd")`
4. **Chrome 路径**：改为 `"C:\Program Files\Google\Chrome\Application\chrome.exe"`（或设置 `CHROME_PATH` 环境变量）
5. **定时任务**：用 Windows 任务计划程序替代 launchd

或者你也可以只用 Python 脚本提取消息，手动喂给任意 AI 总结：

```bash
python extract-messages.py "群名" 2026-04-09 --hour-offset 2 > chat.txt
# 然后把 chat.txt 的内容粘贴到 ChatGPT / Claude / Gemini 等任意 AI 对话中
```

### Linux 用户说明

密钥提取需要 root 权限（通过 `/proc/<pid>/mem` 读取进程内存）：

```bash
sudo python3 init-keys.py
```

`wechat-digest.sh` 基本兼容 Linux，只需注意：
- Chrome 路径改为 `google-chrome` 或 `chromium`（设置 `CHROME_PATH` 环境变量）
- 定时任务用 cron 或 systemd timer 替代 launchd

## 踩坑记录

### 1. wechat-cli history 不稳定
`wechat-cli history` 有时返回 0 条消息。所以我们直接读数据库，不依赖这个命令。

### 2. 消息类型是复合值
微信数据库的 `local_type` 低 32 位才是真实类型。链接消息 type=49，但数据库存的是类似 `244813135921` 的值。必须用 `(local_type & 0xFFFFFFFF) = 49`。

### 3. 消息内容 zstd 压缩
`WCDB_CT_message_content = 4` 表示内容是 zstd 压缩的（WCDB 特性），需要解压后才能读取。

### 4. launchd PATH 缺失
macOS launchd 环境的 PATH 很精简，不包含 homebrew、anaconda 等路径。必须在 plist 里显式设置完整 PATH，否则找不到 `python3` 等命令。

### 5. LLM 会篡改统计数字
在 prompt 里写 `消息总数：510 条`，LLM 可能自作主张改成 `约 280 条`。需要在 prompt 里强调「这个数字是精确统计，请原样使用，不要修改」。

### 6. 微信必须先启动同步
数据库是本地文件。昨晚关机后到今早开机之间的消息，需要微信启动并同步后才会写入数据库。脚本用 `pgrep` 检测微信进程，检测到后等 2 分钟再提取。

### 7. pandoc 重复标题
pandoc 自动生成 `<title>` 和 `<header id="title-block-header">`，导致 PDF 标题重复。用 Python regex 删掉。

### 8. Chrome headless 页眉页脚
Chrome 旧版 `--print-to-pdf-no-header` 不一定生效。用 `--headless=new` + `--no-pdf-header-footer` 才行。

## 项目结构

```
wechat-digest/
├── README.md               # 本文件
├── LICENSE                  # MIT 协议
├── requirements.txt        # Python 依赖
├── prompt-template.txt     # LLM 总结 prompt 模板（可自定义）
├── init-keys.py            # 密钥提取（替代 wechat-cli init）
├── extract-messages.py     # 群聊消息提取（直接读数据库）
├── extract-all-private.py  # 所有私聊消息提取（按联系人分组）
├── voice_to_text.py        # 语音转文字模块（讯飞/Whisper）
├── biz-articles.py         # 公众号文章查询
├── wechat-digest.sh        # 主流程脚本（提取 → 总结 → PDF）
├── crypto/                 # 解密模块（来自 wechat-cli, Apache 2.0）
│   ├── __init__.py
│   ├── decrypt.py          # SQLCipher 4 AES-256-CBC 解密
│   ├── config.py           # 配置加载 + 微信数据目录自动检测
│   └── keys/               # 密钥提取
│       ├── __init__.py     # 平台路由
│       ├── common.py       # 跨平台：HMAC 验证、内存扫描
│       ├── scanner_macos.py
│       ├── scanner_windows.py
│       ├── scanner_linux.py
│       └── bin/
│           ├── find_all_keys_macos.arm64  # macOS ARM 二进制
│           └── find_all_keys_macos.c      # C 源码（参考）
└── output/                 # 输出目录（git ignored）
    ├── 2026-04-09-我的群-聊天记录.md
    ├── 2026-04-09-我的群-摘要.md
    └── 2026-04-09-我的群-摘要.pdf
```

## 安全说明：预编译二进制

> **重要提示**：项目包含一个预编译的 macOS ARM64 二进制文件 `crypto/keys/bin/find_all_keys_macos.arm64`，该文件会读取微信进程内存以提取密钥。**我们强烈建议你从源码自行编译，而非直接信任预编译版本。**

编译方法（一行命令）：

```bash
# macOS ARM (M1/M2/M3)
cc -O2 -o crypto/keys/bin/find_all_keys_macos.arm64 crypto/keys/bin/find_all_keys_macos.c

# macOS Intel
cc -O2 -o crypto/keys/bin/find_all_keys_macos.x86_64 crypto/keys/bin/find_all_keys_macos.c
```

C 源码在 `crypto/keys/bin/find_all_keys_macos.c`，约 300 行，建议审计后再编译使用。Windows 和 Linux 的密钥提取是纯 Python 实现，不涉及编译二进制。

## 致谢

- 数据库解密模块来自 [wechat-cli](https://github.com/freestylefly/wechat-cli)（Apache 2.0 协议）
