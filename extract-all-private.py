#!/usr/bin/env python3
"""
从微信数据库提取指定日期所有私聊消息。

用法：
  python3 extract-all-private.py 2026-04-09
  python3 extract-all-private.py 2026-04-09 --hour-offset 2
  python3 extract-all-private.py 2026-04-09 --hour-offset 2 --min-messages 3

  --hour-offset N:   时间窗口从当天 N:00 到次日 N:00（默认 0）
  --min-messages N:  只输出消息数 >= N 的聊天（默认 1）

输出格式（按联系人分组，消息多的在前）：

  ## 张三 (wxid_xxx)  [66 条]

  [2026-04-09 10:04] 张三: 你好
  [2026-04-09 10:05] 我: 你好呀
  ...

依赖：pip3 install zstandard pycryptodome
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile

import zstandard

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from crypto.decrypt import full_decrypt, decrypt_wal
from crypto.config import load_config


def load_contact_names(db_dir, keys):
    """从 contact.db 加载 username -> 显示名 的映射"""
    enc_key = bytes.fromhex(keys['contact/contact.db']['enc_key'])
    db_path = os.path.join(db_dir, 'contact/contact.db')
    if not os.path.exists(db_path):
        print("contact.db 不存在，将使用 username 作为显示名", file=sys.stderr)
        return {}

    cache_dir = tempfile.mkdtemp(prefix='wechat-contact-')
    out_path = os.path.join(cache_dir, 'dec.db')
    full_decrypt(db_path, out_path, enc_key)
    wal_path = db_path + '-wal'
    if os.path.exists(wal_path):
        decrypt_wal(wal_path, out_path, enc_key)

    conn = sqlite3.connect(out_path)
    rows = conn.execute("SELECT username, remark, nick_name FROM contact").fetchall()
    conn.close()
    os.remove(out_path)

    names = {}
    for username, remark, nick_name in rows:
        # 优先用备注名，其次昵称
        names[username] = remark or nick_name or username
    return names


def _load_voice_data(db_dir, keys, ts_start, ts_end):
    """从 media_0.db 加载语音二进制数据，返回 {create_time: voice_data}"""
    if 'message/media_0.db' not in keys:
        return {}
    enc_key = bytes.fromhex(keys['message/media_0.db']['enc_key'])
    db_path = os.path.join(db_dir, 'message/media_0.db')
    if not os.path.exists(db_path):
        return {}

    cache_dir = tempfile.mkdtemp(prefix='wechat-voice-')
    out_path = os.path.join(cache_dir, 'dec.db')
    try:
        full_decrypt(db_path, out_path, enc_key)
        wal_path = db_path + '-wal'
        if os.path.exists(wal_path):
            decrypt_wal(wal_path, out_path, enc_key)
        conn = sqlite3.connect(out_path)
        rows = conn.execute("""
            SELECT create_time, voice_data FROM VoiceInfo
            WHERE create_time >= ? AND create_time < ?
        """, (ts_start, ts_end)).fetchall()
        conn.close()
        return {ts: data for ts, data in rows}
    except Exception:
        return {}
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass


def _get_transcriber(voice_engine):
    """按需加载 VoiceTranscriber"""
    try:
        from voice_to_text import VoiceTranscriber
        return VoiceTranscriber(engine=voice_engine)
    except Exception:
        return None


def extract_all_private(target_date, hour_offset=0, min_messages=1, voice_engine='auto'):
    """提取所有私聊消息，返回 {username: [(timestamp, sender, text), ...]}"""
    cfg, keys_file = load_config()
    with open(keys_file) as f:
        keys = json.load(f)

    db_dir = cfg['db_dir']
    dctx = zstandard.ZstdDecompressor()

    # 计算时间窗口
    base = datetime.datetime.strptime(target_date, '%Y-%m-%d')
    ts_start = int((base + datetime.timedelta(hours=hour_offset)).timestamp())
    ts_end = int((base + datetime.timedelta(days=1, hours=hour_offset)).timestamp())

    # 收集所有可用的 message_N.db
    msg_dbs = []
    for key_name, key_info in keys.items():
        if re.match(r'^message/message_\d+\.db$', key_name) and 'enc_key' in key_info:
            db_path = os.path.join(db_dir, key_name)
            if os.path.exists(db_path):
                msg_dbs.append((key_name, db_path, bytes.fromhex(key_info['enc_key'])))

    if not msg_dbs:
        print("未找到可用的消息数据库", file=sys.stderr)
        return {}, {}

    print(f"扫描 {len(msg_dbs)} 个消息数据库...", file=sys.stderr)

    # 加载联系人显示名
    contact_names = load_contact_names(db_dir, keys)

    # 预加载语音数据（所有私聊共享）
    voice_data_map = _load_voice_data(db_dir, keys, ts_start, ts_end)
    transcriber = _get_transcriber(voice_engine) if voice_data_map else None

    # 从所有 db 收集私聊用户和消息
    all_chats = {}  # username -> [(ts, content, ct, lt), ...]
    all_private_users = set()

    for key_name, db_path, enc_key in msg_dbs:
        cache_dir = tempfile.mkdtemp(prefix='wechat-private-')
        out_path = os.path.join(cache_dir, 'dec.db')
        try:
            full_decrypt(db_path, out_path, enc_key)
            wal_path = db_path + '-wal'
            if os.path.exists(wal_path):
                decrypt_wal(wal_path, out_path, enc_key)

            conn = sqlite3.connect(out_path)
            try:
                # 获取此 db 中的非群聊 username
                try:
                    users = conn.execute(
                        "SELECT user_name FROM Name2Id WHERE user_name NOT LIKE '%@chatroom'"
                    ).fetchall()
                except sqlite3.OperationalError:
                    continue

                db_found = 0
                for (username,) in users:
                    all_private_users.add(username)
                    table = 'Msg_' + hashlib.md5(username.encode()).hexdigest()
                    try:
                        rows = conn.execute(f"""
                            SELECT create_time, message_content, WCDB_CT_message_content, local_type
                            FROM "{table}"
                            WHERE create_time >= ? AND create_time < ?
                            ORDER BY create_time
                        """, (ts_start, ts_end)).fetchall()
                    except sqlite3.OperationalError:
                        continue
                    if rows:
                        all_chats.setdefault(username, []).extend(rows)
                        db_found += len(rows)
                if db_found:
                    print(f"  {key_name}: {db_found} 条消息", file=sys.stderr)
            finally:
                conn.close()
        except Exception as e:
            print(f"  {key_name}: 跳过 ({e})", file=sys.stderr)
        finally:
            try:
                os.remove(out_path)
            except OSError:
                pass

    # 处理消息：格式化为文本
    result_chats = {}
    for username, raw_rows in all_chats.items():
        raw_rows.sort(key=lambda r: r[0])
        messages = []
        for ts, content, ct, lt in raw_rows:
            real_type = lt & 0xFFFFFFFF
            if real_type not in (1, 34, 49):
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
                # 私聊文本：没有 "sender:\n" 前缀的是自己发的
                if ':\n' in text:
                    parts = text.split(':\n', 1)
                    sender = contact_names.get(parts[0].strip(), parts[0].strip())
                    msg = parts[1].strip()
                else:
                    sender = '我'
                    msg = text.strip()
                messages.append(f'[{dt_str}] {sender}: {msg}')

            elif real_type == 34:
                # 语音消息
                sender_m = re.search(r'fromusername="(.*?)"', text)
                sender_id = sender_m.group(1) if sender_m else None
                if sender_id:
                    sender = contact_names.get(sender_id, sender_id)
                else:
                    sender = '我'
                length_m = re.search(r'voicelength="(\d+)"', text)
                length_sec = int(length_m.group(1)) / 1000 if length_m else 0

                voice_bytes = voice_data_map.get(ts)
                transcribed = None
                if voice_bytes and transcriber:
                    transcribed = transcriber.transcribe(voice_bytes)

                if transcribed:
                    messages.append(f'[{dt_str}] {sender}: [语音] {transcribed}')
                else:
                    messages.append(f'[{dt_str}] {sender}: [语音 {length_sec:.0f}秒]')

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

                # 判断是对方还是自己发的
                sender_m = re.search(r'<fromusername>(.*?)</fromusername>', text)
                if sender_m:
                    sender_id = sender_m.group(1)
                    sender = contact_names.get(sender_id, sender_id)
                else:
                    sender = '我'

                line = f'[{dt_str}] {sender}: [链接] {title}'
                if url and url.startswith('http'):
                    line += f'\n  URL: {url}'
                messages.append(line)

        if len(messages) >= min_messages:
            result_chats[username] = messages

    all_chats = result_chats

    return all_chats, contact_names


def format_output(all_chats, contact_names):
    """格式化输出，按消息数量降序"""
    sorted_chats = sorted(all_chats.items(), key=lambda x: -len(x[1]))

    lines = []
    total_messages = sum(len(msgs) for msgs in all_chats.values())
    total_chats = len(all_chats)
    lines.append(f'# 私聊汇总：{total_chats} 个对话，{total_messages} 条消息\n')

    for username, messages in sorted_chats:
        display_name = contact_names.get(username, username)
        lines.append(f'## {display_name} ({username})  [{len(messages)} 条]\n')
        lines.extend(messages)
        lines.append('')  # 空行分隔

    return '\n'.join(lines)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='从微信数据库提取所有私聊消息')
    parser.add_argument('date', help='目标日期 (YYYY-MM-DD)')
    parser.add_argument('--hour-offset', type=int, default=0,
                        help='时间窗口偏移小时数（默认 0，即 0:00-0:00）')
    parser.add_argument('--min-messages', type=int, default=1,
                        help='最少消息数，低于此数的对话不输出（默认 1）')
    parser.add_argument('--voice-engine', choices=['auto', 'xfyun', 'whisper', 'none'],
                        default='auto', help='语音转写引擎（默认 auto：讯飞>Whisper>跳过）')
    args = parser.parse_args()

    print(f"时间窗口: {args.date} {args.hour_offset:02d}:00 ~ +1d {args.hour_offset:02d}:00",
          file=sys.stderr)

    all_chats, contact_names = extract_all_private(
        args.date, args.hour_offset, args.min_messages, voice_engine=args.voice_engine
    )

    if not all_chats:
        print(f"{args.date} 没有找到私聊消息", file=sys.stderr)
        sys.exit(0)

    total = sum(len(msgs) for msgs in all_chats.values())
    print(f"提取 {len(all_chats)} 个私聊，共 {total} 条消息", file=sys.stderr)

    print(format_output(all_chats, contact_names))
