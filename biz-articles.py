#!/usr/bin/env python3
"""
查询关注的微信公众号文章列表

用法：
  python3 biz-articles.py 某公众号                # 最近 20 篇
  python3 biz-articles.py 某公众号 --since 2026-04-01
  python3 biz-articles.py 某公众号 --limit 50
  python3 biz-articles.py --list                      # 列出所有公众号
  python3 biz-articles.py 某公众号 --format md     # 输出 markdown 表格

原理：
  公众号消息存在 biz_message_*.db 中（与群聊的 message_*.db 不同）。
  每个公众号的消息表为 Msg_{md5(gh_username)}。
  消息格式同群聊链接消息：XML 格式，含 <title>、<url>、<des> 标签。
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

import atexit
import shutil

import zstandard

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from crypto.decrypt import full_decrypt, decrypt_wal
from crypto.config import load_config as _load_config

CACHE_DIR = tempfile.mkdtemp(prefix='wechat-biz-')
atexit.register(shutil.rmtree, CACHE_DIR, True)  # 退出时清理解密的临时数据库


def load_config():
    cfg, keys_file = _load_config()
    with open(keys_file) as f:
        keys = json.load(f)
    return cfg['db_dir'], keys


def decrypt_db(db_dir, keys, db_name):
    """解密一个数据库，返回解密后的临时路径"""
    if db_name not in keys:
        return None
    enc_key = bytes.fromhex(keys[db_name]['enc_key'])
    db_path = os.path.join(db_dir, db_name)
    if not os.path.exists(db_path):
        return None
    out_path = os.path.join(CACHE_DIR, db_name.replace('/', '_'))
    full_decrypt(db_path, out_path, enc_key)
    wal_path = db_path + '-wal'
    if os.path.exists(wal_path):
        decrypt_wal(wal_path, out_path, enc_key)
    return out_path


def find_biz_account(db_dir, keys, name):
    """在 contact.db / session.db 中查找公众号的 username"""
    for db_name in ['session/session.db', 'contact/contact.db']:
        dec_path = decrypt_db(db_dir, keys, db_name)
        if not dec_path:
            continue
        conn = sqlite3.connect(dec_path)
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            for t in tables:
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info('{t}')").fetchall()]
                if 'username' not in cols:
                    continue
                name_col = None
                for c in ['display_name', 'nickname', 'remark', 'chat']:
                    if c in cols:
                        name_col = c
                        break
                if not name_col:
                    continue
                rows = conn.execute(
                    f"SELECT username FROM '{t}' WHERE {name_col} = ?", (name,)
                ).fetchall()
                if rows:
                    return rows[0][0]
        except Exception:
            pass
        finally:
            conn.close()
    return None


def find_biz_username_from_sessions(name):
    """从 wechat-cli 的 sessions 输出找 username（如果 wechat-cli 可用）"""
    try:
        out = subprocess.check_output(
            ['wechat-cli', 'sessions', '--limit', '1000'],
            stderr=subprocess.DEVNULL, text=True
        )
        data = json.loads(out)
        for s in data:
            if s.get('chat') == name:
                return s['username']
    except Exception:
        pass
    return None


def list_all_biz_accounts(db_dir, keys):
    """列出所有有消息的公众号"""
    accounts = []
    try:
        out = subprocess.check_output(
            ['wechat-cli', 'sessions', '--limit', '500'],
            stderr=subprocess.DEVNULL, text=True
        )
        data = json.loads(out)
        for s in data:
            uname = s.get('username', '')
            if uname.startswith('gh_') and not s.get('is_group'):
                accounts.append({
                    'name': s['chat'],
                    'username': uname,
                    'last_msg': s.get('last_message', ''),
                    'time': s.get('time', ''),
                })
    except Exception:
        pass
    return accounts


def get_articles(db_dir, keys, username, since_ts=0, limit=20):
    """从 biz_message 数据库中提取文章"""
    table_name = 'Msg_' + hashlib.md5(username.encode()).hexdigest()
    dctx = zstandard.ZstdDecompressor()
    articles = []

    for i in range(10):
        db_name = f'message/biz_message_{i}.db'
        dec_path = decrypt_db(db_dir, keys, db_name)
        if not dec_path:
            continue
        conn = sqlite3.connect(dec_path)
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,)
            ).fetchone()
            if not exists:
                continue

            rows = conn.execute(f"""
                SELECT create_time, message_content, WCDB_CT_message_content, local_type
                FROM '{table_name}'
                WHERE create_time >= ?
                ORDER BY create_time DESC
            """, (since_ts,)).fetchall()

            for ts, content, ct, local_type in rows:
                if not content:
                    continue
                try:
                    if ct == 4:
                        xml = dctx.decompress(content).decode('utf-8', errors='replace')
                    else:
                        xml = content if isinstance(content, str) else content.decode('utf-8', errors='replace')
                except Exception:
                    continue

                if '<appmsg' not in xml:
                    continue

                title_m = re.search(r'<title><!\[CDATA\[(.*?)\]\]></title>', xml, re.DOTALL)
                if not title_m:
                    title_m = re.search(r'<title>(.*?)</title>', xml, re.DOTALL)
                url_m = re.search(r'<url><!\[CDATA\[(.*?)\]\]></url>', xml, re.DOTALL)
                if not url_m:
                    url_m = re.search(r'<url>(.*?)</url>', xml, re.DOTALL)
                des_m = re.search(r'<des><!\[CDATA\[(.*?)\]\]></des>', xml, re.DOTALL)
                if not des_m:
                    des_m = re.search(r'<des>(.*?)</des>', xml, re.DOTALL)

                title = title_m.group(1).strip() if title_m else ''
                url = url_m.group(1).strip().replace('&amp;', '&') if url_m else ''
                des = des_m.group(1).strip() if des_m else ''
                dt = datetime.datetime.fromtimestamp(ts)

                if title:
                    articles.append({
                        'date': dt.strftime('%Y-%m-%d'),
                        'time': dt.strftime('%H:%M'),
                        'title': title,
                        'url': url,
                        'des': des,
                    })
        except Exception as e:
            print(f"警告: {db_name} 查询失败: {e}", file=sys.stderr)
        finally:
            conn.close()

    articles.sort(key=lambda a: a['date'] + a['time'], reverse=True)
    return articles[:limit] if limit else articles


def main():
    parser = argparse.ArgumentParser(description='查询微信公众号文章')
    parser.add_argument('name', nargs='?', help='公众号名称')
    parser.add_argument('--since', help='起始日期 YYYY-MM-DD')
    parser.add_argument('--limit', type=int, default=20, help='最多返回条数（默认 20，0=不限）')
    parser.add_argument('--list', action='store_true', help='列出所有关注的公众号')
    parser.add_argument('--format', choices=['text', 'md', 'json'], default='text', help='输出格式')
    args = parser.parse_args()

    db_dir, keys = load_config()

    if args.list:
        accounts = list_all_biz_accounts(db_dir, keys)
        if not accounts:
            print('未找到公众号会话（需要 wechat-cli 支持 --list 功能）')
            return
        print(f'找到 {len(accounts)} 个公众号：\n')
        for a in accounts:
            print(f"  {a['name']:20s}  {a['time']}  {a['last_msg'][:40]}")
        return

    if not args.name:
        parser.print_help()
        return

    # 查找 username
    username = find_biz_username_from_sessions(args.name)
    if not username:
        username = find_biz_account(db_dir, keys, args.name)
    if not username:
        if args.name.startswith('gh_'):
            username = args.name
        else:
            print(f'找不到公众号「{args.name}」', file=sys.stderr)
            print('提示：可在脚本中硬编码 gh_ 格式的 username', file=sys.stderr)
            return

    since_ts = 0
    if args.since:
        since_ts = int(datetime.datetime.strptime(args.since, '%Y-%m-%d').timestamp())

    articles = get_articles(db_dir, keys, username, since_ts, args.limit or 0)

    if not articles:
        print(f'未找到「{args.name}」的文章')
        return

    if args.format == 'json':
        print(json.dumps(articles, ensure_ascii=False, indent=2))
    elif args.format == 'md':
        print(f'# {args.name} 文章列表\n')
        print(f'| 日期 | 标题 | 链接 |')
        print(f'|------|------|------|')
        for a in articles:
            link = f'[阅读]({a["url"]})' if a['url'] else ''
            print(f'| {a["date"]} | {a["title"]} | {link} |')
    else:
        for a in articles:
            print(f'[{a["date"]} {a["time"]}] {a["title"]}')
            if a['url']:
                print(f'  {a["url"]}')
            print()


if __name__ == '__main__':
    main()
