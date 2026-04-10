#!/usr/bin/env python3
"""
微信语音转文字模块。

从 media_0.db 提取 SILK 格式语音，解码后调用语音识别 API 转为文字。

转写引擎优先级：
  1. 讯飞一句话识别（需配置环境变量 XFYUN_APP_ID / XFYUN_API_KEY / XFYUN_API_SECRET）
  2. OpenAI Whisper 本地模型（需 pip install openai-whisper）
  3. 都未配置则跳过转写，返回 None

单独使用：
  python3 voice-to-text.py 2026-04-09 --hour-offset 2
  python3 voice-to-text.py 2026-04-09 --engine xfyun    # 强制用讯飞
  python3 voice-to-text.py 2026-04-09 --engine whisper   # 强制用 Whisper

作为模块导入：
  from voice_to_text import VoiceTranscriber
  vt = VoiceTranscriber()
  text = vt.transcribe(silk_bytes)  # 返回文字或 None

依赖：pip3 install pilk zstandard pycryptodome
可选：pip3 install openai-whisper（本地 Whisper）

讯飞 API 文档：https://www.xfyun.cn/doc/asr/voicedictation/API.html
"""

import argparse
import base64
import datetime
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import wave

import pilk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from crypto.decrypt import full_decrypt, decrypt_wal
from crypto.config import load_config


