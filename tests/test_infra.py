"""Automated Unit Tests for mambotts/infra/（spec item 24）。

覆盖：
- detect_language（「阿尼亚・福杰」「进击の巨人」保持 zh，日文句子判为 ja）；
- looks_like_wav（RIFF/WAVE 结构校验）；
- GSVHttpClient 对本地 mock HTTP 服务（/control JSON 校验、默认模型跳过
  /set_model、自定义模型热切换、中途 cancel_event 的 .part 清理）；
- download_file_resumable（Mirror-1 探测失败绝不截断 .part，Mirror-2 从
  断点续传；超长断点重置；首块落地前不留 0 字节 .part；中途取消保留断点）；
- is_local_api_url（含畸形端口的 ValueError 安全）；
- EngineSupervisor busy/online 状态解耦（心跳互锁）。

引擎联调用真实 mock http.server（ThreadingHTTPServer），不依赖本地引擎。
"""
from __future__ import annotations

import glob
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _plenty_disk_usage(*_args, **_kwargs) -> SimpleNamespace:
    """Return synthetic 90 GiB free space so install_engine tests are CI-disk-independent."""
    return SimpleNamespace(total=100 * 1024**3, used=10 * 1024**3, free=90 * 1024**3)

from mambotts.domain.models import DEFAULT_REF_TEXT, VoicePreset
from mambotts.infra.engine_mgr import EngineSupervisor, is_local_api_url, port_in_use
from mambotts.infra.gsv_client import (
    GSVHttpClient,
    TTSClient,
    _is_japanese,
    _looks_like_wav,
    detect_language,
    looks_like_wav,
)
from mambotts.infra.installer import (
    BUILTIN_SOURCES,
    GPU_PACKAGES,
    build_source_urls,
    download_file_resumable,
    load_mirror_urls,
)


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------

