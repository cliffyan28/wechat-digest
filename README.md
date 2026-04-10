# WeChat Group Digest

微信群聊每日摘要自动化工具。从微信本地加密数据库提取聊天记录，用 Claude 生成结构化摘要，输出 Markdown + PDF。

## 效果

每天自动生成三个文件：
- `2026-04-09-群名-聊天记录.md` — 原始聊天记录（含链接 URL）
- `2026-04-09-群名-摘要.md` — 结构化摘要（Tips / 热点讨论 / 推文分享）
- `2026-04-09-群名-摘要.pdf` — PDF 版摘要，可直接分享

## 原理

```
微信本地加密数据库 (SQLCipher 4)
        ↓ init-keys.py（从微信进程内存提取密钥）
解密密钥 (~/.wechat-digest/all_keys.json)
        ↓ extract-messages.py（解密 + 提取文本/链接消息）
格式化聊天记录（含 URL）
        ↓ claude -p（Claude CLI 结构化总结）
Markdown 摘要
        ↓ pandoc + Chrome headless
PDF
```

## 快速开始

### 1. 安装依赖

```bash
# Python 依赖
pip3 install pycryptodome zstandard

# Claude CLI（用于 AI 总结）
npm install -g @anthropic-ai/claude-code

# pandoc（Markdown → HTML）
brew install pandoc  # macOS
# apt install pandoc  # Linux

# Google Chrome（PDF 生成，通常已有）
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

### 4. 运行

```bash
# 手动运行（总结昨天）
bash wechat-digest.sh

# 指定日期
bash wechat-digest.sh 2026-04-09

# 只提取聊天记录（不做总结）
python3 extract-messages.py "群名" 2026-04-09 --hour-offset 2

# 查询公众号文章
python3 biz-articles.py 某公众号 --since 2026-04-01 --format md
python3 biz-articles.py --list  # 列出所有关注的公众号
```

### 5. 设置每日自动运行（macOS launchd）

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
        <!-- 重要：必须包含 python3/claude/pandoc 所在的路径 -->
        <string>/your/anaconda/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key>
        <string>/Users/your-username</string>
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

## 踩坑记录

### 1. wechat-cli history 不稳定
`wechat-cli history` 有时返回 0 条消息。所以我们直接读数据库，不依赖这个命令。

### 2. 消息类型是复合值
微信数据库的 `local_type` 低 32 位才是真实类型。链接消息 type=49，但数据库存的是类似 `244813135921` 的值。必须用 `(local_type & 0xFFFFFFFF) = 49`。

### 3. 消息内容 zstd 压缩
`WCDB_CT_message_content = 4` 表示内容是 zstd 压缩的（WCDB 特性），需要解压后才能读取。

### 4. launchd PATH 缺失
macOS launchd 环境的 PATH 很精简，不包含 homebrew、anaconda 等路径。必须在 plist 里显式设置完整 PATH，否则找不到 `python3`、`claude` 等命令。

### 5. Claude 会篡改统计数字
在 prompt 里写 `消息总数：510 条`，Claude 可能自作主张改成 `约 280 条`。需要在 prompt 里强调「这个数字是精确统计，请原样使用，不要修改」。

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
├── requirements.txt        # Python 依赖
├── init-keys.py            # 密钥提取（替代 wechat-cli init）
├── extract-messages.py     # 群聊消息提取（直接读数据库）
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

## 关于编译二进制

项目包含一个预编译的 macOS ARM64 二进制文件 `crypto/keys/bin/find_all_keys_macos.arm64`，用于从微信进程内存中扫描密钥。如果你不信任预编译二进制，可以从源码自行编译：

```bash
# macOS ARM (M1/M2/M3)
cc -O2 -o crypto/keys/bin/find_all_keys_macos.arm64 crypto/keys/bin/find_all_keys_macos.c

# macOS Intel
cc -O2 -o crypto/keys/bin/find_all_keys_macos.x86_64 crypto/keys/bin/find_all_keys_macos.c
```

C 源码在 `crypto/keys/bin/find_all_keys_macos.c`，可自行审计。

## 致谢

- 数据库解密模块来自 [wechat-cli](https://github.com/freestylefly/wechat-cli)（Apache 2.0 协议）
- AI 总结由 [Claude](https://claude.ai) 驱动