class VoiceTranscriber:
    """语音转文字。自动选择可用引擎：讯飞 > Whisper > None"""

    def __init__(self, engine=None):
        """
        engine: 'xfyun' | 'whisper' | 'auto' (默认)
        """
        self.engine = engine or 'auto'
        self._whisper_model = None
        self._warned = False

        # 检测可用引擎
        if self.engine == 'auto':
            if self._xfyun_available():
                self.engine = 'xfyun'
            elif self._whisper_available():
                self.engine = 'whisper'
            else:
                self.engine = 'none'

        if self.engine == 'xfyun' and not self._xfyun_available():
            print("错误：讯飞 API 未配置（需要 XFYUN_APP_ID, XFYUN_API_KEY, XFYUN_API_SECRET）",
                  file=sys.stderr)
            sys.exit(1)

        if self.engine == 'whisper' and not self._whisper_available():
            print("错误：Whisper 未安装（pip install openai-whisper）", file=sys.stderr)
            sys.exit(1)

    def _xfyun_available(self):
        return all(os.environ.get(k) for k in ('XFYUN_APP_ID', 'XFYUN_API_KEY', 'XFYUN_API_SECRET'))

    def _whisper_available(self):
        try:
            import whisper  # noqa: F401
            return True
        except ImportError:
            return False

    def warn_once(self):
        """首次遇到语音时在 stderr 提示一次"""
        if not self._warned and self.engine == 'none':
            print("检测到语音消息，未配置转写服务，跳过转写。配置方法见 README", file=sys.stderr)
            self._warned = True

    def transcribe(self, silk_data):
        """
        输入 SILK 二进制数据（含或不含 0x02 头），返回文字或 None。
        """
        if self.engine == 'none':
            self.warn_once()
            return None

        wav_data = self._silk_to_wav(silk_data)
        if not wav_data:
            return None

        if self.engine == 'xfyun':
            return self._transcribe_xfyun(wav_data)
        elif self.engine == 'whisper':
            return self._transcribe_whisper(wav_data)
        return None

    def _silk_to_wav(self, silk_data):
        """SILK 二进制 → WAV 二进制"""
        # 去掉微信自定义的 0x02 头部
        if silk_data[0:1] == b'\x02':
            silk_data = silk_data[1:]

        tmp_dir = tempfile.mkdtemp(prefix='wechat-voice-')
        silk_path = os.path.join(tmp_dir, 'voice.silk')
        pcm_path = os.path.join(tmp_dir, 'voice.pcm')
        wav_path = os.path.join(tmp_dir, 'voice.wav')

        try:
            with open(silk_path, 'wb') as f:
                f.write(silk_data)

            pilk.decode(silk_path, pcm_path)

            pcm_data = open(pcm_path, 'rb').read()
            if not pcm_data:
                return None

            with wave.open(wav_path, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)  # 16-bit
                wf.setframerate(24000)  # SILK 默认采样率
                wf.writeframes(pcm_data)

            return open(wav_path, 'rb').read()
        except Exception as e:
            print(f"SILK 解码失败: {e}", file=sys.stderr)
            return None
        finally:
            for p in (silk_path, pcm_path, wav_path):
                try:
                    os.remove(p)
                except OSError:
                    pass
            try:
                os.rmdir(tmp_dir)
            except OSError:
                pass

    def _transcribe_xfyun(self, wav_data):
        """讯飞一句话识别 API"""
        app_id = os.environ['XFYUN_APP_ID']
        api_key = os.environ['XFYUN_API_KEY']
        api_secret = os.environ['XFYUN_API_SECRET']

        # 构建鉴权 URL
        url = 'wss://iat-api.xfyun.cn/v2/iat'
        # 讯飞一句话识别也支持 HTTP POST 方式，更简单
        # 使用 REST API: https://iat-api.xfyun.cn/v2/iat
        # 这里用 HTTP 接口更简单，不需要 WebSocket

        host = 'iat-api.xfyun.cn'
        path = '/v2/iat'
        now = datetime.datetime.now(datetime.timezone.utc)
        date = now.strftime('%a, %d %b %Y %H:%M:%S GMT')

        # 构建签名
        signature_origin = f"host: {host}\ndate: {date}\nGET {path} HTTP/1.1"
        signature_sha = hmac.new(
            api_secret.encode(), signature_origin.encode(), hashlib.sha256
        ).digest()
        signature = base64.b64encode(signature_sha).decode()

        authorization_origin = (
            f'api_key="{api_key}", algorithm="hmac-sha256", '
            f'headers="host date request-line", signature="{signature}"'
        )
        authorization = base64.b64encode(authorization_origin.encode()).decode()

        # 构建 WebSocket URL
        ws_url = (
            f"wss://{host}{path}"
            f"?authorization={urllib.parse.quote(authorization)}"
            f"&date={urllib.parse.quote(date)}"
            f"&host={urllib.parse.quote(host)}"
        )

        # 使用 WebSocket 发送音频
        try:
            import websocket
        except ImportError:
            # 没有 websocket-client，用 HTTP 方式 fallback
            return self._transcribe_xfyun_http(wav_data, app_id, api_key, api_secret)

        result_text = []
        ws_finished = [False]

        def on_message(ws, message):
            data = json.loads(message)
            if data.get('code') != 0:
                print(f"讯飞 API 错误: {data.get('message')}", file=sys.stderr)
                ws_finished[0] = True
                return
            result = data.get('data', {}).get('result', {})
            if result:
                for w in result.get('ws', []):
                    for cw in w.get('cw', []):
                        result_text.append(cw.get('w', ''))
            if data.get('data', {}).get('status') == 2:
                ws_finished[0] = True

        def on_error(ws, error):
            print(f"讯飞 WebSocket 错误: {error}", file=sys.stderr)
            ws_finished[0] = True

        def on_open(ws):
            # 发送音频数据，每帧 1280 bytes
            frame_size = 1280
            status = 0  # 0=first, 1=continue, 2=last
            i = 0
            while i < len(wav_data):
                chunk = wav_data[i:i + frame_size]
                i += frame_size
                if i >= len(wav_data):
                    status = 2
                data = {
                    "common": {"app_id": app_id} if status == 0 else None,
                    "business": {
                        "language": "zh_cn",
                        "domain": "iat",
                        "accent": "mandarin",
                        "vad_eos": 3000,
                    } if status == 0 else None,
                    "data": {
                        "status": status,
                        "format": "audio/L16;rate=24000",
                        "encoding": "raw",
                        "audio": base64.b64encode(chunk).decode(),
                    }
                }
                # 去掉 None 值
                data = {k: v for k, v in data.items() if v is not None}
                ws.send(json.dumps(data))
                if status == 0:
                    status = 1
                time.sleep(0.04)  # 40ms 间隔，避免发太快

        ws = websocket.WebSocketApp(
            ws_url,
            on_message=on_message,
            on_error=on_error,
            on_open=on_open,
        )
        ws.run_forever(ping_timeout=10)

        text = ''.join(result_text).strip()
        return text if text else None

    def _transcribe_xfyun_http(self, wav_data, app_id, api_key, api_secret):
        """讯飞 HTTP 方式（不依赖 websocket-client）"""
        # 讯飞一句话识别 WebAPI
        # https://www.xfyun.cn/doc/asr/voicedictation/API.html
        base_url = "https://iat-api.xfyun.cn/v2/iat"
        host = "iat-api.xfyun.cn"
        path = "/v2/iat"

        now = datetime.datetime.now(datetime.timezone.utc)
        date = now.strftime('%a, %d %b %Y %H:%M:%S GMT')

        signature_origin = f"host: {host}\ndate: {date}\nGET {path} HTTP/1.1"
        signature_sha = hmac.new(
            api_secret.encode(), signature_origin.encode(), hashlib.sha256
        ).digest()
        signature = base64.b64encode(signature_sha).decode()

        authorization_origin = (
            f'api_key="{api_key}", algorithm="hmac-sha256", '
            f'headers="host date request-line", signature="{signature}"'
        )
        authorization = base64.b64encode(authorization_origin.encode()).decode()

        # 讯飞 IAT 只支持 WebSocket，HTTP fallback 不可用
        # 提示用户安装 websocket-client
        print("请安装 websocket-client: pip install websocket-client", file=sys.stderr)
        return None

    def _transcribe_whisper(self, wav_data):
        """Whisper 本地转写"""
        if self._whisper_model is None:
            import whisper
            print("加载 Whisper 模型（首次较慢）...", file=sys.stderr)
            self._whisper_model = whisper.load_model("base")

        tmp_path = tempfile.mktemp(suffix='.wav', prefix='wechat-whisper-')
        try:
            with open(tmp_path, 'wb') as f:
                f.write(wav_data)
            result = self._whisper_model.transcribe(tmp_path, language='zh')
            text = result.get('text', '').strip()
            return text if text else None
        except Exception as e:
            print(f"Whisper 转写失败: {e}", file=sys.stderr)
            return None
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def load_voice_data(target_date, hour_offset=0):
    """从 media_0.db 提取语音数据，返回 [(create_time, voice_data, voicelength_ms), ...]"""
    import zstandard
    cfg, keys_file = load_config()
    with open(keys_file) as f:
        keys = json.load(f)

    db_dir = cfg['db_dir']

    base = datetime.datetime.strptime(target_date, '%Y-%m-%d')
    ts_start = int((base + datetime.timedelta(hours=hour_offset)).timestamp())
    ts_end = int((base + datetime.timedelta(days=1, hours=hour_offset)).timestamp())

    # 解密 media_0.db
    enc_key = bytes.fromhex(keys['message/media_0.db']['enc_key'])
    db_path = os.path.join(db_dir, 'message/media_0.db')
    if not os.path.exists(db_path):
        print(f"数据库不存在: {db_path}", file=sys.stderr)
        return []

    cache_dir = tempfile.mkdtemp(prefix='wechat-voice-')
    out_path = os.path.join(cache_dir, 'dec.db')
    full_decrypt(db_path, out_path, enc_key)
    wal_path = db_path + '-wal'
    if os.path.exists(wal_path):
        decrypt_wal(wal_path, out_path, enc_key)

    conn = sqlite3.connect(out_path)
    rows = conn.execute("""
        SELECT create_time, voice_data
        FROM VoiceInfo
        WHERE create_time >= ? AND create_time < ?
        ORDER BY create_time
    """, (ts_start, ts_end)).fetchall()
    conn.close()
    os.remove(out_path)

    # 同时从 message_0.db 拿语音的元数据（时长、发送者）
    enc_key_msg = bytes.fromhex(keys['message/message_0.db']['enc_key'])
    db_path_msg = os.path.join(db_dir, 'message/message_0.db')
    cache_dir2 = tempfile.mkdtemp(prefix='wechat-voice-msg-')
    out_path2 = os.path.join(cache_dir2, 'dec.db')
    full_decrypt(db_path_msg, out_path2, enc_key_msg)
    wal_path2 = db_path_msg + '-wal'
    if os.path.exists(wal_path2):
        decrypt_wal(wal_path2, out_path2, enc_key_msg)

    msg_conn = sqlite3.connect(out_path2)
    dctx = zstandard.ZstdDecompressor()

    # 建立 create_time -> voicelength 映射
    voice_meta = {}
    tables = msg_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
    ).fetchall()
    import re
    for (tname,) in tables:
        try:
            msg_rows = msg_conn.execute(f"""
                SELECT create_time, message_content, WCDB_CT_message_content
                FROM "{tname}"
                WHERE create_time >= ? AND create_time < ?
                AND (local_type & 0xFFFFFFFF) = 34
            """, (ts_start, ts_end)).fetchall()
        except sqlite3.OperationalError:
            continue

        for ts, content, ct in msg_rows:
            try:
                if ct == 4:
                    text = dctx.decompress(content).decode('utf-8', errors='replace')
                else:
                    text = content if isinstance(content, str) else content.decode('utf-8', errors='replace')
                length_m = re.search(r'voicelength="(\d+)"', text)
                sender_m = re.search(r'fromusername="(.*?)"', text)
                voice_meta[ts] = {
                    'length_ms': int(length_m.group(1)) if length_m else 0,
                    'sender': sender_m.group(1) if sender_m else 'unknown',
                }
            except Exception:
                pass

    msg_conn.close()
    os.remove(out_path2)

    result = []
    for ts, voice_data in rows:
        meta = voice_meta.get(ts, {'length_ms': 0, 'sender': 'unknown'})
        result.append({
            'create_time': ts,
            'voice_data': voice_data,
            'length_ms': meta['length_ms'],
            'sender': meta['sender'],
        })

    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='微信语音转文字')
    parser.add_argument('date', help='目标日期 (YYYY-MM-DD)')
    parser.add_argument('--hour-offset', type=int, default=0,
                        help='时间窗口偏移小时数（默认 0）')
    parser.add_argument('--engine', choices=['auto', 'xfyun', 'whisper'],
                        default='auto', help='转写引擎（默认 auto）')
    args = parser.parse_args()

    print(f"时间窗口: {args.date} {args.hour_offset:02d}:00 ~ +1d {args.hour_offset:02d}:00",
          file=sys.stderr)

    voices = load_voice_data(args.date, args.hour_offset)
    if not voices:
        print(f"{args.date} 没有语音消息", file=sys.stderr)
        sys.exit(0)

    print(f"找到 {len(voices)} 条语音消息", file=sys.stderr)

    vt = VoiceTranscriber(engine=args.engine)
    print(f"转写引擎: {vt.engine}", file=sys.stderr)

    success = 0
    for v in voices:
        dt_str = datetime.datetime.fromtimestamp(v['create_time']).strftime('%Y-%m-%d %H:%M')
        length_sec = v['length_ms'] / 1000

        text = vt.transcribe(v['voice_data'])
        if text:
            print(f"[{dt_str}] {v['sender']}: [语音] {text}")
            success += 1
        else:
            print(f"[{dt_str}] {v['sender']}: [语音 {length_sec:.0f}秒]")

    print(f"\n转写完成: {success}/{len(voices)} 条成功", file=sys.stderr)
