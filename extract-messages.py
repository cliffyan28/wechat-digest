#!/usr/bin/env python3
"""
从微信数据库直接提取群聊消息，自动提取链接 URL。

用法：
  python3 extract-messages.py <group_name> <date> [--hour-offset 2]

  --hour-offset N: 时间窗口从当天 N:00 到次日 N:00（默认 0 即 0:00-0:00）

例如：
  python3 extract-messages.py "我的群" 2026-04-09 --hour-offset 2
  # 提取 2026-04-09 02:00 ~ 2026-04-10 02:00 的消息

依赖：
  pip3 install zstandard pycryptodome

原理：
  1. 读取 ~/.wechat-digest/all_keys.json（或 ~/.wechat-cli/all_keys.json）获取解密密钥
  2. 解密 message_0.db（SQLCipher 4, AES-256-CBC）
  3. 群聊消息存在 Msg_{md5(group_username)} 表中
  4. 消息类型是复合值，低 32 位才是真实类型：
     - type 1 = 文本消息
     - type 49 = 链接/小程序等（XML 格式，含 title 和 url）
     查询时用 (local_type & 0xFFFFFFFF) = 49，不能直接 = 49
  5. WCDB_CT_message_content = 4 表示内容是 zstd 压缩的，需要先解压
  6. 链接消息的 URL 从 XML 的 <url> 标签中提取
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile

import zstandard

# 确保能找到 crypto 模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from crypto.decrypt import full_decrypt, decrypt_wal
from crypto.config import load_config


def find_group_username(group_name):
    """通过 wechat-cli sessions 查找群 username（如果 wechat-cli 可用）"""
    try:
        result = subprocess.run(
            ['wechat-cli', 'sessions', '--limit', '1000'],
            capture_output=True, text=True, timeout=30
        )
        sessions = json.loads(result.stdout)
        for s in sessions:
            if s.get('chat') == group_name:
                return s['username']
    except Exception:
        pass
    return None


def extract_messages(group_username, target_date, hour_offset=0):
    """从数据库提取消息，返回格式化的文本行列表"""
    cfg, keys_file = load_config()
    with open(keys_file) as f:
        keys = json.load(f)

    db_dir = cfg['db_dir']
    table_name = 'Msg_' + hashlib.md5(group_username.encode()).hexdigest()
    dctx = zstandard.ZstdDecompressor()

    # 计算时间窗口
    base = datetime.datetime.strptime(target_date, '%Y-%m-%d')
    ts_start = int((base + datetime.timedelta(hours=hour_offset)).timestamp())
    ts_end = int((base + datetime.timedelta(days=1, hours=hour_offset)).timestamp())

    enc_key = bytes.fromhex(keys['message/message_0.db']['enc_key'])
    db_path = os.path.join(db_dir, 'message/message_0.db')
    if not os.path.exists(db_path):
        print(f"数据库不存在: {db_path}", file=sys.stderr)
        return []

    cache_dir = tempfile.mkdtemp(prefix='wechat-extract-')
    out_path = os.path.join(cache_dir, 'dec.db')
    full_decrypt(db_path, out_path, enc_key)
    wal_path = db_path + '-wal'
    if os.path.exists(wal_path):
        decrypt_wal(wal_path, out_path, enc_key)

    conn = sqlite3.connect(out_path)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        ).fetchone()
        if not exists:
            print(f"表不存在: {table_name}", file=sys.stderr)
            return []

        rows = conn.execute(f"""
            SELECT create_time, message_content, WCDB_CT_message_content, local_type
            FROM "{table_name}"
            WHERE create_time >= ? AND create_time < ?
            ORDER BY create_time
        """, (ts_start, ts_end)).fetchall()
    finally:
        conn.close()
        os.remove(out_path)

    output_lines = []
    for ts, content, ct, lt in rows:
        real_type = lt & 0xFFFFFFFF
        if real_type not in (1, 49):
            continue
        if not content:
            continue

        dt_str = datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')

        try:
            if ct == 4:
                text = dctx.decompress(content).decode('utf-8', errors='replace')
            else:
                text = content if isinstance(content, str) else content.decode('utf-8', errors='replace')
        except Exception:
            continue

        if real_type == 1:
            parts = text.split(':\n', 1)
            if len(parts) == 2:
                sender = parts[0].strip()
                msg = parts[1].strip()
            else:
                sender = 'unknown'
                msg = text.strip()
            output_lines.append(f'[{dt_str}] {sender}: {msg}')

        elif real_type == 49:
            title_m = re.search(r'<title><!\[CDATA\[(.*?)\]\]></title>', text, re.DOTALL)
            if not title_m:
                title_m = re.search(r'<title>(.*?)</title>', text, re.DOTALL)
            url_m = re.search(r'<url><!\[CDATA\[(.*?)\]\]></url>', text, re.DOTALL)
            if not url_m:
                url_m = re.search(r'<url>(.*?)</url>', text, re.DOTALL)

            title = title_m.group(1).strip() if title_m else ''
            url = url_m.group(1).strip().replace('&amp;', '&') if url_m else ''

            if not title:
                continue

            sender_m = re.search(r'<fromusername>(.*?)</fromusername>', text)
            sender = sender_m.group(1) if sender_m else 'unknown'

            line = f'[{dt_str}] {sender}: [链接] {title}'
            if url and url.startswith('http'):
                line += f'\n  URL: {url}'
            output_lines.append(line)

    return output_lines


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='从微信数据库提取群聊消息')
    parser.add_argument('group_name', help='群名称')
    parser.add_argument('date', help='目标日期 (YYYY-MM-DD)')
    parser.add_argument('--hour-offset', type=int, default=0,
                        help='时间窗口偏移小时数（默认 0，即 0:00-0:00）')
    args = parser.parse_args()

    # 查找群 username
    group_username = find_group_username(args.group_name)
    if not group_username:
        # 可在此添加已知群的硬编码映射
        known = {}
        group_username = known.get(args.group_name)
    if not group_username:
        print(f"找不到群「{args.group_name}」", file=sys.stderr)
        print("提示：如果没有安装 wechat-cli，请在上方 known 字典中添加群名到 username 的映射", file=sys.stderr)
        print("群 username 格式如：12345678901@chatroom", file=sys.stderr)
        sys.exit(1)

    print(f"群: {args.group_name} ({group_username})", file=sys.stderr)
    print(f"时间窗口: {args.date} {args.hour_offset:02d}:00 ~ +1d {args.hour_offset:02d}:00", file=sys.stderr)

    lines = extract_messages(group_username, args.date, args.hour_offset)
    print(f"提取 {len(lines)} 条消息（文本+链接）", file=sys.stderr)

    print('\n'.join(lines))
