#!/usr/bin/env python3
"""
初始化：从微信进程内存中提取数据库加密密钥。

用法（需要 sudo）：
  sudo python3 init-keys.py
  sudo python3 init-keys.py --db-dir /path/to/db_storage

运行前确保：
  1. 微信已启动并登录
  2. 已安装依赖：pip3 install pycryptodome

运行后会生成：
  ~/.wechat-digest/config.json   — 微信数据目录路径
  ~/.wechat-digest/all_keys.json — 数据库加密密钥

注意：
  - macOS 首次运行可能需要对微信重新签名（脚本会自动处理）
  - 密钥在微信大版本更新或重新登录后可能失效，届时需要重新运行
  - 如果你已经用 wechat-cli init 生成过密钥，本项目会自动读取 ~/.wechat-cli/ 下的配置
"""

import argparse
import json
import os
import sys

# 确保能找到 crypto 模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from crypto.config import STATE_DIR, CONFIG_FILE, KEYS_FILE, auto_detect_db_dir
from crypto.keys import extract_keys


def main():
    parser = argparse.ArgumentParser(description='提取微信数据库加密密钥')
    parser.add_argument('--db-dir', default=None, help='微信数据目录路径（默认自动检测）')
    parser.add_argument('--force', action='store_true', help='强制重新提取密钥')
    args = parser.parse_args()

    print("WeChat Digest 密钥初始化")
    print("=" * 40)

    # 检查是否已初始化
    if os.path.exists(CONFIG_FILE) and os.path.exists(KEYS_FILE) and not args.force:
        print(f"已初始化（配置: {CONFIG_FILE}）")
        print("使用 --force 重新提取密钥")
        return

    # 检查 wechat-cli 的配置是否存在
    wechat_cli_keys = os.path.expanduser("~/.wechat-cli/all_keys.json")
    if os.path.exists(wechat_cli_keys) and not args.force:
        print(f"检测到 wechat-cli 已有密钥: {wechat_cli_keys}")
        print("本项目会自动使用该配置，无需重复初始化。")
        print("如需重新提取，使用 --force")
        return

    os.makedirs(STATE_DIR, exist_ok=True)

    # 确定 db_dir
    db_dir = args.db_dir
    if db_dir is None:
        db_dir = auto_detect_db_dir()
        if db_dir is None:
            print("[!] 未能自动检测到微信数据目录", file=sys.stderr)
            print("请通过 --db-dir 参数指定，例如:", file=sys.stderr)
            print("  sudo python3 init-keys.py --db-dir ~/path/to/db_storage", file=sys.stderr)
            sys.exit(1)
        print(f"[+] 检测到微信数据目录: {db_dir}")
    else:
        db_dir = os.path.abspath(db_dir)
        if not os.path.isdir(db_dir):
            print(f"[!] 目录不存在: {db_dir}", file=sys.stderr)
            sys.exit(1)
        print(f"[+] 使用指定数据目录: {db_dir}")

    # 提取密钥
    print("\n开始提取密钥...")
    try:
        key_map = extract_keys(db_dir, KEYS_FILE)
    except RuntimeError as e:
        print(f"\n[!] 密钥提取失败: {e}", file=sys.stderr)
        sys.exit(1)

    # 写入配置
    cfg = {"db_dir": db_dir}
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

    print(f"\n[+] 初始化完成!")
    print(f"    配置: {CONFIG_FILE}")
    print(f"    密钥: {KEYS_FILE}")
    print(f"    提取到 {len(key_map)} 个数据库密钥")
    print(f"\n现在可以运行：")
    print(f"  python3 extract-messages.py \"群名\" 2026-04-09")
    print(f"  bash wechat-digest.sh")


if __name__ == '__main__':
    main()