def _make_wav(frames=32000, rate=32000):
    """构造一个合法的 RIFF/WAVE mono 16bit WAV 字节串。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x01\x02" * frames)
    return buf.getvalue()


def _noop_progress(p):
    pass


class _CollectLog(list):
    def __call__(self, msg):
        self.append(msg)


# ----------------------------------------------------------------------
# Mock HTTP 服务器（模拟 GPT-SoVITS api.py 与镜像下载源）
# ----------------------------------------------------------------------

class _ServerState:
    """可按用例动态调整的 mock 行为开关。"""

    def __init__(self, tts_wav):
        self.control_mode = "json"      # json | html | notjson404
        self.tts_mode = "wav"           # wav | slow_wav | notwav | error500
        self.tts_wav = tts_wav
        self.set_model_calls = []
        self.tts_calls = []
        self.file_bytes = b""
        self.range_ok = True            # False 时按 fail_status 应答（模拟坏镜像）
        self.fail_status = 404
        self.serve_delay = 0.0          # TTS 流式发送的块间延时（秒）


class _MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send_bytes(self, status, body, ctype="application/octet-stream", headers=None):
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass  # 客户端中途断开（取消场景）属正常

    def do_GET(self):
        st = self.server.state
        path = self.path.split("?", 1)[0]
        if path == "/control":
            if st.control_mode == "json":
                self._send_bytes(200, b'{"status": "ok"}', "application/json")
            elif st.control_mode == "html":
                self._send_bytes(200, b"<html><body>not the engine</body></html>", "text/html")
            else:
                self._send_bytes(404, b"plain text, not json", "text/plain")
        elif path == "/download":
            self._serve_download()
        else:
            self._send_bytes(404, b"no route", "text/plain")

    def do_POST(self):
        st = self.server.state
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            body = {}
        if path == "/set_model":
            st.set_model_calls.append(body)
            self._send_bytes(200, b'{"message": "ok"}', "application/json")
        elif path == "/":
            st.tts_calls.append(body)
            self._serve_tts()
        else:
            self._send_bytes(404, b"no route", "text/plain")

    def _serve_tts(self):
        st = self.server.state
        if st.tts_mode == "error500":
            self._send_bytes(500, b"engine internal error", "text/plain")
            return
        if st.tts_mode == "notwav":
            # 200 但内容是 JSON 错误页：looks_like_wav 必须拒绝
            self._send_bytes(200, b'{"error": "not audio"}', "application/json")
            return
        wav = st.tts_wav
        chunk = 4096
        try:
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(wav)))
            self.end_headers()
            for i in range(0, len(wav), chunk):
                self.wfile.write(wav[i:i + chunk])
                self.wfile.flush()
                if st.serve_delay > 0:
                    time.sleep(st.serve_delay)
        except OSError:
            pass  # 客户端取消后断开连接

    def _serve_download(self):
        st = self.server.state
        data = st.file_bytes
        rng = self.headers.get("Range")
        if not st.range_ok:
            self._send_bytes(st.fail_status, b"mirror down", "text/plain")
            return
        if rng and rng.startswith("bytes="):
            spec = rng[len("bytes="):].split(",")[0]
            first, _, last = spec.partition("-")
            try:
                start = int(first) if first else 0
                end = int(last) if last else len(data) - 1
            except ValueError:
                self._send_bytes(416, b"bad range", "text/plain")
                return
            end = min(end, len(data) - 1)
            if start >= len(data) or start > end:
                self._send_bytes(416, b"range not satisfiable", "text/plain")
                return
            body = data[start:end + 1]
            self._send_bytes(206, body, "application/octet-stream",
                             headers={"Content-Range": f"bytes {start}-{end}/{len(data)}"})
        else:
            self._send_bytes(200, data, "application/octet-stream")


class _MockServer:
    def __init__(self, state):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _MockHandler)
        self.httpd.daemon_threads = True
        self.httpd.state = state
        self.state = state  # 同一 _ServerState 实例：setUp/用例与 handler 共享
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.port}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# ----------------------------------------------------------------------
# detect_language / _is_japanese
# ----------------------------------------------------------------------

class TestDetectLanguage(unittest.TestCase):

    def test_chinese_name_with_middle_dot_stays_zh(self):
        # 「・」是 U+30FB（中点），被排除出假名统计；其余为汉字 → zh
        self.assertEqual(detect_language("阿尼亚・福杰"), "zh")

    def test_single_kana_in_longer_text_stays_zh(self):
        # 「の」是假名但只有 1 个，且总长 > 4，两条规则都不满足 → zh
        self.assertEqual(detect_language("进击の巨人"), "zh")

    def test_japanese_sentence_detected(self):
        self.assertEqual(detect_language("こんにちは、今日もいい天気ですね。"), "ja")

    def test_short_kana_word_detected(self):
        # 短文本分支：<= 4 字且假名占比 >= 50%
        self.assertEqual(detect_language("私は"), "ja")
        self.assertEqual(detect_language("それな"), "ja")

    def test_plain_chinese_stays_zh(self):
        self.assertEqual(detect_language("大家好，今天给大家分享一个有趣的内容"), "zh")

    def test_long_sound_mark_excluded(self):
        # 「ー」U+30FC（长音符号）与「・」U+30FB 单独出现不计入假名 → zh
        self.assertEqual(detect_language("ー"), "zh")
        self.assertEqual(detect_language("・"), "zh")

    def test_explicit_default_lang_passthrough(self):
        self.assertEqual(detect_language("こんにちは、世界", "en"), "en")
        self.assertEqual(detect_language("こんにちは、世界", "ja"), "ja")

    def test_empty_and_whitespace(self):
        self.assertEqual(detect_language(""), "zh")
        self.assertEqual(detect_language("   "), "zh")

    def test_is_japanese_helper(self):
        self.assertTrue(_is_japanese("進撃の巨人"))
        self.assertTrue(_is_japanese("カタカナ"))
        self.assertFalse(_is_japanese("大家好"))
        self.assertFalse(_is_japanese(""))


# ----------------------------------------------------------------------
# looks_like_wav
# ----------------------------------------------------------------------

class TestLooksLikeWav(unittest.TestCase):

    def setUp(self):
        self.wav = _make_wav(frames=256)

    def test_valid_wav_accepted(self):
        self.assertTrue(looks_like_wav(self.wav))
        self.assertTrue(looks_like_wav(self.wav[:100]))  # 只有头也认（声明非空即认可）

    def test_json_error_page_rejected(self):
        self.assertFalse(looks_like_wav(b'{"error": "boom"}'))

    def test_empty_and_truncated_rejected(self):
        self.assertFalse(looks_like_wav(b""))
        self.assertFalse(looks_like_wav(self.wav[:10]))   # < 44 字节
        self.assertFalse(looks_like_wav(b"RIFFxxxxWAVE")) # 假头（8:12 是 WAVE 但太短）

    def test_missing_data_chunk_rejected(self):
        # RIFF/WAVE 头 + 只有 fmt 块且声明巨大（8KB 内无 data 块）
        fake = b"RIFF" + (9999).to_bytes(4, "little") + b"WAVE"
        fake += b"fmt " + (16).to_bytes(4, "little") + b"\x00" * 16
        fake += b"LIST" + (65536).to_bytes(4, "little") + b"\x00" * 64
        self.assertFalse(looks_like_wav(fake))

    def test_legacy_alias(self):
        self.assertIs(_looks_like_wav, looks_like_wav)


# ----------------------------------------------------------------------
# GSVHttpClient（对 mock 引擎）
# ----------------------------------------------------------------------

class TestGSVHttpClient(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.wav = _make_wav(frames=128000)  # ~256KB，够慢流式取消测试用
        cls.state = _ServerState(tts_wav=cls.wav)
        cls.server = _MockServer(cls.state)
        cls.tmp_project = tempfile.mkdtemp(prefix="mambotts_gsv_test_")

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        shutil.rmtree(cls.tmp_project, ignore_errors=True)

    def setUp(self):
        st = self.server.state
        st.control_mode = "json"
        st.tts_mode = "wav"
        st.serve_delay = 0.0
        st.set_model_calls.clear()
        st.tts_calls.clear()
        self.out_dir = tempfile.mkdtemp(prefix="mambotts_out_")
        self.addCleanup(shutil.rmtree, self.out_dir, ignore_errors=True)

    def _default_preset(self):
        return VoicePreset(
            id="mambo:default", character="mambo", emotion="default",
            ref_wav=os.path.join(self.tmp_project, "models", "refer.wav"),
            ref_text="大家好，欢迎来到我的频道",
            ref_lang="zh", gpt_model="", sovits_model="",
        )

    def _client(self):
        return GSVHttpClient(api_url=self.server.base_url, project_dir=self.tmp_project)

    # ---- /control JSON 校验 ----

    def test_is_api_running_json_ok(self):
        self.assertTrue(self._client().is_api_running())

    def test_is_api_running_html_rejected(self):
        self.server.state.control_mode = "html"
        self.assertFalse(self._client().is_api_running())

    def test_is_api_running_non_json_404_rejected(self):
        self.server.state.control_mode = "notjson404"
        self.assertFalse(self._client().is_api_running())

    def test_is_api_running_offline(self):
        client = GSVHttpClient(api_url="http://127.0.0.1:1", project_dir=self.tmp_project)
        self.assertFalse(client.is_api_running(timeout=0.3))

    # ---- 默认模型跳过 /set_model ----

    def test_default_preset_skips_set_model(self):
        client = self._client()
        save_path = os.path.join(self.out_dir, "clause1.wav")
        ok, msg = client.synthesize_clause("大家好，我是曼波。", save_path, self._default_preset())
        self.assertTrue(ok, msg)
        self.assertEqual(self.server.state.set_model_calls, [])
        self.assertTrue(os.path.exists(save_path))
        with open(save_path, "rb") as f:
            self.assertTrue(looks_like_wav(f.read(8192)))
        # 请求体使用旧版 api.py 字段
        body = self.server.state.tts_calls[0]
        self.assertEqual(body["text"], "大家好，我是曼波。")
        self.assertEqual(body["text_language"], "zh")
        self.assertEqual(body["refer_wav_path"],
                         os.path.abspath(os.path.join(self.tmp_project, "models", "refer.wav")))

    # ---- 自定义模型热切换（仅一次） ----

    def test_custom_model_switches_once_with_absolute_paths(self):
        gpt = os.path.join(self.tmp_project, "voices", "custom", "custom.ckpt")
        sovits = os.path.join(self.tmp_project, "voices", "custom", "custom.pth")
        preset = VoicePreset(
            id="custom:hot", character="custom", emotion="hot",
            ref_wav=os.path.join(self.tmp_project, "models", "refer.wav"),
            ref_text="大家好", ref_lang="zh", gpt_model=gpt, sovits_model=sovits,
        )
        client = self._client()
        ok1, msg1 = client.synthesize_clause("第一句", os.path.join(self.out_dir, "a.wav"), preset)
        ok2, msg2 = client.synthesize_clause("第二句", os.path.join(self.out_dir, "b.wav"), preset)
        self.assertTrue(ok1, msg1)
        self.assertTrue(ok2, msg2)
        self.assertEqual(len(self.server.state.set_model_calls), 1)
        sent = self.server.state.set_model_calls[0]
        self.assertEqual(sent["gpt_model_path"], os.path.abspath(gpt))
        self.assertEqual(sent["sovits_model_path"], os.path.abspath(sovits))

    def test_switch_model_back_to_default_switches_again(self):
        gpt = os.path.join(self.tmp_project, "x.ckpt")
        sovits = os.path.join(self.tmp_project, "x.pth")
        client = self._client()
        custom = VoicePreset(id="c:d", character="c", emotion="d",
                             ref_wav="", ref_text="hi", ref_lang="zh",
                             gpt_model=gpt, sovits_model=sovits)
        default = self._default_preset()
        client.synthesize_clause("一", os.path.join(self.out_dir, "1.wav"), custom)
        client.synthesize_clause("二", os.path.join(self.out_dir, "2.wav"), default)
        self.assertEqual(len(self.server.state.set_model_calls), 2)
        self.assertEqual(self.server.state.set_model_calls[1]["gpt_model_path"],
                         os.path.abspath(os.path.join(self.tmp_project, "models", "manbo-e10.ckpt")))

    # ---- 中途取消：.part 清理 ----

    def test_mid_stream_cancel_cleans_part(self):
        st = self.server.state
        st.tts_mode = "slow_wav"
        st.serve_delay = 0.03  # ~256KB / 4KB 块 ≈ 1.9s，0.2s 处取消必然在流中
        client = self._client()
        cancel = threading.Event()
        timer = threading.Timer(0.2, cancel.set)
        timer.start()
        self.addCleanup(timer.cancel)
        save_path = os.path.join(self.out_dir, "clause_cancel.wav")
        ok, msg = client.synthesize_clause("这一句会被取消", save_path,
                                           self._default_preset(), cancel_event=cancel)
        self.assertFalse(ok)
        self.assertEqual(glob.glob(os.path.join(self.out_dir, "*.part")), [])
        self.assertFalse(os.path.exists(save_path))
        self.assertEqual(len(st.tts_calls), 1)  # 确实进入了流式合成

    def test_cancel_before_request_start(self):
        client = self._client()
        cancel = threading.Event()
        cancel.set()
        save_path = os.path.join(self.out_dir, "clause_precancel.wav")
        ok, msg = client.synthesize_clause("还没开始就取消", save_path,
                                           self._default_preset(), cancel_event=cancel)
        self.assertFalse(ok)
        self.assertEqual(glob.glob(os.path.join(self.out_dir, "*.part")), [])
        self.assertEqual(len(self.server.state.tts_calls), 0)

    # ---- 坏响应防护 ----

    def test_non_wav_200_rejected_and_cleaned(self):
        self.server.state.tts_mode = "notwav"
        client = self._client()
        save_path = os.path.join(self.out_dir, "bad.wav")
        ok, msg = client.synthesize_clause("你好", save_path, self._default_preset())
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(save_path))
        self.assertEqual(glob.glob(os.path.join(self.out_dir, "*.part")), [])

    def test_error_status_reported(self):
        self.server.state.tts_mode = "error500"
        client = self._client()
        save_path = os.path.join(self.out_dir, "err.wav")
        ok, msg = client.synthesize_clause("你好", save_path, self._default_preset())
        self.assertFalse(ok)
        self.assertIn("500", msg)

    def test_unsupported_lang_rejected(self):
        client = self._client()
        save_path = os.path.join(self.out_dir, "lang.wav")
        ok, msg = client.synthesize_clause("hola", save_path, self._default_preset(),
                                           text_lang="fr")
        self.assertFalse(ok)
        self.assertIn("不支持的文本语种", msg)

    # ---- legacy 包装 ----

    def test_generate_speech_legacy_wrapper(self):
        client = self._client()
        save_path = os.path.join(self.out_dir, "legacy.wav")
        ok, msg = client.generate_speech("测试一下旧接口", save_path)
        self.assertTrue(ok, msg)
        self.assertEqual(os.path.abspath(msg), os.path.abspath(save_path))
        body = self.server.state.tts_calls[0]
        self.assertEqual(body["text"], "测试一下旧接口")
        self.assertEqual(body["prompt_text"], DEFAULT_REF_TEXT)
        self.assertIn("refer_wav_path", body)

    def test_tts_client_alias(self):
        self.assertIs(TTSClient, GSVHttpClient)


# ----------------------------------------------------------------------
# download_file_resumable（多镜像 Range 续传）
# ----------------------------------------------------------------------

class TestDownloadResumable(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.data = b"7z\xbc\xaf\x27\x1c" + (bytes(range(256)) * 1024)[6:]  # 262144 字节
        # Mirror-2：支持 Range/206 的好镜像
        cls.good_state = _ServerState(tts_wav=b"")
        cls.good_state.file_bytes = cls.data
        cls.good = _MockServer(cls.good_state)
        # Mirror-1：探测阶段即失败（HTTP 404，不支持 Range）
        cls.fail404_state = _ServerState(tts_wav=b"")
        cls.fail404_state.range_ok = False
        cls.fail404_state.fail_status = 404
        cls.fail404 = _MockServer(cls.fail404_state)
        # Mirror-1b：返回 200（忽略 Range 头）
        cls.ignore_state = _ServerState(tts_wav=b"")
        cls.ignore_state.range_ok = False
        cls.ignore_state.fail_status = 200
        cls.ignore = _MockServer(cls.ignore_state)

    @classmethod
    def tearDownClass(cls):
        for srv in (cls.good, cls.fail404, cls.ignore):
            srv.close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mambotts_dl_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dest = os.path.join(self.tmp, "bundle.7z")
        self.part = self.dest + ".part"
        self.log = _CollectLog()

    def _read_part(self):
        with open(self.part, "rb") as f:
            return f.read()

    def test_mirror1_probe_failure_does_not_truncate_part_then_mirror2_resumes(self):
        # 预置 100000 字节断点
        with open(self.part, "wb") as f:
            f.write(self.data[:100000])
        # Pass 1：Mirror-1 探测失败 → 立即 False，.part 必须原封不动
        ok = download_file_resumable(self.fail404.base_url + "/download", self.dest,
                                     len(self.data), _noop_progress, self.log,
                                     lambda: False, allow_reset_part=False)
        self.assertFalse(ok)
        self.assertTrue(os.path.exists(self.part))
        self.assertEqual(os.path.getsize(self.part), 100000)
        self.assertEqual(self._read_part(), self.data[:100000])
        # Mirror-2 支持续传 → 从 100000 偏移继续，最终文件完整
        ok = download_file_resumable(self.good.base_url + "/download", self.dest,
                                     len(self.data), _noop_progress, self.log,
                                     lambda: False, allow_reset_part=False)
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(self.part))
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), self.data)

    def test_mirror_ignoring_range_keeps_part_in_pass1(self):
        with open(self.part, "wb") as f:
            f.write(self.data[:50000])
        ok = download_file_resumable(self.ignore.base_url + "/download", self.dest,
                                     len(self.data), _noop_progress, self.log,
                                     lambda: False, allow_reset_part=False)
        self.assertFalse(ok)
        self.assertEqual(os.path.getsize(self.part), 50000)
        self.assertEqual(self._read_part(), self.data[:50000])

    def test_pass2_allows_reset_when_all_mirrors_refuse_offset(self):
        with open(self.part, "wb") as f:
            f.write(self.data[:50000])
        # Pass 2（allow_reset_part=True）：坏镜像获准清断点后，好镜像从头下载
        ok1 = download_file_resumable(self.fail404.base_url + "/download", self.dest,
                                      len(self.data), _noop_progress, self.log,
                                      lambda: False, allow_reset_part=True)
        self.assertFalse(ok1)  # 该镜像自身仍然失败
        ok2 = download_file_resumable(self.good.base_url + "/download", self.dest,
                                      len(self.data), _noop_progress, self.log,
                                      lambda: False, allow_reset_part=True)
        self.assertTrue(ok2)
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), self.data)

    def test_oversize_part_is_reset(self):
        # 断点超过期望大小（源换包/写入错乱）→ 无条件重置后重新下载完整文件
        with open(self.part, "wb") as f:
            f.write(b"\xde\xad" * (len(self.data) // 2 + 4096))
        ok = download_file_resumable(self.good.base_url + "/download", self.dest,
                                     len(self.data), _noop_progress, self.log,
                                     lambda: False, allow_reset_part=False)
        self.assertTrue(ok)
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), self.data)

    def test_cancel_before_first_chunk_leaves_no_part(self):
        ok = download_file_resumable(self.good.base_url + "/download", self.dest,
                                     len(self.data), _noop_progress, self.log,
                                     lambda: True, allow_reset_part=False)
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(self.part))
        self.assertFalse(os.path.exists(self.dest))

    def test_complete_part_promotes_without_http(self):
        with open(self.part, "wb") as f:
            f.write(self.data)
        ok = download_file_resumable(self.good.base_url + "/download", self.dest,
                                     len(self.data), _noop_progress, self.log,
                                     lambda: False, allow_reset_part=False)
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(self.part))
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), self.data)

    def test_mid_download_cancel_keeps_part_for_resume(self):
        # 3MB 数据 + 第 2 次 cancel 检查即取消：至少已落 1MB，断点保留
        data = b"7z\xbc\xaf\x27\x1c" + (bytes(range(256)) * 12288)[6:]  # 3145728 字节
        self.good_state.file_bytes = data
        self.addCleanup(setattr, self.good_state, "file_bytes", self.data)
        calls = {"n": 0}

        def cancel():
            calls["n"] += 1
            return calls["n"] >= 2

        ok = download_file_resumable(self.good.base_url + "/download", self.dest,
                                     len(data), _noop_progress, self.log, cancel,
                                     allow_reset_part=False)
        self.assertFalse(ok)
        self.assertTrue(os.path.exists(self.part))
        part_size = os.path.getsize(self.part)
        self.assertGreater(part_size, 0)
        self.assertLess(part_size, len(data))
        self.assertEqual(self._read_part(), data[:part_size])
        # 不带断点限制地续传 → 完整落地
        ok = download_file_resumable(self.good.base_url + "/download", self.dest,
                                     len(data), _noop_progress, self.log,
                                     lambda: False, allow_reset_part=False)
        self.assertTrue(ok)
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), data)


# ----------------------------------------------------------------------
# 镜像配置与安装包常量
# ----------------------------------------------------------------------

class TestMirrorConfig(unittest.TestCase):

    def test_load_mirror_urls_parses_and_substitutes(self):
        tmp = tempfile.mkdtemp(prefix="mambotts_mirror_")
        try:
            with open(os.path.join(tmp, "mirrors.txt"), "w", encoding="utf-8") as f:
                f.write("# 注释行\n\n"
                        "https://mirror.example.com/pkg/{file}\n"
                        "  https://cdn.example.net/d/{file}  \n")
            urls = load_mirror_urls("GPT-SoVITS-v5-20261005.7z", tmp)
            self.assertEqual(urls, [
                "https://mirror.example.com/pkg/GPT-SoVITS-v5-20261005.7z",
                "https://cdn.example.net/d/GPT-SoVITS-v5-20261005.7z",
            ])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_load_mirror_urls_missing_file_returns_empty(self):
        tmp = tempfile.mkdtemp(prefix="mambotts_mirror_")
        try:
            self.assertEqual(load_mirror_urls("x.7z", tmp), [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_build_source_urls_user_mirror_first_then_builtin(self):
        tmp = tempfile.mkdtemp(prefix="mambotts_mirror_")
        try:
            builtin_first = BUILTIN_SOURCES[0].replace("{file}", "pkg.7z")
            with open(os.path.join(tmp, "mirrors.txt"), "w", encoding="utf-8") as f:
                f.write(builtin_first + "\n"
                        + BUILTIN_SOURCES[1].replace("{file}", "pkg.7z") + "\n")
            urls = build_source_urls({"file": "pkg.7z"}, tmp)
            self.assertEqual(urls[0], builtin_first)          # 用户镜像优先
            self.assertEqual(len(urls), len(BUILTIN_SOURCES)) # 与内置重复的行被去重
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestGPUPackages(unittest.TestCase):

    def test_packages_have_size_and_sha256(self):
        for key, pkg in GPU_PACKAGES.items():
            self.assertTrue(pkg["file"].endswith(".7z"), key)
            self.assertGreater(pkg["size_bytes"], 1024 ** 3, key)
            self.assertRegex(pkg["sha256"], r"^[0-9a-f]{64}$", key)


# ----------------------------------------------------------------------
# is_local_api_url / port_in_use
# ----------------------------------------------------------------------

class TestIsLocalApiUrl(unittest.TestCase):

    def test_loopback_urls_accepted(self):
        self.assertTrue(is_local_api_url("http://127.0.0.1:9880"))
        self.assertTrue(is_local_api_url("http://127.0.0.1"))
        self.assertTrue(is_local_api_url("http://127.0.0.2:9880/"))
        self.assertTrue(is_local_api_url("http://localhost:9880"))
        self.assertTrue(is_local_api_url("  http://localhost:9880  "))

    def test_remote_urls_rejected(self):
        self.assertFalse(is_local_api_url("http://192.168.1.5:9880"))
        self.assertFalse(is_local_api_url("http://example.com:9880"))
        self.assertFalse(is_local_api_url("https://api.example.com"))

    def test_malformed_urls_rejected_valueerror_safe(self):
        self.assertFalse(is_local_api_url(""))
        self.assertFalse(is_local_api_url(None))
        self.assertFalse(is_local_api_url("not a url"))
        self.assertFalse(is_local_api_url("http://127.0.0.1:abc"))    # 畸形端口
        self.assertFalse(is_local_api_url("http://127.0.0.1:99999"))  # 端口越界


class TestPortInUse(unittest.TestCase):

    def test_listening_port_detected(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        try:
            self.assertTrue(port_in_use("127.0.0.1", port))
        finally:
            s.close()

    def test_closed_port_free(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertFalse(port_in_use("127.0.0.1", port))


# ----------------------------------------------------------------------
# EngineSupervisor busy/online 状态解耦（心跳互锁）
# ----------------------------------------------------------------------

class TestEngineSupervisorStateDecoupling(unittest.TestCase):

    def test_busy_toggle_never_touches_online(self):
        sup = EngineSupervisor()
        sup.mark_online(True)
        sup.set_synthesis_busy(True)
        self.assertTrue(sup._online)              # online 未被 busy 触碰
        self.assertFalse(sup.should_poll_health()) # 忙 → 挂起健康轮询
        sup.set_synthesis_busy(False)
        self.assertTrue(sup._online)              # online 依旧未被触碰
        self.assertTrue(sup.should_poll_health())

    def test_mark_online_works_while_busy(self):
        sup = EngineSupervisor()
        sup.set_synthesis_busy(True)
        sup.mark_online(False)
        self.assertFalse(sup._online)
        self.assertFalse(sup.should_poll_health())
        sup.mark_online(True)
        self.assertTrue(sup._online)
        self.assertFalse(sup.should_poll_health())  # busy 仍然抑制轮询

    def test_defaults(self):
        sup = EngineSupervisor()
        self.assertFalse(sup._synthesis_busy)
        self.assertFalse(sup._online)
        self.assertTrue(sup.should_poll_health())
        self.assertFalse(sup.is_running())


class TestV5InstallerAndInfraFixes(unittest.TestCase):
    def test_v5_packages_and_no_baidu_sources(self):
        from mambotts.infra.installer import (
            DISK_SPACE_FACTOR,
            GPU_PACKAGES,
            describe_sources,
            estimate_disk_requirement,
        )

        self.assertIn("nvidia50", GPU_PACKAGES)
        self.assertIn("general", GPU_PACKAGES)
        for key, pkg in GPU_PACKAGES.items():
            self.assertIn("20261005", pkg["file"], key)
            self.assertGreater(pkg["size_bytes"], 9 * 1024 ** 3, key)
            self.assertEqual(len(pkg["sha256"]), 64, key)
            with tempfile.TemporaryDirectory(prefix="mambotts_src_") as tmp:
                with open(os.path.join(tmp, "mirrors.txt"), "w", encoding="utf-8") as fh:
                    fh.write("https://custom.example.org/pkgs/{file}\n")
                sources = describe_sources(pkg, base_dir=tmp)
                self.assertGreaterEqual(len(sources), 5)
                self.assertEqual(sources[0]["channel"], "mirrors.txt")
                for src in sources:
                    self.assertNotIn("baidu", src["url"].lower())
                req_empty, matched_empty = estimate_disk_requirement(pkg, base_dir=tmp)
                self.assertEqual(matched_empty, 0)
                self.assertGreaterEqual(
                    req_empty, int(pkg["size_bytes"] * DISK_SPACE_FACTOR)
                )

    def test_probe_source_metrics_rejects_html_and_measures_binary(self):
        from unittest import mock
        from mambotts.infra.installer import probe_source_metrics

        html_resp = mock.MagicMock()
        html_resp.status_code = 200
        html_resp.headers = {"content-type": "text/html; charset=utf-8"}
        html_resp.__enter__ = mock.Mock(return_value=html_resp)
        html_resp.__exit__ = mock.Mock(return_value=False)
        with mock.patch("mambotts.infra.installer.requests.Session.get", return_value=html_resp):
            spd, lat = probe_source_metrics("https://mirror.example.org/pkg.7z")
            self.assertIsNone(spd)
            self.assertIsNone(lat)

        bin_resp = mock.MagicMock()
        bin_resp.status_code = 206
        bin_resp.headers = {"content-type": "application/octet-stream"}
        bin_resp.iter_content.return_value = [b"7z\xbc\xaf\x27\x1c" + b"\x00" * 4090]
        bin_resp.__enter__ = mock.Mock(return_value=bin_resp)
        bin_resp.__exit__ = mock.Mock(return_value=False)
        with mock.patch("mambotts.infra.installer.requests.Session.get", return_value=bin_resp):
            spd, lat = probe_source_metrics("https://mirror.example.org/pkg.7z")
            self.assertIsNotNone(spd)
            self.assertGreater(spd, 0.0)
            self.assertIsNotNone(lat)
            self.assertGreaterEqual(lat, 0)

        cancelled_spd, cancelled_lat = probe_source_metrics(
            "https://mirror.example.org/pkg.7z", cancel_cb=lambda: True
        )
        self.assertIsNone(cancelled_spd)
        self.assertIsNone(cancelled_lat)

    def test_pass2_part_preserved_on_network_error_or_503(self):
        from unittest import mock
        import requests as _req

        with tempfile.TemporaryDirectory(prefix="mambotts_dl_part_") as tmp:
            dest = os.path.join(tmp, "pkg.7z")
            part = dest + ".part"
            with open(part, "wb") as fh:
                fh.write(b"partial-data-bytes")

            probe_ok = mock.Mock(status_code=206, headers={"Content-Range": "bytes 0-0/100"})
            with mock.patch(
                "mambotts.infra.installer.requests.Session.get",
                side_effect=[probe_ok, _req.ConnectionError("dropped")],
            ):
                ok = download_file_resumable(
                    "https://mirror.example.org/pkg.7z",
                    dest,
                    100,
                    lambda *_a: None,
                    lambda *_a: None,
                    lambda: False,
                )
            self.assertFalse(ok)
            self.assertTrue(os.path.isfile(part), ".part must be kept after Pass 2 network error")

            resp_503 = mock.MagicMock()
            resp_503.status_code = 503
            resp_503.headers = {}
            resp_503.__enter__ = mock.Mock(return_value=resp_503)
            resp_503.__exit__ = mock.Mock(return_value=False)
            with mock.patch(
                "mambotts.infra.installer.requests.Session.get",
                side_effect=[probe_ok, resp_503],
            ):
                ok2 = download_file_resumable(
                    "https://mirror.example.org/pkg.7z",
                    dest,
                    100,
                    lambda *_a: None,
                    lambda *_a: None,
                    lambda: False,
                )
            self.assertFalse(ok2)
            self.assertTrue(os.path.isfile(part), ".part must be kept after HTTP 503")

    def test_download_promotes_exact_complete_part(self):
        with tempfile.TemporaryDirectory(prefix="mambotts_dl_done_") as tmp:
            dest = os.path.join(tmp, "pkg.7z")
            part = dest + ".part"
            payload = b"7z\xbc\xaf\x27\x1c" + b"X" * 94
            with open(part, "wb") as fh:
                fh.write(payload)
            ok = download_file_resumable(
                "https://mirror.example.org/pkg.7z",
                dest,
                len(payload),
                lambda *_a: None,
                lambda *_a: None,
                lambda: False,
            )
            self.assertTrue(ok)
            self.assertTrue(os.path.isfile(dest))
            self.assertFalse(os.path.exists(part))
            with open(dest, "rb") as fh:
                self.assertEqual(fh.read(), payload)

    def test_bug_a_preset_none_and_bug_b_bat_escaping(self):
        from unittest import mock
        from mambotts.infra.engine_mgr import (
            _engine_default_ref_text,
            _TQDM_RE,
            EngineSupervisor,
            validate_http_api_url,
        )
        from mambotts.infra.installer import _sanitize_bat_arg
        from mambotts.infra.gsv_client import GSVHttpClient

        with tempfile.TemporaryDirectory(prefix="mambotts_bugab_") as tmp:
            models = os.path.join(tmp, "models")
            os.makedirs(models, exist_ok=True)
            ref_wav = os.path.join(models, "refer.wav")
            with open(ref_wav, "wb") as fh:
                fh.write(_make_wav())
            with open(os.path.join(models, "refer.txt"), "w", encoding="utf-8") as fh:
                fh.write("自定义参考台词。")

            self.assertEqual(_engine_default_ref_text(ref_wav), "自定义参考台词。")
            sanitized = _sanitize_bat_arg('a%b"c\r\nd&|')
            self.assertIn("%%", sanitized)
            self.assertNotIn("%", sanitized.replace("%%", ""))
            self.assertNotIn('"', sanitized)
            self.assertNotIn("\r", sanitized)
            self.assertNotIn("\n", sanitized)

            client = GSVHttpClient(project_dir=tmp)
            wav_bytes = _make_wav(frames=3200)
            fake_resp = mock.MagicMock()
            fake_resp.status_code = 200
            fake_resp.content = wav_bytes
            fake_resp.headers = {"content-type": "audio/wav"}
            fake_resp.iter_content.return_value = [wav_bytes]
            fake_resp.__enter__ = mock.Mock(return_value=fake_resp)
            fake_resp.__exit__ = mock.Mock(return_value=False)
            out_wav = os.path.join(tmp, "out.wav")
            with mock.patch.object(client._session, "post", return_value=fake_resp) as post_mock:
                ok, err = client.synthesize_clause(
                    "测试台词", out_wav, preset=None, check_online=False
                )
            self.assertTrue(ok, err)
            sent_payload = post_mock.call_args.kwargs.get("json") or post_mock.call_args[1]["json"]
            self.assertEqual(sent_payload["prompt_text"], "自定义参考台词。")
            self.assertEqual(
                os.path.normcase(os.path.abspath(sent_payload["refer_wav_path"])),
                os.path.normcase(os.path.abspath(ref_wav)),
            )

        self.assertEqual(validate_http_api_url("http://127.0.0.1:9880"), "http://127.0.0.1:9880")
        self.assertIsNone(validate_http_api_url(""))
        self.assertIsNone(validate_http_api_url("ftp://127.0.0.1:9880"))
        self.assertIsNone(validate_http_api_url("http://"))

        sup = EngineSupervisor()
        sup.is_running = lambda: True
        sup._bound_api_url = "http://127.0.0.1:9880"
        self.assertTrue(sup.is_running_for("http://127.0.0.1:9880"))
        self.assertFalse(sup.is_running_for("http://127.0.0.1:9999"))

        self.assertIsNotNone(_TQDM_RE.match("  45%|████▌     | 45/100 [00:02<00:02, 20.1it/s]"))
        self.assertIsNone(_TQDM_RE.match("RuntimeError: CUDA out of memory"))

    def test_detached_engine_start_logs_to_file_not_pipe(self):
        import subprocess
        from unittest import mock
        from mambotts.infra.engine_mgr import EngineSupervisor

        with tempfile.TemporaryDirectory(prefix="mambo_detached_log_") as tmp:
            sup = EngineSupervisor(base_dir=tmp)
            fake_proc = mock.Mock(pid=4242, stdout=None)
            fake_proc.poll.return_value = None
            captured_stdout = {}

            def fake_popen(*_args, **kwargs):
                stdout_f = kwargs.get("stdout")
                captured_stdout["stdout"] = stdout_f
                captured_stdout["name"] = getattr(stdout_f, "name", "")
                captured_stdout["closed_in_popen"] = getattr(stdout_f, "closed", None)
                return fake_proc

            with (
                mock.patch("mambotts.infra.engine_mgr.preflight_check", return_value=[]),
                mock.patch("mambotts.infra.engine_mgr.port_in_use", return_value=False),
                mock.patch("mambotts.infra.engine_mgr.subprocess.Popen", side_effect=fake_popen) as popen_mock,
            ):
                ok, _msg = sup.start(api_url="http://127.0.0.1:9880", bind_job_object=False)
            self.assertTrue(ok)
            kwargs = popen_mock.call_args.kwargs
            self.assertNotEqual(kwargs.get("stdout"), subprocess.PIPE)
            self.assertFalse(captured_stdout["closed_in_popen"])
            self.assertTrue(captured_stdout["stdout"].closed)
            self.assertEqual(
                os.path.abspath(captured_stdout["name"]),
                os.path.abspath(os.path.join(tmp, "engine.log")),
            )
            self.assertEqual(kwargs.get("stderr"), subprocess.STDOUT)
            self.assertEqual(kwargs.get("stdin"), subprocess.DEVNULL)


class TestInfraAuditRegressions(unittest.TestCase):
    """Regressions from the Goldfish audit across installer, engine_mgr, gsv_client."""

    def test_download_rejects_html_and_corrupt_full_part_and_bad_206_range(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from mambotts.infra.installer import probe_source_metrics

        mode = {"kind": "html"}
        payload = b"7z\xbc\xaf\x27\x1c" + b"X" * 4090

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_GET(self):
                if mode["kind"] == "html":
                    body = b"<html><body>forbidden mirror page</body></html>" * 120
                    ctype = "text/html; charset=utf-8"
                    status = 200
                    hdrs = {}
                elif mode["kind"] == "json":
                    body = b'{"error":"rate_limit"}'
                    ctype = "application/json"
                    status = 200
                    hdrs = {}
                elif mode["kind"] == "bad206":
                    body = payload[100:]
                    ctype = "application/octet-stream"
                    status = 206
                    hdrs = {"Content-Range": f"bytes 999-{len(payload) - 1}/{len(payload)}"}
                else:
                    body = payload
                    ctype = "application/octet-stream"
                    status = 200
                    hdrs = {}
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in hdrs.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

        srv = HTTPServer(("127.0.0.1", 0), Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            url = f"http://127.0.0.1:{srv.server_port}/pkg.7z"
            # probe rejects HTML/JSON & tiny 200 payloads
            bps_html, _lat_html = probe_source_metrics(url, expected_bytes=len(payload))
            self.assertIsNone(bps_html)

            mode["kind"] = "json"
            bps_json, _lat_json = probe_source_metrics(url, expected_bytes=len(payload))
            self.assertIsNone(bps_json)

            with tempfile.TemporaryDirectory(prefix="mambo_dl_audit_") as tmp:
                dest = os.path.join(tmp, "pkg.7z")
                part = dest + ".part"
                log = _CollectLog()

                # HTML payload at offset 0 must fail AND remove .part
                mode["kind"] = "html"
                ok = download_file_resumable(
                    url, dest, len(payload), _noop_progress, log, lambda: False
                )
                self.assertFalse(ok)
                self.assertFalse(os.path.exists(part))
                self.assertFalse(os.path.exists(dest))

                # Full-size corrupt .part lacking 7z magic must NOT promote (pass 1 refuses; pass 2 resets & recovers)
                with open(part, "wb") as fh:
                    fh.write(b"<!html" + b"0" * (len(payload) - 6))
                mode["kind"] = "good"
                self.assertFalse(
                    download_file_resumable(
                        url, dest, len(payload), _noop_progress, log, lambda: False,
                        allow_reset_part=False,
                    )
                )
                self.assertFalse(os.path.exists(dest))
                ok = download_file_resumable(
                    url, dest, len(payload), _noop_progress, log, lambda: False,
                    allow_reset_part=True,
                )
                self.assertTrue(ok)
                with open(dest, "rb") as fh:
                    self.assertEqual(fh.read(), payload)

                # Bad Content-Range start offset on 206 resume must reject & keep intact partial
                os.remove(dest)
                with open(part, "wb") as fh:
                    fh.write(payload[:100])
                mode["kind"] = "bad206"
                ok = download_file_resumable(
                    url, dest, len(payload), _noop_progress, log, lambda: False,
                    allow_reset_part=False,
                )
                self.assertFalse(ok)
                self.assertEqual(os.path.getsize(part), 100)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_url_loopback_validation_and_host_port(self):
        from mambotts.infra.engine_mgr import (
            _host_port_from_url,
            is_local_api_url,
            validate_http_api_url,
        )

        self.assertFalse(is_local_api_url("http://127.evil.com:9880"))
        self.assertTrue(is_local_api_url("http://127.0.0.1:9880"))
        self.assertTrue(is_local_api_url("http://localhost:9880"))
        self.assertTrue(is_local_api_url("http://[::1]:9880"))
        self.assertIsNone(validate_http_api_url("http://user:pass@127.0.0.1:9880"))
        self.assertIsNone(validate_http_api_url("http://127.0.0.1:9880/api?q=1"))
        self.assertIsNone(validate_http_api_url("http://127.0.0.1:9880/#frag"))
        self.assertEqual(validate_http_api_url("http://127.0.0.1/"), "http://127.0.0.1")
        self.assertEqual(_host_port_from_url("http://127.0.0.1"), ("127.0.0.1", 9880))
        self.assertEqual(_host_port_from_url("http://example.com"), ("example.com", 80))
        self.assertEqual(_host_port_from_url("https://example.com"), ("example.com", 443))

    def test_installer_bom_cache_disk_sanitize_and_cancel(self):
        from unittest import mock
        from mambotts.domain.models import DEFAULT_REF_TEXT
        from mambotts.infra.installer import (
            DISK_SPACE_FACTOR,
            _sanitize_bat_arg,
            classify_local_cache,
            compute_sha256,
            download_7z_tool,
            estimate_disk_requirement,
        )

        with tempfile.TemporaryDirectory(prefix="mambo_inst_misc_") as tmp:
            mirror_file = os.path.join(tmp, "mirrors.txt")
            with open(mirror_file, "wb") as fh:
                fh.write(
                    "\ufeff# comment\nhttps://good.example.com/dl\nftp://bad.example.com/dl\nfile:///c:/x\n".encode("utf-8")
                )
            urls = load_mirror_urls("mirrors.txt", tmp)
            self.assertEqual(urls, ["https://good.example.com/dl"])

            pkg = GPU_PACKAGES["general"]
            temp_dir = os.path.join(tmp, "engine_temp")
            os.makedirs(temp_dir, exist_ok=True)
            pkg_file = os.path.join(temp_dir, pkg["file"])
            part_file = pkg_file + ".part"
            with open(part_file, "wb") as fh:
                fh.write(b"7z\xbc\xaf\x27\x1c" + b"0" * 1024)
            self.assertEqual(classify_local_cache(pkg, tmp)[0], "partial")

            # Do not double-count if both target file and stale .part exist.
            with open(pkg_file, "wb") as fh:
                fh.write(b"7z\xbc\xaf\x27\x1c" + b"0" * 2048)
            self.assertEqual(classify_local_cache(pkg, tmp), ("unverified", 2054))
            req, matched = estimate_disk_requirement(pkg, tmp)
            gross = int(pkg["size_bytes"] * DISK_SPACE_FACTOR)
            self.assertEqual(matched, 2054)
            self.assertEqual(req, max(0, gross - 2054))

            # Trailing backslash + % escape + all-stripped fallback in bat sanitizer.
            self.assertEqual(_sanitize_bat_arg("你好世界\\"), "你好世界")
            self.assertEqual(_sanitize_bat_arg("100%!"), "100%%!")
            self.assertEqual(_sanitize_bat_arg("\x00\"&|^<>\r\n\\"), DEFAULT_REF_TEXT)

            # Cancellation in compute_sha256 & download_7z_tool.
            from mambotts.infra.installer import _InstallCancelled

            with open(pkg_file, "wb") as fh:
                fh.write(b"abc" * 200000)
            with self.assertRaises(_InstallCancelled):
                compute_sha256(pkg_file, cancel_cb=lambda: True)

            tools_dir = os.path.join(temp_dir, "tools")
            with mock.patch("mambotts.infra.installer.requests.Session") as sess_cls:
                sess = sess_cls.return_value
                sess.__enter__.return_value = sess
                resp = mock.MagicMock()
                resp.__enter__.return_value = resp
                resp.raise_for_status.return_value = None
                resp.iter_content.return_value = [b"data" * 1024]
                sess.get.return_value = resp
                calls = {"n": 0}

                def cancel_after_get():
                    calls["n"] += 1
                    return calls["n"] >= 3

                with self.assertRaises(_InstallCancelled):
                    download_7z_tool(tools_dir, lambda _m: None, cancel_cb=cancel_after_get)
            sess.__exit__.assert_called()

    def test_install_engine_staging_flatten_and_rollback(self):
        import hashlib
        from unittest import mock
        from mambotts.infra.installer import install_engine

        fake_bytes = b"7z\xbc\xaf\x27\x1c" + b"0" * 64
        fake_sha = hashlib.sha256(fake_bytes).hexdigest()
        fake_pkg = {
            "file": "fake_pkg.7z",
            "label": "Mock GPU",
            "size_bytes": len(fake_bytes),
            "sha256": fake_sha,
        }

        with tempfile.TemporaryDirectory(prefix="mambo_staging_") as tmp:
            temp_dir = os.path.join(tmp, "engine_temp")
            os.makedirs(temp_dir, exist_ok=True)
            pkg_path = os.path.join(temp_dir, fake_pkg["file"])
            with open(pkg_path, "wb") as fh:
                fh.write(fake_bytes)

            # Seed an existing GPT-SoVITS dir and ensure failed extraction leaves it intact.
            gsv_dir = os.path.join(tmp, "GPT-SoVITS")
            os.makedirs(os.path.join(gsv_dir, "runtime"), exist_ok=True)
            with open(os.path.join(gsv_dir, "api.py"), "w", encoding="utf-8") as fh:
                fh.write("# existing\n")
            with open(os.path.join(gsv_dir, "runtime", "python.exe"), "wb") as fh:
                fh.write(b"MZ")

            r_fail, w_fail = os.pipe()
            os.close(w_fail)
            fake_proc = mock.Mock()
            fake_proc.poll.return_value = 1  # non-zero exit -> extraction failure
            fake_proc.stdout = os.fdopen(r_fail, "rb")
            fake_proc.wait.return_value = 1

            with (
                mock.patch.dict(GPU_PACKAGES, {"general": fake_pkg}),
                mock.patch("mambotts.infra.installer.detect_gpu_type", return_value="general"),
                mock.patch("mambotts.infra.installer.download_7z_tool", return_value=os.path.join(tmp, "7zr.exe")),
                mock.patch("mambotts.infra.installer.subprocess.Popen", return_value=fake_proc),
            ):
                ok, _msg = install_engine(
                    gpu_type="general",
                    base_dir=tmp,
                    log_cb=lambda _m: None,
                    progress_cb=lambda _p: None,
                    state_cb=lambda _s: None,
                    preferred_url=None,
                    cancel_cb=lambda: False,
                )
            self.assertFalse(ok)
            self.assertTrue(os.path.isfile(os.path.join(gsv_dir, "api.py")))

            # Now test successful extraction producing nested GPT-SoVITS/GPT-SoVITS/api.py!
            def fake_popen(cmd, **_kwargs):
                out_flag = next(c for c in cmd if c.startswith("-o"))
                staging_root = out_flag[2:]
                nested = os.path.join(staging_root, "GPT-SoVITS")
                os.makedirs(os.path.join(nested, "runtime"), exist_ok=True)
                with open(os.path.join(nested, "api.py"), "w", encoding="utf-8") as fh:
                    fh.write("# new\n")
                with open(os.path.join(nested, "runtime", "python.exe"), "wb") as fh:
                    fh.write(b"MZ")
                r_ok, w_ok = os.pipe()
                os.write(w_ok, b"  50% \n  100% \n")
                os.close(w_ok)
                p = mock.Mock()
                p.stdout = os.fdopen(r_ok, "rb")
                p.poll.side_effect = [None, 0, 0]
                p.wait.return_value = 0
                return p

            with (
                mock.patch.dict(GPU_PACKAGES, {"general": fake_pkg}),
                mock.patch("mambotts.infra.installer.detect_gpu_type", return_value="general"),
                mock.patch("mambotts.infra.installer.download_7z_tool", return_value=os.path.join(tmp, "7zr.exe")),
                mock.patch("mambotts.infra.installer.subprocess.Popen", side_effect=fake_popen),
            ):
                ok_new, msg_new = install_engine(
                    gpu_type="general",
                    base_dir=tmp,
                    log_cb=lambda _m: None,
                    progress_cb=lambda _p: None,
                    state_cb=lambda _s: None,
                    preferred_url=None,
                    cancel_cb=lambda: False,
                )
            self.assertTrue(ok_new, msg_new)
            self.assertTrue(os.path.isfile(os.path.join(gsv_dir, "api.py")))
            self.assertTrue(os.path.isfile(os.path.join(gsv_dir, ".mambo_install_ok")))
            self.assertFalse(os.path.exists(os.path.join(gsv_dir, "GPT-SoVITS")))

    def test_gsv_client_model_cache_controls(self):
        client = TTSClient(api_url="http://127.0.0.1:9880")
        client.invalidate_active_models()
        self.assertIsNone(client._active_models)
        client.reset_active_models()
        self.assertEqual(client._active_models, (client._default_gpt, client._default_sovits))

    def test_port_in_use_ipv6_and_unresolvable_host_safe(self):
        import socket
        from mambotts.infra.engine_mgr import port_in_use

        # Never raises gaierror even on invalid hostname or IPv6 loopback.
        self.assertFalse(port_in_use("invalid.host.example.", 9880))
        _unused = port_in_use("::1", 59997)
        if getattr(socket, "has_ipv6", False):
            try:
                srv = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                srv.bind(("::1", 0))
                srv.listen(1)
                port = srv.getsockname()[1]
            except OSError:
                srv = None
                port = 0
            if srv is not None:
                try:
                    self.assertTrue(port_in_use("::1", port))
                finally:
                    srv.close()

    def test_download_7z_tool_enforces_sha256_and_abort_io_closes_sockets(self):
        import hashlib
        from unittest import mock
        from mambotts.infra.installer import (
            SEVEN_Z_SHA256,
            _abort_response_socket,
            _tracked_http_io,
            _valid_pe_tool,
            download_7z_tool,
            terminate_active_install_processes,
        )

        with tempfile.TemporaryDirectory(prefix="mambo_7z_sha_") as tmp:
            bad_exe = os.path.join(tmp, "bad_7zr.exe")
            fake_payload = b"MZ" + b"\x00" * (120 * 1024)
            with mock.patch("mambotts.infra.installer.requests.Session") as sess_cls:
                sess = sess_cls.return_value
                sess.__enter__.return_value = sess
                resp = mock.MagicMock()
                resp.__enter__.return_value = resp
                resp.raise_for_status.return_value = None
                resp.iter_content.return_value = [fake_payload]
                sess.get.return_value = resp
                with mock.patch("mambotts.infra.installer.time.sleep"):
                    with self.assertRaises(RuntimeError) as ctx:
                        download_7z_tool(bad_exe, lambda _m: None)
                self.assertIn("SHA-256", str(ctx.exception))
            self.assertFalse(os.path.exists(bad_exe))

            valid_payload = b"MZ" + b"X" * (120 * 1024)
            expected_sha = hashlib.sha256(valid_payload).hexdigest()
            good_exe = os.path.join(tmp, "good_7zr.exe")
            with (
                mock.patch("mambotts.infra.installer.SEVEN_Z_SHA256", expected_sha),
                mock.patch("mambotts.infra.installer.requests.Session") as sess_cls,
            ):
                sess = sess_cls.return_value
                sess.__enter__.return_value = sess
                resp = mock.MagicMock()
                resp.__enter__.return_value = resp
                resp.raise_for_status.return_value = None
                resp.iter_content.return_value = [valid_payload]
                sess.get.return_value = resp
                download_7z_tool(good_exe, lambda _m: None)
                self.assertTrue(_valid_pe_tool(good_exe))
            self.assertNotEqual(SEVEN_Z_SHA256, expected_sha)

            # Abort closes in-flight session + response immediately.
            fake_sess = mock.Mock()
            fake_resp = mock.Mock()
            fake_resp.raw = mock.Mock()
            with _tracked_http_io(session=fake_sess, response=fake_resp):
                terminate_active_install_processes()
            fake_resp.close.assert_called()
            fake_sess.close.assert_called()

    def test_egm_ship_readiness_infra_regressions(self):
        import requests
        from unittest import mock
        from mambotts.infra import ensure_deps
        from mambotts.infra.engine_mgr import EngineSupervisor, engine_log_path
        from mambotts.infra.installer import install_engine

        # 1. ensure_deps mirror speed probe uses scalar timeout, urllib response, and MIRRORS URLs.
        self.assertIsInstance(ensure_deps.PROBE_TIMEOUT, (int, float))

        class _FakeResp:
            status = 200

            def __init__(self, payload: bytes):
                self._payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def read(self):
                return self._payload

        html = b'<a href="../../packages/ff/PySide6_Addons-6.10.0-cp39-abi3-win_amd64.whl">whl</a>'
        with mock.patch("mambotts.infra.ensure_deps._open_direct", return_value=_FakeResp(html)) as open_mock:
            resolved = ensure_deps._resolve_wheel_url("https://mirrors.aliyun.com/pypi/simple")
        self.assertEqual(
            resolved,
            "https://mirrors.aliyun.com/pypi/packages/ff/PySide6_Addons-6.10.0-cp39-abi3-win_amd64.whl",
        )
        self.assertEqual(open_mock.call_args.kwargs["timeout"], ensure_deps.PROBE_TIMEOUT)

        seen_args: list[str] = []
        with mock.patch(
            "mambotts.infra.ensure_deps._probe_speed",
            side_effect=lambda u: seen_args.append(u) or (1000.0, ""),
        ):
            ranked = ensure_deps.benchmark_mirrors()
        self.assertTrue(len(ranked) == len(ensure_deps.MIRRORS))
        self.assertEqual(set(seen_args), set(ensure_deps.MIRRORS.values()))

        # 2. Issue #5 / PR #9: RequestException during streaming is never misreported as disk failure,
        #    cleans up .part, and preserves an existing output WAV; true disk OSError reports disk failure.
        with tempfile.TemporaryDirectory(prefix="mambo_gsv_stream_") as tmp:
            os.makedirs(os.path.join(tmp, "models"), exist_ok=True)
            out_wav = os.path.join(tmp, "existing.wav")
            with open(out_wav, "wb") as fh:
                fh.write(b"EXISTING_WAV")

            client = TTSClient(api_url="http://127.0.0.1:9880", project_dir=tmp)
            resp = mock.MagicMock()
            resp.__enter__.return_value = resp
            resp.status_code = 200
            resp.iter_content.side_effect = requests.exceptions.ContentDecodingError("bad stream")
            with (
                mock.patch.object(client, "is_api_running", return_value=True),
                mock.patch.object(client._session, "post", return_value=resp),
            ):
                ok, msg = client.synthesize_clause(
                    "你好",
                    out_wav,
                    VoicePreset("mambo:default", "mambo", "default", "", "t", "zh", "", ""),
                )
            self.assertFalse(ok)
            self.assertNotIn("保存文件失败", msg)
            self.assertIn("engine.log", msg)
            with open(out_wav, "rb") as fh:
                self.assertEqual(fh.read(), b"EXISTING_WAV")
            self.assertEqual([f for f in os.listdir(tmp) if f.endswith(".part")], [])
            self.assertTrue(os.path.isfile(engine_log_path(tmp)))

            # Disk write error reports 保存文件失败 cleanly.
            resp_ok = mock.MagicMock()
            resp_ok.__enter__.return_value = resp_ok
            resp_ok.status_code = 200
            resp_ok.iter_content.return_value = [b"RIFFdata"]
            with (
                mock.patch.object(client, "is_api_running", return_value=True),
                mock.patch.object(client._session, "post", return_value=resp_ok),
                mock.patch("builtins.open", side_effect=OSError("disk full")),
            ):
                ok_disk, msg_disk = client.synthesize_clause(
                    "你好",
                    out_wav,
                    VoicePreset("mambo:default", "mambo", "default", "", "t", "zh", "", ""),
                )
            self.assertFalse(ok_disk)
            self.assertIn("保存文件失败", msg_disk)

        # 3. Managed EngineSupervisor persists [引擎] lines and non-zero exit code to engine.log.
        with tempfile.TemporaryDirectory(prefix="mambo_enginelog_") as tmp:
            r_fd, w_fd = os.pipe()
            os.write(w_fd, b"Uvicorn running on http://127.0.0.1:9880\n")
            os.close(w_fd)
            proc = mock.Mock(pid=9999)
            proc.stdout = os.fdopen(r_fd, "r", encoding="utf-8")
            proc.poll.return_value = 137
            sup = EngineSupervisor(base_dir=tmp)
            sup._pump_stdout(proc)
            with open(engine_log_path(tmp), "r", encoding="utf-8") as fh:
                content = fh.read()
            self.assertIn("Uvicorn running", content)
            self.assertIn("退出码 137", content)

        # 4. Cancelled install during benchmark_sources reports cancelled, not "所有下载源均失败".
        with tempfile.TemporaryDirectory(prefix="mambo_bench_cancel_") as tmp:
            cancel_state = {"hit": False}

            def fake_bench(urls, _log_cb, cancel_cb=None, expected_bytes=0):
                cancel_state["hit"] = True
                return list(urls)

            with (
                mock.patch("mambotts.infra.installer.shutil.disk_usage", side_effect=_plenty_disk_usage),
                mock.patch("mambotts.infra.installer.benchmark_sources", side_effect=fake_bench),
                mock.patch("mambotts.infra.installer.download_file_resumable") as dl_mock,
            ):
                ok_inst, msg_inst = install_engine(
                    gpu_type="general",
                    base_dir=tmp,
                    log_cb=lambda _m: None,
                    progress_cb=lambda _p: None,
                    state_cb=lambda _s: None,
                    cancel_cb=lambda: cancel_state["hit"],
                )
            self.assertFalse(ok_inst)
            self.assertIn("取消", msg_inst)
            self.assertNotIn("所有下载源均失败", msg_inst)
            dl_mock.assert_not_called()


class TestAuditInfraRegressions(unittest.TestCase):
    def test_gsv_client_disconnect_classifies_bert_512_vs_cuda_oom(self):
        from unittest import mock
        import requests
        from mambotts.domain.models import VoicePreset
        from mambotts.infra.engine_mgr import engine_log_path
        from mambotts.infra.gsv_client import TTSClient

        with tempfile.TemporaryDirectory(prefix="mambo_disc_cls_") as tmp:
            os.makedirs(os.path.join(tmp, "models"), exist_ok=True)
            log_path = engine_log_path(tmp)
            client = TTSClient(api_url="http://127.0.0.1:9880", project_dir=tmp)
            preset = VoicePreset("mambo:default", "mambo", "default", "", "t", "zh", "", "")
            resp = mock.MagicMock()
            resp.__enter__.return_value = resp
            resp.status_code = 200
            resp.iter_content.side_effect = requests.exceptions.ChunkedEncodingError("mid-stream break")

            with (
                mock.patch.object(client, "is_api_running", return_value=True),
                mock.patch.object(client._session, "post", return_value=resp),
            ):
                with open(log_path, "w", encoding="utf-8") as fh:
                    fh.write("RuntimeError: get_bert_feature tensor size mismatch size of tensor b (512)\n")
                ok1, msg1 = client.synthesize_clause("你好", os.path.join(tmp, "a.wav"), preset)
                self.assertFalse(ok1)
                self.assertIn("BERT 512", msg1)
                self.assertNotIn("CUDA OOM", msg1)

                with open(log_path, "w", encoding="utf-8") as fh:
                    fh.write("torch.OutOfMemoryError: CUDA out of memory\n")
                ok2, msg2 = client.synthesize_clause("你好", os.path.join(tmp, "b.wav"), preset)
                self.assertFalse(ok2)
                self.assertIn("CUDA OOM", msg2)

    def test_ensure_deps_proxy_isolation_for_domestic_vs_pypi(self):
        from unittest import mock
        from mambotts.infra import ensure_deps

        captured: list[tuple[list[str], dict[str, str]]] = []

        def fake_run(cmd, env=None, **_kw):
            captured.append((list(cmd), dict(env or {})))
            return mock.Mock(returncode=0)

        logs: list[str] = []
        base_env = {
            **os.environ,
            "HTTP_PROXY": "http://127.0.0.1:7890",
            "HTTPS_PROXY": "http://127.0.0.1:7890",
            "ALL_PROXY": "socks5://127.0.0.1:7891",
            "PIP_PROXY": "http://127.0.0.1:7890",
        }
        with (
            mock.patch.dict(os.environ, base_env, clear=True),
            mock.patch("mambotts.infra.ensure_deps.subprocess.run", side_effect=fake_run),
        ):
            self.assertTrue(ensure_deps.install_from("tsinghua", "https://pypi.tuna.tsinghua.edu.cn/simple", logs.append))
            self.assertTrue(ensure_deps.install_from("pypi", "https://pypi.org/simple", logs.append))

        cmd_dom, env_dom = captured[0]
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "PIP_PROXY"):
            self.assertNotIn(key, env_dom)
        self.assertEqual(env_dom["NO_PROXY"], "*")
        self.assertEqual(env_dom["PIP_NO_PROXY"], "*")
        self.assertIn("urllib.request.getproxies = lambda: {}", " ".join(cmd_dom))

        cmd_pypi, env_pypi = captured[1]
        self.assertEqual(env_pypi.get("HTTP_PROXY"), "http://127.0.0.1:7890")
        self.assertIn("-m", cmd_pypi)
        self.assertIn("pip", cmd_pypi)

    def test_installer_disk_write_error_aborts_mirror_retries_extract_space_and_lock(self):
        import hashlib
        from unittest import mock
        from mambotts.infra import installer

        with tempfile.TemporaryDirectory(prefix="mambo_inst_audit_") as tmp:
            # 1. Local disk write failure stops mirror retries on first attempt.
            real_open = open
            part_open_calls = {"n": 0}
            sample_7z = b"7z\xbc\xaf\x27\x1c1234"
            dl_fake_gpu = {"general": {"file": "dl_pkg.7z", "label": "Mock GPU", "size_bytes": len(sample_7z), "sha256": hashlib.sha256(sample_7z).hexdigest()}}
            plenty_free = mock.Mock(total=100 * 1024**3, used=10 * 1024**3, free=90 * 1024**3)

            def failing_open(file, mode="r", *a, **kw):
                if isinstance(file, str) and file.endswith(".7z.part") and any(m in mode for m in ("w", "a", "+")):
                    part_open_calls["n"] += 1
                    raise OSError("simulated disk write failure")
                return real_open(file, mode, *a, **kw)

            sess = mock.MagicMock()
            sess.__enter__.return_value = sess
            resp = mock.MagicMock()
            resp.__enter__.return_value = resp
            resp.status_code = 200
            resp.raise_for_status.return_value = None
            resp.headers = {"Content-Length": str(len(sample_7z))}
            resp.raw = mock.MagicMock()
            resp.raw.read.side_effect = [sample_7z, b""]
            sess.get.return_value = resp

            with (
                mock.patch.dict(installer.GPU_PACKAGES, dl_fake_gpu, clear=True),
                mock.patch("mambotts.infra.installer.shutil.disk_usage", return_value=plenty_free),
                mock.patch("mambotts.infra.installer.benchmark_sources", return_value=["https://m1/x.7z", "https://m2/x.7z", "https://m3/x.7z"]),
                mock.patch("mambotts.infra.installer.requests.Session", return_value=sess),
                mock.patch("builtins.open", side_effect=failing_open),
            ):
                ok_dl, msg_dl = installer.install_engine(gpu_type="general", base_dir=tmp)
            self.assertFalse(ok_dl)
            self.assertIn("本地磁盘写入失败", msg_dl)
            self.assertEqual(part_open_calls["n"], 1)
            self.assertEqual(sess.get.call_count, 1)

            # 2. Pre-extract disk check aborts before 7zr extraction while preserving verified .7z.
            fake_bytes = b"7z\xbc\xaf\x27\x1c" + b"X" * 128
            fake_sha = hashlib.sha256(fake_bytes).hexdigest()
            pkg_dir = os.path.join(tmp, "engine_temp")
            os.makedirs(pkg_dir, exist_ok=True)
            fake_pkg_path = os.path.join(pkg_dir, "fake_pkg.7z")
            with open(fake_pkg_path, "wb") as fh:
                fh.write(fake_bytes)
            fake_gpu = {"general": {"file": "fake_pkg.7z", "label": "Mock GPU", "size_bytes": len(fake_bytes), "sha256": fake_sha}}
            du_calls = {"n": 0}

            def phased_disk_usage(_path):
                du_calls["n"] += 1
                if du_calls["n"] == 1:
                    return mock.Mock(total=10**9, used=10**9 - 5000, free=5000)
                return mock.Mock(total=10**9, used=10**9 - 10, free=10)

            with (
                mock.patch.dict(installer.GPU_PACKAGES, fake_gpu, clear=True),
                mock.patch("mambotts.infra.installer._valid_pe_tool", return_value=True),
                mock.patch("mambotts.infra.installer.download_7z_tool"),
                mock.patch("mambotts.infra.installer.shutil.disk_usage", side_effect=phased_disk_usage),
                mock.patch("mambotts.infra.installer.subprocess.Popen") as popen_mock,
            ):
                with open(os.path.join(tmp, "7zr.exe"), "wb") as fh:
                    fh.write(b"MZ" + b"\x00" * 32)
                ok_sp, msg_sp = installer.install_engine(gpu_type="general", base_dir=tmp)
            self.assertFalse(ok_sp)
            self.assertIn("解压所需磁盘空间不足", msg_sp)
            popen_mock.assert_not_called()
            self.assertTrue(os.path.isfile(fake_pkg_path))

            # 3. _acquire_install_lock prevents concurrent installs on the same directory.
            with installer._acquire_install_lock(tmp):
                with self.assertRaises(BlockingIOError):
                    with installer._acquire_install_lock(tmp):
                        pass
            with installer._acquire_install_lock(tmp):
                pass

    def test_engine_mgr_tqdm_disable_and_live_log_rotation_windows(self):
        import subprocess
        from unittest import mock
        from mambotts.infra import engine_mgr

        with tempfile.TemporaryDirectory(prefix="mambo_eng_audit_") as tmp:
            # 1. Supervisor injects TQDM_DISABLE=1 in spawned environment.
            fake_paths = {
                "base_dir": tmp,
                "gsv_dir": tmp,
                "models_dir": os.path.join(tmp, "models"),
                "python_exe": sys.executable,
                "api_py": os.path.join(tmp, "api.py"),
                "sovits": os.path.join(tmp, "models", "m.pth"),
                "gpt": os.path.join(tmp, "models", "m.ckpt"),
                "ref_wav": os.path.join(tmp, "models", "refer.wav"),
            }
            os.makedirs(fake_paths["models_dir"], exist_ok=True)
            for p in (fake_paths["api_py"], fake_paths["sovits"], fake_paths["gpt"], fake_paths["ref_wav"]):
                with open(p, "wb") as fh:
                    fh.write(b"x")
            sup = engine_mgr.EngineSupervisor(base_dir=tmp)
            proc = mock.Mock(pid=54321)
            proc.poll.return_value = None
            proc.stdout = mock.MagicMock()
            proc.stdout.__iter__.return_value = iter(())
            with (
                mock.patch("mambotts.infra.engine_mgr.engine_paths", return_value=fake_paths),
                mock.patch("mambotts.infra.engine_mgr.port_in_use", return_value=False),
                mock.patch("mambotts.infra.engine_mgr.bind_process_to_job_object", return_value=None),
                mock.patch("mambotts.infra.engine_mgr.subprocess.Popen", return_value=proc) as popen_mock,
            ):
                ok, _msg = sup.start("http://127.0.0.1:19880", bind_job_object=True)
            self.assertTrue(ok)
            env_passed = popen_mock.call_args.kwargs["env"]
            self.assertEqual(env_passed.get("TQDM_DISABLE"), "1")
            sup._stop_locked()

            # 2. On Windows, live child stdout opened via _open_detached_log_handle rotates cleanly with 0 NUL bytes.
            if sys.platform == "win32":
                log_path = engine_mgr.engine_log_path(tmp)
                handle = engine_mgr._open_detached_log_handle(log_path)
                child = None
                try:
                    script = (
                        "import sys, time; "
                        "print('BEFORE_ROTATE', flush=True); "
                        "sys.stdin.read(1); "
                        "print('AFTER_ROTATE', flush=True)"
                    )
                    child = subprocess.Popen(
                        [sys.executable, "-c", script],
                        stdin=subprocess.PIPE,
                        stdout=handle,
                        stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    handle.close()
                    handle = None

                    deadline = time.monotonic() + 5.0
                    while time.monotonic() < deadline:
                        if os.path.exists(log_path) and os.path.getsize(log_path) > 0:
                            break
                        time.sleep(0.02)

                    with open(log_path, "ab") as fh:
                        fh.write(b"X" * engine_mgr.ENGINE_LOG_MAX_BYTES)
                    engine_mgr._rotate_engine_log_if_needed(log_path)
                    self.assertTrue(os.path.isfile(f"{log_path}.1"))

                    assert child.stdin is not None
                    child.stdin.write(b"\n")
                    child.stdin.flush()
                    child.stdin.close()
                    child.wait(timeout=5)
                    engine_mgr.append_engine_log("PARENT_NEW_FILE", base_dir=tmp)
                finally:
                    if handle is not None:
                        handle.close()
                    if child is not None and child.poll() is None:
                        child.kill()
                        child.wait(timeout=2)

                with open(f"{log_path}.1", "rb") as fh:
                    rotated_bytes = fh.read()
                self.assertIn(b"AFTER_ROTATE", rotated_bytes)
                self.assertEqual(rotated_bytes.count(b"\x00"), 0)

                with open(log_path, "rb") as fh:
                    new_bytes = fh.read()
                self.assertIn(b"PARENT_NEW_FILE", new_bytes)
                self.assertEqual(new_bytes.count(b"\x00"), 0)

    def test_safe_print_installer_and_ensure_deps_survive_cp1252_stdout(self):
        import io
        from unittest import mock
        from mambotts.infra import ensure_deps, installer, safe_print

        raw_out = io.BytesIO()
        cp1252_out = io.TextIOWrapper(raw_out, encoding="cp1252", errors="strict")
        with mock.patch.object(sys, "stdout", cp1252_out):
            safe_print("[状态] 中文调试输出", flush=True)
            with mock.patch(
                "mambotts.infra.ensure_deps._probe_speed",
                side_effect=lambda _u: (12.5, None),
            ):
                ranked = ensure_deps.benchmark_mirrors()
            self.assertEqual(len(ranked), len(ensure_deps.MIRRORS))

            with tempfile.TemporaryDirectory(prefix="mambo_cp1252_inst_") as tmp:
                with (
                    mock.patch("mambotts.infra.installer.shutil.disk_usage", side_effect=_plenty_disk_usage),
                    mock.patch(
                        "mambotts.infra.installer.benchmark_sources",
                        side_effect=lambda urls, *_a, **_kw: list(urls),
                    ),
                    mock.patch(
                        "mambotts.infra.installer.download_file_resumable",
                        return_value=False,
                    ),
                ):
                    ok, msg = installer.install_engine(
                        gpu_type="general",
                        base_dir=tmp,
                        log_cb=None,
                        progress_cb=lambda _p: None,
                        state_cb=lambda _s: None,
                        cancel_cb=lambda: True,
                    )
                self.assertFalse(ok)
                self.assertEqual(msg, "下载尚未开始。")
                with self.assertRaises(installer._InstallCancelled):
                    installer.download_7z_tool(
                        os.path.join(tmp, "7zr.exe"),
                        log_cb=None,
                        cancel_cb=lambda: True,
                    )
        cp1252_out.flush()
        output = raw_out.getvalue().decode("cp1252")
        self.assertIn("[??]", output)
        self.assertIn("MamboTTS", output)
        cp1252_out.detach()


if __name__ == "__main__":
    unittest.main()
