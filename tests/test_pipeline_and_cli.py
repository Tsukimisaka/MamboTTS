"""Unit & integration tests for ``mambotts.pipeline`` and ``mambotts.cli``.

All synthesis runs against a mock ``GSVHttpClient`` (spec item 25): no real
engine, no network.  Covers:

* Text helpers — ``has_pronounceable_char``, ``normalize_script_text``.
* ``dsp_srt`` — 24k/32k/48k resampling, silence trimming incl. all-silence
  guard, peak normalization to -1.0 dBFS, zero cumulative SRT drift across 40
  clauses, mixed-rate assembly, internal ``zero_wav`` SRT splitting.
* ``SegmentCache`` / ``SynthesisPipeline.run_job`` — whole-text cache reuse,
  cancellation returning ``SynthesisJobResult``, heartbeat interlock
  (``should_poll_health() == False`` during the whole run).
* ``run_cli`` — monkeypatched pipeline: ``synth --json``, ``batch --srt
  --manifest`` (per-clause WAVs + sha256 manifest),
  ``engine status --json``, exit codes 0/1/2/3, and a hard "never import
  PySide6" guarantee.
"""
from __future__ import annotations

import array
import ast
import contextlib
import hashlib
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from mambotts.domain.models import (  # noqa: E402
    CUT_PUNC,
    SPEED_MAX,
    SPEED_MIN,
    ClauseResult,
    SegmentSpec,
    SynthesisJobResult,
    VoicePreset,
    has_pronounceable_char,
    normalize_script_text,
)
from mambotts.pipeline import dsp_srt  # noqa: E402
from mambotts.pipeline.scheduler import SegmentCache, SynthesisPipeline  # noqa: E402

try:  # real supervisor state machine (Worker-2 deliverable)
    from mambotts.infra.engine_mgr import EngineSupervisor as RealEngineSupervisor

    HAVE_ENGINE_MGR = True
except Exception:  # pragma: no cover - infra not present yet
    RealEngineSupervisor = None
    HAVE_ENGINE_MGR = False


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class FakeVoiceRepo:
    """Duck-typed ``VoiceRepository``: the single built-in preset."""

    def __init__(self) -> None:
        self.preset = VoicePreset(
            id="mambo:default", character="mambo", emotion="default",
            ref_wav="", ref_text="大家好",
        )

    def scan(self):
        return [self.preset]

    def get(self, query: str = "") -> VoicePreset:
        return self.preset


class FakeGSVClient:
    """Mock ``GSVHttpClient``: writes a real deterministic WAV per clause."""

    def __init__(self, sample_rate: int = 24000, online: bool = True,
                 fail_on: set[str] | None = None, supervisor=None) -> None:
        self.sample_rate = sample_rate
        self.online = online
        self.fail_on = fail_on or set()
        self.supervisor = supervisor
        self.calls: list[dict] = []
        self.probe_results: list[bool] = []

    @staticmethod
    def samples_for(text: str) -> int:
        return 1200 + (len(text or "") * 137) % 1200

    def is_api_running(self, timeout: float = 1.5, api_url=None) -> bool:
        return self.online

    def synthesize_clause(self, text, save_path, preset, speed=1.0, text_lang="zh",
                          api_url=None, cut_punc=CUT_PUNC, cancel_event=None, **kwargs):
        self.calls.append({
            "text": text, "save_path": save_path, "preset": preset,
            "speed": speed, "text_lang": text_lang, "api_url": api_url,
            "cut_punc": cut_punc,
        })
        if self.supervisor is not None:
            self.probe_results.append(self.supervisor.should_poll_health())
        if text in self.fail_on:
            return (False, "mock synthesis failure")
        count = self.samples_for(text)
        samples = array.array("h", [((count + i) % 17 + 1) * 300 for i in range(count)])
        dsp_srt.write_pcm16_mono(save_path, self.sample_rate, samples)
        return (True, save_path)


class FakeSupervisor:
    """Duck-typed ``EngineSupervisor`` heartbeat-interlock state machine."""

    def __init__(self) -> None:
        self.busy = False
        self.online = False
        self.busy_history: list[bool] = []

    def set_synthesis_busy(self, busy: bool) -> None:
        self.busy = bool(busy)
        self.busy_history.append(self.busy)

    def mark_online(self, online: bool = True) -> None:
        self.online = bool(online)

    def should_poll_health(self) -> bool:
        return self.online and not self.busy


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mambotts_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Text normalization & pronounceability guards
# ---------------------------------------------------------------------------

class TestHasPronounceableChar(TempDirTestCase):
    def test_basic(self):
        self.assertTrue(has_pronounceable_char("你好"))
        self.assertTrue(has_pronounceable_char("。。。x"))
        self.assertFalse(has_pronounceable_char("。，！"))
        self.assertFalse(has_pronounceable_char("   "))
        self.assertFalse(has_pronounceable_char(""))

    def test_normalize_script_text(self):
        self.assertEqual(
            normalize_script_text("第一行。\n\n  第二行。 \t [Ctrl+C] "),
            "第一行。 第二行。 [Ctrl+C]",
        )
        self.assertEqual(normalize_script_text("   \n  "), "")


# ---------------------------------------------------------------------------
# dsp_srt
# ---------------------------------------------------------------------------

class TestResamplePcm16(TempDirTestCase):
    def _sine(self, count: int, period: int = 50) -> array.array:
        return array.array(
            "h", [int(10000 * math.sin(i * math.pi / period)) for i in range(count)]
        )

    def test_24k_to_48k_and_back(self):
        src = self._sine(2400)
        up = dsp_srt.resample_pcm16(src, 24000, 48000)
        self.assertEqual(len(up), max(1, round(2400 * 48000 / 24000)))
        self.assertAlmostEqual(len(up) / 48000, len(src) / 24000, places=9)
        down = dsp_srt.resample_pcm16(up, 48000, 24000)
        self.assertEqual(len(down), 2400)
        self.assertAlmostEqual(max(abs(v) for v in down), 10000, delta=200)

    def test_32k_to_24k(self):
        src = self._sine(3200)
        out = dsp_srt.resample_pcm16(src, 32000, 24000)
        self.assertEqual(len(out), max(1, round(3200 * 24000 / 32000)))
        self.assertAlmostEqual(max(abs(v) for v in out), 10000, delta=250)

    def test_24k_to_32k(self):
        src = self._sine(2400)
        out = dsp_srt.resample_pcm16(src, 24000, 32000)
        self.assertEqual(len(out), max(1, round(2400 * 32000 / 24000)))

    def test_same_rate_returns_equal(self):
        src = self._sine(100)
        self.assertEqual(list(dsp_srt.resample_pcm16(src, 24000, 24000)), list(src))

    def test_edge_cases(self):
        self.assertEqual(len(dsp_srt.resample_pcm16(array.array("h"), 24000, 48000)), 0)
        single = dsp_srt.resample_pcm16(array.array("h", [12345]), 24000, 48000)
        self.assertEqual(len(single), max(1, round(1 * 48000 / 24000)))
        self.assertTrue(all(0 <= v <= 12345 for v in single))
        clamped = dsp_srt.resample_pcm16(array.array("h", [32767, -32768]), 24000, 96000)
        self.assertTrue(all(-32768 <= v <= 32767 for v in clamped))


class TestTrimSilencePcm16(TempDirTestCase):
    def _signal(self) -> tuple[int, array.array]:
        rate = 24000
        lead = [0] * 4800  # 200 ms silence
        body = [10000 if i % 2 == 0 else -10000 for i in range(2400)]  # 100 ms
        tail = [0] * 9600  # 400 ms silence
        return rate, array.array("h", lead + body + tail)

    def test_trims_with_default_pads(self):
        rate, samples = self._signal()
        out = dsp_srt.trim_silence_pcm16(samples, rate)
        self.assertEqual(len(out), 2400 + 720 + 1440)  # body + 30ms + 60ms pads
        self.assertEqual(max(abs(v) for v in out[:700]), 0)  # head pad still silence
        self.assertEqual(abs(out[720 + 300]), 10000)  # body preserved past the fade

    def test_fade_at_cut_boundaries(self):
        rate = 24000
        lead = [100] * 4800  # 200 ms sub-threshold floor (-45 dBFS ≈ 184)
        body = [10000 if i % 2 == 0 else -10000 for i in range(2400)]  # 100 ms speech
        tail = [100] * 9600  # 400 ms sub-threshold floor
        samples = array.array("h", lead + body + tail)

        # Fade is clamped inside the preserved silence cushions so active speech stays intact.
        out_padded = dsp_srt.trim_silence_pcm16(samples, rate, pad_head_ms=10, pad_tail_ms=10)
        self.assertEqual(len(out_padded), 240 + 2400 + 240)
        self.assertLess(abs(out_padded[0]), 10)  # cushion cut boundary faded from ~0
        self.assertAlmostEqual(out_padded[239], 100, delta=5)  # cushion end reaches floor
        self.assertEqual(abs(out_padded[240]), 10000)  # speech onset NOT attenuated
        self.assertEqual(abs(out_padded[240 + 2399]), 10000)  # speech release NOT attenuated
        self.assertLess(abs(out_padded[-1]), 10)  # tail cut boundary faded toward ~0

        # Zero silence cushion must never attenuate active speech boundaries.
        out_zero = dsp_srt.trim_silence_pcm16(samples, rate, pad_head_ms=0, pad_tail_ms=0)
        self.assertEqual(len(out_zero), 2400)
        self.assertEqual(abs(out_zero[0]), 10000)
        self.assertEqual(out_zero[1200], body[1200])
        self.assertEqual(abs(out_zero[-1]), 10000)

    def test_all_silence_guard_returns_original(self):
        rate = 24000
        samples = array.array("h", bytes(2 * 4800))
        result = dsp_srt.trim_silence_pcm16(samples, rate)
        self.assertIs(result, samples)
        quiet = array.array("h", [100, -90, 120] * 100)  # below -45 dBFS (≈184)
        self.assertIs(dsp_srt.trim_silence_pcm16(quiet, rate), quiet)


class TestPeakNormalizePcm16(TempDirTestCase):
    def test_normalizes_to_minus_1_dbfs(self):
        samples = array.array("h", [0, 16384, -16384, 8192])
        out = dsp_srt.peak_normalize_pcm16(samples, target_db=-1.0)
        target_peak = 32767 * (10 ** (-1.0 / 20.0))  # ≈ 29201
        self.assertAlmostEqual(max(abs(v) for v in out), target_peak, delta=2)
        self.assertAlmostEqual(out[3], 8192 * target_peak / 16384, delta=2)

    def test_gain_capped_at_4x(self):
        samples = array.array("h", [0, 100, -100, 50])
        out = dsp_srt.peak_normalize_pcm16(samples, target_db=-1.0)
        self.assertEqual(max(abs(v) for v in out), 400)

    def test_zero_peak_guard(self):
        samples = array.array("h", bytes(64))
        self.assertIs(dsp_srt.peak_normalize_pcm16(samples), samples)

    def test_table_path_matches_direct_math(self):
        import random

        rng = random.Random(7)
        samples = array.array(
            "h", [rng.randint(-32768, 32767) for _ in range(1000)]
        )
        out = dsp_srt.peak_normalize_pcm16(samples, target_db=-6.0)
        peak = max(-min(samples), max(samples))
        target = 32767 * (10 ** (-6.0 / 20.0))
        gain = min(4.0, target / peak)
        if 0.999 <= gain <= 1.001:
            expected = list(samples)
        else:
            expected = [dsp_srt._clamp_int16(v * gain) for v in samples]
        self.assertEqual(list(out), expected)

    def test_all_negative_signal_normalizes(self):
        samples = array.array("h", [-100, -50, -80])
        out = dsp_srt.peak_normalize_pcm16(samples, target_db=-1.0)
        self.assertEqual(max(abs(v) for v in out), 400)  # gain capped at 4x


class TestFormatSrtTimestamp(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(dsp_srt.format_srt_timestamp(0), "00:00:00,000")
        self.assertEqual(dsp_srt.format_srt_timestamp(1234), "00:00:01,234")
        self.assertEqual(dsp_srt.format_srt_timestamp(3661000), "01:01:01,000")
        self.assertEqual(dsp_srt.format_srt_timestamp(35999999), "09:59:59,999")
        self.assertEqual(dsp_srt.format_srt_timestamp(-5), "00:00:00,000")


class TestPcm16Io(TempDirTestCase):
    def test_write_read_roundtrip(self):
        path = os.path.join(self.tmp, "round.wav")
        samples = array.array("h", [0, 1, -1, 32767, -32768, 1000, -1000])
        dsp_srt.write_pcm16_mono(path, 24000, samples)
        rate, read_back = dsp_srt.read_pcm16_mono(path)
        self.assertEqual(rate, 24000)
        self.assertEqual(list(read_back), list(samples))
        leftovers = [n for n in os.listdir(self.tmp) if n.endswith(".part")]
        self.assertEqual(leftovers, [])

    def test_read_8bit_mono(self):
        import wave

        path = os.path.join(self.tmp, "u8.wav")
        with wave.open(path, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(1)
            writer.setframerate(22050)
            writer.writeframes(bytes([0, 128, 255]))
        rate, samples = dsp_srt.read_pcm16_mono(path)
        self.assertEqual(rate, 22050)
        self.assertEqual(list(samples), [-32768, 0, (255 - 128) * 256])

    def test_read_32bit_mono(self):
        import wave

        path = os.path.join(self.tmp, "i32.wav")
        raw = array.array("i", [1 << 20, -(1 << 20), 32767 << 16, -(1 << 30)])
        with wave.open(path, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(4)
            writer.setframerate(48000)
            writer.writeframes(raw.tobytes())
        _rate, samples = dsp_srt.read_pcm16_mono(path)
        self.assertEqual(list(samples), [16, -16, 32767, -16384])

    def test_read_stereo_downmix_average(self):
        import wave

        path = os.path.join(self.tmp, "stereo.wav")
        with wave.open(path, "wb") as writer:
            writer.setnchannels(2)
            writer.setsampwidth(2)
            writer.setframerate(24000)
            writer.writeframes(array.array("h", [100, 200, -100, -200]).tobytes())
        _rate, samples = dsp_srt.read_pcm16_mono(path)
        self.assertEqual(list(samples), [150, -150])


class TestAssembleClausesAndSrt(TempDirTestCase):
    def test_zero_cumulative_drift_across_40_clauses(self):
        rate = 24000
        lengths = [900 + (i * 37) % 1500 for i in range(40)]
        specs, paths = [], []
        for index, count in enumerate(lengths):
            specs.append(SegmentSpec(
                index=index,
                text=f"第{index + 1}句测试内容。",
                voice_id="mambo:default",
            ))
            path = os.path.join(self.tmp, f"clause_{index:03d}.wav")
            dsp_srt.write_pcm16_mono(
                path, rate,
                array.array("h", [((index % 7) + 1) * 400] * count),
            )
            paths.append(path)
        clause_wavs = [(specs[i], paths[i], i % 3 == 0) for i in range(40)]
        out_wav = os.path.join(self.tmp, "out.wav")
        out_srt = os.path.join(self.tmp, "out.srt")

        duration, results = dsp_srt.assemble_clauses_and_srt(
            clause_wavs, out_wav, out_srt, trim_silence=False, normalize=False,
        )

        self.assertEqual(len(results), 40)
        offset = 0
        for index in range(40):
            start_ms = round(offset * 1000.0 / rate)
            offset += lengths[index]
            end_ms = round(offset * 1000.0 / rate)
            self.assertEqual(results[index].start_ms, start_ms,
                             f"clause {index} start drifted")
            self.assertEqual(results[index].end_ms, end_ms,
                             f"clause {index} end drifted")
            self.assertEqual(results[index].duration_ms, end_ms - start_ms)
            self.assertEqual(results[index].text, specs[index].text)
            self.assertEqual(results[index].from_cache, index % 3 == 0)
        total_samples = sum(lengths)
        self.assertEqual(results[-1].end_ms, round(total_samples * 1000.0 / rate))
        self.assertAlmostEqual(duration, offset / rate, places=9)

        out_rate, out_samples = dsp_srt.read_pcm16_mono(out_wav)
        self.assertEqual(out_rate, rate)
        self.assertEqual(len(out_samples), offset)
        with open(out_srt, "r", encoding="utf-8") as handle:
            srt_text = handle.read()
        self.assertEqual(srt_text.count("-->"), 40)
        self.assertIn("00:00:00,000 -->", srt_text)
        self.assertIn(f"{results[39].text}\n", srt_text)

    def test_mixed_sample_rates_resampled_to_master(self):
        p48 = os.path.join(self.tmp, "r48.wav")
        p24 = os.path.join(self.tmp, "r24.wav")
        dsp_srt.write_pcm16_mono(p48, 48000, array.array("h", [800] * 4800))
        dsp_srt.write_pcm16_mono(p24, 24000, array.array("h", [900] * 2400))
        duration, results = dsp_srt.assemble_clauses_and_srt(
            [(SegmentSpec(index=0, text="甲。", voice_id="mambo:default"), p48, False),
             (SegmentSpec(index=1, text="乙。", voice_id="mambo:default"), p24, False)],
            os.path.join(self.tmp, "mixed.wav"), None,
            trim_silence=False, normalize=False,
        )
        out_rate, out_samples = dsp_srt.read_pcm16_mono(os.path.join(self.tmp, "mixed.wav"))
        self.assertEqual(out_rate, 48000)  # master_rate comes from the first clause
        self.assertEqual(len(out_samples), 4800 + round(2400 * 48000 / 24000))
        self.assertAlmostEqual(duration, 0.2, places=6)
        self.assertEqual(results[1].start_ms, results[0].end_ms)

    def test_normalize_keeps_length(self):
        p = os.path.join(self.tmp, "quiet.wav")
        # peak 10000 -> gain ≈ 2.92 (below the 4.0 cap) → output peak ≈ -1 dBFS
        dsp_srt.write_pcm16_mono(p, 24000, array.array("h", [10000, -10000, 5000, -5000] * 600))
        _duration, _results = dsp_srt.assemble_clauses_and_srt(
            [(SegmentSpec(index=0, text="轻声。", voice_id="mambo:default"), p, False)],
            os.path.join(self.tmp, "loud.wav"), None,
            trim_silence=False, normalize=True,
        )
        _rate, out = dsp_srt.read_pcm16_mono(os.path.join(self.tmp, "loud.wav"))
        self.assertEqual(len(out), 2400)
        self.assertAlmostEqual(max(abs(v) for v in out), 32767 * (10 ** -0.05), delta=2)

    def test_empty_clause_list_raises(self):
        with self.assertRaises(ValueError):
            dsp_srt.assemble_clauses_and_srt([], os.path.join(self.tmp, "x.wav"))


# ---------------------------------------------------------------------------
# SegmentCache
# ---------------------------------------------------------------------------

class TestSegmentCache(TempDirTestCase):
    def _wav(self, name: str) -> str:
        path = os.path.join(self.tmp, name)
        dsp_srt.write_pcm16_mono(path, 24000, array.array("h", [100, -100, 200] * 50))
        return path

    def test_put_get_roundtrip(self):
        cache = SegmentCache(cache_dir=os.path.join(self.tmp, "cache"))
        src = self._wav("src.wav")
        stored = cache.put("key1", src)
        self.assertTrue(os.path.isfile(stored))
        self.assertEqual(cache.get("key1"), stored)

    def test_get_rejects_corrupt_file(self):
        cache = SegmentCache(cache_dir=os.path.join(self.tmp, "cache"))
        with open(cache.path_for("bad"), "wb") as handle:
            handle.write(b'{"error": "not a wav"}')
        self.assertIsNone(cache.get("bad"))
        self.assertIsNone(cache.get("missing"))

    def test_get_touches_mtime(self):
        cache = SegmentCache(cache_dir=os.path.join(self.tmp, "cache"))
        stored = cache.put("k", self._wav("src.wav"))
        os.utime(stored, (1000, 1000))
        cache.get("k")
        self.assertGreater(os.stat(stored).st_mtime, 1000)

    def test_invalidate_and_clear(self):
        cache = SegmentCache(cache_dir=os.path.join(self.tmp, "cache"))
        cache.put("a", self._wav("a.wav"))
        cache.put("b", self._wav("b.wav"))
        cache.invalidate("a")
        self.assertIsNone(cache.get("a"))
        self.assertIsNotNone(cache.get("b"))
        cache.clear()
        self.assertIsNone(cache.get("b"))

    def test_lru_prune_evicts_oldest_by_mtime(self):
        cache = SegmentCache(cache_dir=os.path.join(self.tmp, "cache"), max_entries=2)
        first = cache.put("k1", self._wav("a.wav"))
        os.utime(first, (1000, 1000))
        second = cache.put("k2", self._wav("b.wav"))
        os.utime(second, (2000, 2000))
        cache.put("k3", self._wav("c.wav"))  # triggers prune: k1 (oldest) evicted
        self.assertFalse(os.path.isfile(cache.path_for("k1")))
        self.assertTrue(os.path.isfile(cache.path_for("k2")))
        self.assertTrue(os.path.isfile(cache.path_for("k3")))

    def test_default_dir_under_tempdir(self):
        cache = SegmentCache()
        self.assertEqual(
            cache.cache_dir,
            os.path.join(tempfile.gettempdir(), "mambotts_clause_cache"),
        )


# ---------------------------------------------------------------------------
# SynthesisPipeline (mock GSVHttpClient)
# ---------------------------------------------------------------------------

class TestSynthesisPipeline(TempDirTestCase):
    def _pipeline(self, client=None, supervisor=None):
        supervisor = supervisor if supervisor is not None else FakeSupervisor()
        client = client if client is not None else FakeGSVClient(supervisor=supervisor)
        cache = SegmentCache(cache_dir=os.path.join(self.tmp, "cache"))
        pipeline = SynthesisPipeline(
            FakeVoiceRepo(), client, supervisor=supervisor, cache=cache,
        )
        return pipeline, client, supervisor

    def test_run_job_single_request_with_srt(self):
        pipeline, client, supervisor = self._pipeline()
        save = os.path.join(self.tmp, "out.wav")
        result = pipeline.run_job(
            "大家好，我是曼波。\n今天讲一个好玩的故事！",
            save,
            speed=1.2,
            export_srt=True,
        )
        self.assertIsInstance(result, SynthesisJobResult)
        self.assertTrue(result.success, result.message)
        self.assertTrue(os.path.isfile(result.wav_path))
        self.assertEqual(result.srt_path, os.path.splitext(save)[0] + ".srt")
        self.assertTrue(os.path.isfile(result.srt_path))
        self.assertEqual(len(result.clauses), 1)
        self.assertEqual(result.clauses[0].voice_id, "mambo:default")
        # Full normalized script goes to engine in one request with full cut_punc.
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["cut_punc"], CUT_PUNC)
        self.assertAlmostEqual(client.calls[0]["speed"], 1.2)
        self.assertEqual(client.calls[0]["preset"].id, "mambo:default")
        # successful HTTP synthesis marks the supervisor online
        self.assertTrue(supervisor.online)
        with open(result.srt_path, "r", encoding="utf-8") as handle:
            srt_text = handle.read()
        self.assertGreaterEqual(srt_text.count("-->"), 1)

    def test_cache_reuse_second_run_never_calls_engine(self):
        pipeline, client, _supervisor = self._pipeline()
        text = "第一句内容。第二句内容。"
        first = pipeline.run_job(text, os.path.join(self.tmp, "a.wav"))
        self.assertTrue(first.success)
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(all(not c.from_cache for c in first.clauses))

        second = pipeline.run_job(text, os.path.join(self.tmp, "b.wav"))
        self.assertTrue(second.success)
        self.assertEqual(len(client.calls), 1)  # engine untouched
        self.assertTrue(all(c.from_cache for c in second.clauses))
        self.assertEqual([c.text for c in second.clauses], [c.text for c in first.clauses])

    def test_engine_always_gets_full_cut_punc(self):
        pipeline, client, _supervisor = self._pipeline()
        pipeline.run_job("第一句。第二句。", os.path.join(self.tmp, "a.wav"))
        self.assertEqual(client.calls[0]["cut_punc"], CUT_PUNC)
        # Same rule for any other text: the engine owns punctuation breaking.
        pipeline.run_job(
            "换个没缓存过的句子，验证引擎侧切分。", os.path.join(self.tmp, "b.wav"),
        )
        self.assertEqual(client.calls[-1]["cut_punc"], CUT_PUNC)

    def test_pre_cancelled_event_returns_result_without_engine_calls(self):
        pipeline, client, _supervisor = self._pipeline()
        cancel_event = threading.Event()
        cancel_event.set()
        result = pipeline.run_job("第一句。第二句。", os.path.join(self.tmp, "a.wav"),
                                  cancel_event=cancel_event)
        self.assertIsInstance(result, SynthesisJobResult)
        self.assertFalse(result.success)
        self.assertIn("取消", result.message)
        self.assertEqual(len(client.calls), 0)

    def test_mid_run_cancellation(self):
        supervisor = FakeSupervisor()
        cancel_event = threading.Event()
        client = FakeGSVClient(supervisor=supervisor)
        original = client.synthesize_clause

        def hooked(text, save_path, preset, **kwargs):
            outcome = original(text, save_path, preset, **kwargs)
            if len(client.calls) == 1:  # flip the event after the first clause
                cancel_event.set()
            return outcome

        client.synthesize_clause = hooked
        pipeline, _client, _sup = self._pipeline(client=client, supervisor=supervisor)
        result = pipeline.run_job("第一句。第二句。", os.path.join(self.tmp, "a.wav"),
                                  cancel_event=cancel_event)
        self.assertIsInstance(result, SynthesisJobResult)
        self.assertFalse(result.success)
        self.assertIn("取消", result.message)
        self.assertEqual(len(client.calls), 1)  # second clause never attempted

    def test_heartbeat_interlock_busy_throughout_run(self):
        supervisor = FakeSupervisor()
        supervisor.mark_online(True)  # engine known-online before the job
        client = FakeGSVClient(supervisor=supervisor)
        pipeline, _client, _sup = self._pipeline(client=client, supervisor=supervisor)
        result = pipeline.run_job("第一句内容。第二句内容。第三句内容。",
                                  os.path.join(self.tmp, "a.wav"))
        self.assertTrue(result.success)
        self.assertTrue(client.probe_results)  # probes were sampled
        self.assertFalse(any(client.probe_results),
                         "should_poll_health() must be False during synthesis")
        self.assertEqual(supervisor.busy_history, [True, False])
        self.assertTrue(supervisor.should_poll_health())  # released after run

    @unittest.skipUnless(HAVE_ENGINE_MGR, "mambotts.infra.engine_mgr unavailable")
    def test_real_supervisor_interlock_states(self):
        supervisor = RealEngineSupervisor()
        supervisor.mark_online(True)
        self.assertTrue(supervisor.should_poll_health())
        supervisor.set_synthesis_busy(True)
        self.assertFalse(supervisor.should_poll_health())
        supervisor.mark_online(True)  # online updates never bypass busy
        self.assertFalse(supervisor.should_poll_health())
        supervisor.set_synthesis_busy(False)
        self.assertTrue(supervisor.should_poll_health())

    def test_single_voice_no_fallback_notes(self):
        pipeline, client, _supervisor = self._pipeline()
        result = pipeline.run_job(
            "第一句内容。第二句内容。",
            os.path.join(self.tmp, "a.wav"),
        )
        self.assertTrue(result.success)
        self.assertEqual(len(client.calls), 1)
        # Single built-in voice: never a fallback, never an attention note.
        self.assertEqual(result.message, "合成完成")
        self.assertTrue(all(c.voice_id == "mambo:default" for c in result.clauses))

    def test_single_clause_fast_path(self):
        pipeline, client, _supervisor = self._pipeline()
        save = os.path.join(self.tmp, "fast.wav")
        result = pipeline.run_job(
            "只有一句话没有标点", save,
            trim_silence=False, normalize_audio=False,
        )
        self.assertTrue(result.success)
        self.assertEqual(len(result.clauses), 1)
        self.assertEqual(result.srt_path, "")
        self.assertEqual(os.path.abspath(result.clauses[0].wav_path), os.path.abspath(save))
        self.assertEqual(result.clauses[0].start_ms, 0)
        expected = FakeGSVClient.samples_for("只有一句话没有标点")
        self.assertAlmostEqual(result.duration_sec, expected / 24000, places=6)
        _rate, samples = dsp_srt.read_pcm16_mono(save)
        self.assertEqual(len(samples), expected)

    def test_no_fast_path_when_trim_requested(self):
        pipeline, _client, _supervisor = self._pipeline()
        save = os.path.join(self.tmp, "trimmed.wav")
        result = pipeline.run_job(
            "只有一句话没有标点", save,
            trim_silence=True, normalize_audio=False,
        )
        self.assertTrue(result.success)
        # assembled (non-fast-path) results keep cache paths on ClauseResult
        self.assertNotEqual(os.path.abspath(result.clauses[0].wav_path), os.path.abspath(save))
        self.assertTrue(os.path.isfile(save))

    def test_empty_and_unpronounceable_text_guards(self):
        pipeline, client, _supervisor = self._pipeline()
        for text in ("", "   ", "。。。", "，。！"):
            with self.subTest(text=text):
                result = pipeline.run_job(text, os.path.join(self.tmp, "a.wav"))
                self.assertIsInstance(result, SynthesisJobResult)
                self.assertFalse(result.success)
        self.assertEqual(len(client.calls), 0)

    def test_engine_failure_returns_result(self):
        pipeline, _client, supervisor = self._pipeline(
            client=FakeGSVClient(fail_on={"第一句。第二句。"}, supervisor=FakeSupervisor()),
        )
        result = pipeline.run_job("第一句。第二句。", os.path.join(self.tmp, "a.wav"))
        self.assertIsInstance(result, SynthesisJobResult)
        self.assertFalse(result.success)
        self.assertIn("第 1 句", result.message)
        self.assertFalse(supervisor.busy)  # released via try/finally

    def test_progress_cb_reports(self):
        pipeline, _client, _supervisor = self._pipeline()
        events: list[tuple[int, int, str]] = []
        result = pipeline.run_job("第一句。第二句。", os.path.join(self.tmp, "a.wav"),
                                  progress_cb=lambda done, total, stage: events.append((done, total, stage)))
        self.assertTrue(result.success)
        # Single spec: start -> synthesized -> export.
        self.assertEqual([done for done, _total, _stage in events], [0, 1, 1])
        self.assertTrue(all(total == 1 for _done, total, _stage in events))

    def test_use_cache_false_forces_fresh_synthesis(self):
        pipeline, client, _supervisor = self._pipeline()
        pipeline.run_job("第一句内容。", os.path.join(self.tmp, "a.wav"))
        self.assertEqual(len(client.calls), 1)
        pipeline.run_job("第一句内容。", os.path.join(self.tmp, "b.wav"), use_cache=False)
        self.assertEqual(len(client.calls), 2)


# ---------------------------------------------------------------------------
# CLI (monkeypatched pipeline)
# ---------------------------------------------------------------------------

class StubPipeline:
    """Replaces ``SynthesisPipeline`` in CLI tests; writes real WAV artifacts."""

    clause_dirs: list[str] = []
    instances: list["StubPipeline"] = []

    def __init__(self, voice_repo, gsv_client, supervisor=None, cache=None) -> None:
        self.run_job_calls: list[dict] = []
        StubPipeline.instances.append(self)

    def run_job(self, text, save_path, **kwargs):
        save_path = os.path.abspath(save_path)
        self.run_job_calls.append({"text": text, "save_path": save_path, **kwargs})
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        rate = 24000
        dsp_srt.write_pcm16_mono(
            save_path, rate, array.array("h", [(i % 31) * 300 for i in range(4800)]),
        )
        clause_dir = tempfile.mkdtemp(prefix="mambotts_stub_clauses_")
        StubPipeline.clause_dirs.append(clause_dir)
        clauses = []
        offset = 0
        for index in range(2):
            wav = os.path.join(clause_dir, f"clause_{index}.wav")
            dsp_srt.write_pcm16_mono(wav, rate, array.array("h", [(index + 1) * 500] * 1200))
            start_ms = round(offset * 1000.0 / rate)
            offset += 1200
            end_ms = round(offset * 1000.0 / rate)
            clauses.append(ClauseResult(
                index=index, text=f"第{index + 1}句。", voice_id="mambo:default",
                wav_path=wav, duration_ms=end_ms - start_ms,
                start_ms=start_ms, end_ms=end_ms, from_cache=False,
            ))
        srt_path = ""
        if kwargs.get("export_srt"):
            srt_path = os.path.splitext(save_path)[0] + ".srt"
            with open(srt_path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(
                    "1\n00:00:00,000 --> 00:00:00,050\n第一句。\n\n"
                    "2\n00:00:00,050 --> 00:00:00,100\n第二句。\n\n"
                )
        return SynthesisJobResult(
            success=True, message="合成完成", wav_path=save_path,
            srt_path=srt_path, duration_sec=4800 / 24000.0, clauses=clauses,
        )


@contextlib.contextmanager
def _patched_cli_clients(gsv_client=None, voice_repo=None, supervisor=None):
    repo = voice_repo if voice_repo is not None else FakeVoiceRepo()
    client = gsv_client if gsv_client is not None else FakeGSVClient()
    sup = supervisor if supervisor is not None else FakeSupervisor()
    with mock.patch("mambotts.cli._make_clients", return_value=(repo, client, sup)):
        yield repo, client, sup


class CliTestCase(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        StubPipeline.instances = []
        StubPipeline.clause_dirs = []

    def tearDown(self) -> None:
        for clause_dir in StubPipeline.clause_dirs:
            shutil.rmtree(clause_dir, ignore_errors=True)
        StubPipeline.clause_dirs = []
        StubPipeline.instances = []
        super().tearDown()

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        from mambotts.cli import run_cli

        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = run_cli(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_synth_json_success(self):
        save = os.path.join(self.tmp, "single.wav")
        with _patched_cli_clients(), \
                mock.patch("mambotts.cli.SynthesisPipeline", StubPipeline):
            code, stdout, _stderr = self._run(
                ["synth", "大家好，我是曼波。", "-o", save, "--json"],
            )
        self.assertEqual(code, 0, stdout)
        payload = json.loads(stdout)
        self.assertEqual(
            set(payload.keys()),
            {"success", "message", "wav_path", "srt_path", "duration_sec", "clauses"},
        )
        self.assertTrue(payload["success"])
        self.assertTrue(os.path.isfile(payload["wav_path"]))
        self.assertEqual(payload["srt_path"], "")
        self.assertEqual(len(payload["clauses"]), 2)
        self.assertEqual(
            set(payload["clauses"][0].keys()),
            {"index", "text", "voice_id", "wav_path", "duration_ms",
             "start_ms", "end_ms", "from_cache"},
        )
        job = StubPipeline.instances[-1].run_job_calls[-1]
        self.assertFalse(job["export_srt"])

    def test_synth_passes_flags_through(self):
        save = os.path.join(self.tmp, "flags.wav")
        with _patched_cli_clients(), \
                mock.patch("mambotts.cli.SynthesisPipeline", StubPipeline):
            code, _stdout, _stderr = self._run(
                ["synth", "内容。", "-o", save, "--srt",
                 "--speed", "1.3",
                 "--no-trim", "--no-normalize", "--no-cache"],
            )
        self.assertEqual(code, 0)
        job = StubPipeline.instances[-1].run_job_calls[-1]
        self.assertAlmostEqual(job["speed"], 1.3)
        self.assertTrue(job["export_srt"])
        self.assertFalse(job["trim_silence"])
        self.assertFalse(job["normalize_audio"])
        self.assertFalse(job["use_cache"])

    def test_synth_engine_offline_exit_3(self):
        offline = FakeGSVClient(online=False)
        with _patched_cli_clients(gsv_client=offline):
            code, stdout, _stderr = self._run(
                ["synth", "内容。", "-o", os.path.join(self.tmp, "x.wav"), "--json"],
            )
        self.assertEqual(code, 3)
        payload = json.loads(stdout)
        self.assertFalse(payload["success"])
        self.assertIn("引擎", payload["message"])

    def test_batch_writes_audio_srt_clauses_and_manifest(self):
        source = os.path.join(self.tmp, "ep01.txt")
        with open(source, "w", encoding="utf-8") as handle:
            handle.write("第一句内容。\n第二句内容。\n")
        out_dir = os.path.join(self.tmp, "output")
        manifest_path = os.path.join(out_dir, "manifest.json")

        with _patched_cli_clients(), \
                mock.patch("mambotts.cli.SynthesisPipeline", StubPipeline):
            code, stdout, _stderr = self._run(
                ["batch", source, "--out-dir", out_dir, "--srt",
                 "--manifest", manifest_path, "--json"],
            )
        self.assertEqual(code, 0, stdout)
        manifest = json.loads(stdout)

        self.assertEqual(
            set(manifest.keys()),
            {"source_file", "wav_path", "srt_path", "duration_sec", "clauses"},
        )
        self.assertEqual(manifest["source_file"], os.path.abspath(source))
        self.assertTrue(os.path.isfile(manifest["wav_path"]))
        self.assertTrue(os.path.isfile(os.path.join(out_dir, "ep01.wav")))
        self.assertTrue(os.path.isfile(os.path.join(out_dir, "ep01.srt")))
        self.assertEqual(manifest["srt_path"], os.path.join(out_dir, "ep01.srt"))

        self.assertEqual(len(manifest["clauses"]), 2)
        for index, entry in enumerate(manifest["clauses"]):
            self.assertEqual(
                set(entry.keys()),
                {"index", "text", "wav_path", "sha256",
                 "duration_sec", "start_ms", "end_ms"},
            )
            self.assertEqual(entry["index"], index)
            expected_target = os.path.join(out_dir, f"ep01_{index + 1:03d}.wav")
            self.assertEqual(entry["wav_path"], expected_target)
            self.assertTrue(os.path.isfile(expected_target))
            with open(expected_target, "rb") as handle:
                self.assertEqual(
                    entry["sha256"],
                    hashlib.sha256(handle.read()).hexdigest(),
                )
        self.assertTrue(os.path.isfile(manifest_path))
        with open(manifest_path, "r", encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), manifest)

    def test_batch_manifest_defaults_and_output_dir_default(self):
        source = os.path.join(self.tmp, "ep02.txt")
        with open(source, "w", encoding="utf-8") as handle:
            handle.write("内容。\n")
        with _patched_cli_clients(), \
                mock.patch("mambotts.cli.SynthesisPipeline", StubPipeline):
            code, _stdout, _stderr = self._run(["batch", source, "--srt"])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "ep02.wav")))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "manifest.json")))

    def test_batch_missing_input_exit_1(self):
        with _patched_cli_clients():
            code, _stdout, stderr = self._run(
                ["batch", os.path.join(self.tmp, "nope.txt"), "--out-dir", self.tmp],
            )
        self.assertEqual(code, 1)
        self.assertIn("不存在", stderr)

    def test_batch_auto_start_engine(self):
        source = os.path.join(self.tmp, "ep03.txt")
        with open(source, "w", encoding="utf-8") as handle:
            handle.write("内容。\n")
        offline = FakeGSVClient(online=False)
        started_with: dict = {}

        class StartableSupervisor:
            """Fake EngineSupervisor whose start() brings the engine online."""

            def __init__(self, *args, **kwargs) -> None:
                pass

            def start(self, api_url=None, bind_job_object=True):
                started_with.update(api_url=api_url, bind_job_object=bind_job_object)
                offline.online = True  # the "engine" comes up
                return (True, "fake engine started")

        with _patched_cli_clients(gsv_client=offline, supervisor=FakeSupervisor()), \
                mock.patch("mambotts.cli.SynthesisPipeline", StubPipeline), \
                mock.patch("mambotts.infra.engine_mgr.EngineSupervisor",
                           StartableSupervisor):
            code, _stdout, _stderr = self._run(
                ["batch", source, "--out-dir", self.tmp, "--auto-start-engine"],
            )
        self.assertEqual(code, 0)
        self.assertEqual(started_with["bind_job_object"], False)
        self.assertTrue(offline.online)  # fake start flipped the engine online

    def test_usage_errors_exit_2(self):
        with _patched_cli_clients():
            for argv in (
                ["definitely-not-a-command"],
                ["synth", "text-without-out"],
                ["voices"],  # removed subcommand -> usage error
                ["engine"],
            ):
                with self.subTest(argv=argv):
                    code, _stdout, stderr = self._run(argv)
                    self.assertEqual(code, 2, stderr)

    @unittest.skipUnless(HAVE_ENGINE_MGR, "mambotts.infra.engine_mgr unavailable")
    def test_engine_status_json_offline_exit_3(self):
        with _patched_cli_clients(gsv_client=FakeGSVClient(online=False)), \
                mock.patch("mambotts.infra.engine_mgr.is_engine_installed",
                           return_value=True):
            code, stdout, _stderr = self._run(["engine", "status", "--json"])
        self.assertEqual(code, 3)
        payload = json.loads(stdout)
        self.assertEqual(
            set(payload.keys()), {"installed", "running", "api_url"},
        )
        self.assertTrue(payload["installed"])
        self.assertFalse(payload["running"])
        self.assertEqual(payload["api_url"], "http://127.0.0.1:9880")

    @unittest.skipUnless(HAVE_ENGINE_MGR, "mambotts.infra.engine_mgr unavailable")
    def test_engine_status_json_online_exit_0(self):
        with _patched_cli_clients(gsv_client=FakeGSVClient(online=True)), \
                mock.patch("mambotts.infra.engine_mgr.is_engine_installed",
                           return_value=False):
            code, stdout, _stderr = self._run(["engine", "status", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertTrue(payload["running"])
        self.assertFalse(payload["installed"])

    def test_no_pyside6_imports_in_cli_and_pipeline_sources(self):
        targets = [
            os.path.join(_REPO_ROOT, "mambotts", "cli.py"),
            os.path.join(_REPO_ROOT, "mambotts", "__main__.py"),
            os.path.join(_REPO_ROOT, "mambotts", "pipeline", "dsp_srt.py"),
            os.path.join(_REPO_ROOT, "mambotts", "pipeline", "scheduler.py"),
        ]
        for path in targets:
            with open(path, "r", encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertFalse(alias.name.startswith("PySide6"), path)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    self.assertFalse(node.module.startswith("PySide6"), path)

    def test_cli_import_never_brings_in_pyside6(self):
        code = (
            "import sys\n"
            f"sys.path.insert(0, r'{_REPO_ROOT}')\n"
            "import mambotts.cli\n"
            "import mambotts.__main__\n"
            "import mambotts.pipeline.scheduler\n"
            "assert 'PySide6' not in sys.modules, 'PySide6 got imported'\n"
            "print('ok')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=180, cwd=_REPO_ROOT,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ok", proc.stdout)


# ---------------------------------------------------------------------------
# __main__ routing
# ---------------------------------------------------------------------------

class TestMainEntrypoint(unittest.TestCase):
    def test_routes_to_cli(self):
        from mambotts import __main__ as entry

        with mock.patch("mambotts.cli.run_cli", return_value=7) as fake_cli:
            code = entry.main(["voices", "list"])
        self.assertEqual(code, 7)
        fake_cli.assert_called_once_with(["voices", "list"])

    def test_empty_and_gui_argv_route_to_gui(self):
        from mambotts import __main__ as entry

        # The PySide6 studio (mambotts.ui.app_shell) is a Phase-4 deliverable;
        # inject a stand-in module so this routing test runs before that.
        gui_calls: list[list[str]] = []
        fake_shell = types.ModuleType("mambotts.ui.app_shell")
        fake_shell.run_gui = lambda args: gui_calls.append(list(args)) or 5
        fake_ui = types.ModuleType("mambotts.ui")
        fake_ui.__path__ = []
        with mock.patch.dict(sys.modules, {
            "mambotts.ui": fake_ui,
            "mambotts.ui.app_shell": fake_shell,
        }):
            with mock.patch("mambotts.cli.run_cli") as fake_cli:
                self.assertEqual(entry.main([]), 5)
                self.assertEqual(entry.main(["--gui"]), 5)
        self.assertEqual(gui_calls, [[], ["--gui"]])
        fake_cli.assert_not_called()


class TestDspSrtAndCliValidation(unittest.TestCase):
    def test_dsp_srt_splits_sub_chunks_by_03s_zero_wav_valley(self):
        import re
        import wave

        sr = 32000
        # 0.5s tone + 0.3s pure zero_wav silence + 0.4s tone + trailing 0.3s zero_wav
        tone1 = [int(12000 * math.sin(2 * math.pi * 440 * i / sr)) for i in range(int(0.5 * sr))]
        zero = [0] * int(0.3 * sr)
        tone2 = [int(12000 * math.sin(2 * math.pi * 550 * i / sr)) for i in range(int(0.4 * sr))]
        pcm = array.array("h", tone1 + zero + tone2 + zero)
        with tempfile.TemporaryDirectory(prefix="mambotts_srt_") as tmp:
            wav_path = os.path.join(tmp, "chunk.wav")
            with wave.open(wav_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sr)
                wf.writeframes(pcm.tobytes())

            spec = SegmentSpec(
                index=0,
                text="第一句，第二句。",
                voice_id="mambo:default",
                speed=1.0,
            )
            srt_path = os.path.join(tmp, "final.srt")
            dur, results = dsp_srt.assemble_clauses_and_srt(
                [(spec, wav_path, False)],
                out_wav_path=os.path.join(tmp, "final.wav"),
                out_srt_path=srt_path,
                trim_silence=False,
                normalize=False,
            )
            self.assertGreater(dur, 0.0)
            self.assertEqual(len(results), 1)
            with open(srt_path, "r", encoding="utf-8") as fh:
                srt_body = fh.read()
            blocks = [b.strip() for b in re.split(r"\r?\n\r?\n", srt_body.strip()) if b.strip()]
            self.assertEqual(len(blocks), 2)
            self.assertIn("第一句", blocks[0])
            self.assertIn("第二句", blocks[1])

            # Valley scarcity aligns best-match + proportional split cleanly without 1-cue collapse.
            spec_three = SegmentSpec(
                index=0,
                text="一，二，三。",
                voice_id="mambo:default",
                speed=1.0,
            )
            fb_srt = os.path.join(tmp, "fallback.srt")
            _dur_fb, res_fb = dsp_srt.assemble_clauses_and_srt(
                [(spec_three, wav_path, False)],
                out_wav_path=os.path.join(tmp, "fallback.wav"),
                out_srt_path=fb_srt,
                trim_silence=False,
                normalize=False,
            )
            self.assertEqual(len(res_fb), 1)
            with open(fb_srt, "r", encoding="utf-8") as fh:
                fb_body = fh.read()
            fb_blocks = [b.strip() for b in re.split(r"\r?\n\r?\n", fb_body.strip()) if b.strip()]
            self.assertEqual(len(fb_blocks), 3)
            self.assertIn("一", fb_blocks[0])
            self.assertIn("二", fb_blocks[1])
            self.assertIn("三", fb_blocks[2])

    def test_cli_offline_validation_and_api_url_propagation(self):
        from mambotts import cli

        with tempfile.TemporaryDirectory(prefix="mambotts_cli_val_") as tmp:
            bad_invocations = [
                ["synth", "你好", "-o", os.path.join(tmp, "out.mp3")],
                ["synth", "你好", "-o", os.path.join(tmp, "noext")],
                ["synth", "你好", "-o", os.path.join(tmp, ".wav")],
                ["synth", "你好", "-o", os.path.join(tmp, "ok.wav"), "--speed", "5.0"],
                ["synth", "你好", "-o", os.path.join(tmp, "ok.wav"), "--speed", "nan"],
                ["synth", "你好", "-o", os.path.join(tmp, "ok.wav"), "--api-url", "ftp://127.0.0.1:9880"],
            ]
            with mock.patch("mambotts.cli._make_clients") as make_cli:
                for argv in bad_invocations:
                    buf_err = io.StringIO()
                    with contextlib.redirect_stderr(buf_err):
                        code = cli.run_cli(argv)
                    self.assertEqual(code, cli.EXIT_USAGE, f"argv={argv} stderr={buf_err.getvalue()}")
                make_cli.assert_not_called()

            script_path = os.path.join(tmp, "in.txt")
            with open(script_path, "w", encoding="utf-8") as fh:
                fh.write("第一行")
            bad_out_dir = os.path.join(tmp, "should_not_exist")
            with mock.patch("mambotts.cli._make_clients") as make_cli:
                buf_err = io.StringIO()
                with contextlib.redirect_stderr(buf_err):
                    code = cli.run_cli(["batch", script_path, "--out-dir", bad_out_dir, "--speed", "0.05"])
                self.assertEqual(code, cli.EXIT_USAGE)
                make_cli.assert_not_called()
            self.assertFalse(os.path.exists(bad_out_dir))

            fake_client = mock.Mock()
            fake_client.api_url = "http://127.0.0.1:9999"
            fake_client.is_api_running.return_value = False
            fake_sup = mock.Mock()
            fake_sup.start.return_value = (False, "fail")
            with mock.patch("mambotts.infra.engine_mgr.EngineSupervisor", return_value=fake_sup):
                ok = cli._ensure_engine_online(
                    fake_client, auto_start=True, wait_seconds=0.0, api_url="http://127.0.0.1:9999"
                )
            self.assertFalse(ok)
            fake_sup.start.assert_called_once_with(
                api_url="http://127.0.0.1:9999", bind_job_object=False
            )


class TestPipelineAndCliAuditRegressions(unittest.TestCase):
    """Regressions from the Goldfish audit across dsp_srt, scheduler, cli."""

    def test_48khz_200ms_zero_wav_srt_split_and_consecutive_punc(self):
        import struct
        import wave
        from mambotts.pipeline.dsp_srt import (
            _clause_srt_cues,
            _split_text_like_api,
            assemble_clauses_and_srt,
        )

        self.assertEqual(_split_text_like_api("太棒了！！下一步。"), ["太棒了！！", "下一步。"])

        sr = 48000
        tone = [12000 if (i % 40) < 20 else -12000 for i in range(sr)]
        gap = [0] * int(sr * 0.20)  # 9600 samples = 200ms matching upstream v4/v5 zero_wav
        samples = tone + gap + tone
        arr = array.array("h", samples)
        cues = _clause_srt_cues("你好？！世界。", arr, sr, 0, 0, round(len(samples) * 1000 / sr))
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0][2], "你好？！")
        self.assertEqual(cues[1][2], "世界。")
        self.assertAlmostEqual(cues[0][1] / 1000.0, 1.1, delta=0.12)
        self.assertAlmostEqual(cues[1][0] / 1000.0, 1.1, delta=0.12)

        with tempfile.TemporaryDirectory(prefix="mambo_srt48_") as tmp:
            wav_path = os.path.join(tmp, "v5_48k.wav")
            out_wav = os.path.join(tmp, "out.wav")
            out_srt = os.path.join(tmp, "out.srt")
            with wave.open(wav_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sr)
                wf.writeframes(struct.pack(f"<{len(samples)}h", *samples))
            assemble_clauses_and_srt(
                [(SegmentSpec(index=0, text="你好？！世界。"), wav_path, False)],
                out_wav_path=out_wav,
                out_srt_path=out_srt,
                trim_silence=False,
                normalize=False,
            )
            with open(out_srt, "r", encoding="utf-8") as fh:
                srt_txt = fh.read()
            self.assertIn("你好？！", srt_txt)
            self.assertIn("世界。", srt_txt)

    def test_scheduler_speed_clamping_nan_and_bounds(self):
        from mambotts.pipeline.scheduler import _clamp_speed

        self.assertEqual(_clamp_speed(float("nan")), 1.0)
        self.assertEqual(_clamp_speed(float("inf")), 1.0)
        self.assertEqual(_clamp_speed(0.0), SPEED_MIN)
        self.assertEqual(_clamp_speed(99.0), SPEED_MAX)

    def test_scheduler_cancel_priority_and_cache_pruning(self):
        from mambotts.pipeline.scheduler import SegmentCache, SynthesisPipeline

        with tempfile.TemporaryDirectory(prefix="mambo_sched_audit_") as tmp:
            # 1) Cache prune keeps newest <= max_entries=2 across distinct jobs.
            voice_repo = FakeVoiceRepo()
            cache = SegmentCache(os.path.join(tmp, "segcache"), max_entries=2)
            client = FakeGSVClient(sample_rate=32000)

            pipe = SynthesisPipeline(voice_repo, client, cache=cache)
            for idx in range(4):
                out_wav = os.path.join(tmp, f"job_{idx}.wav")
                res = pipe.run_job(
                    text=f"第{idx + 1}句。",
                    save_path=out_wav,
                    trim_silence=False,
                    normalize_audio=False,
                    use_cache=True,
                )
                self.assertTrue(res.success, res.message)
                self.assertTrue(os.path.isfile(out_wav))
            cached_wavs = [f for f in os.listdir(cache.cache_dir) if f.endswith(".wav")]
            self.assertLessEqual(len(cached_wavs), 2)

            # 2) Cancel event set simultaneously with transport error must report cancelled, not failure.
            cancel_evt = threading.Event()
            err_client = mock.Mock()
            err_client.api_url = "http://127.0.0.1:9880"
            err_client.is_api_running.return_value = True
            err_client.ensure_models.return_value = None

            def abort_and_fail(*_args, **_kwargs):
                cancel_evt.set()
                raise RuntimeError("Connection broken during forced restart")

            err_client.synthesize_clause.side_effect = abort_and_fail
            pipe2 = SynthesisPipeline(voice_repo, err_client, cache=cache)
            res_cancel = pipe2.run_job(
                text="取消中断测试。",
                save_path=os.path.join(tmp, "canceled.wav"),
                cancel_event=cancel_evt,
                use_cache=False,
            )
            self.assertFalse(res_cancel.success)
            self.assertIn("取消", res_cancel.message)

    def test_cli_validation_offline_batch_dir_and_engine_stop_api_url(self):
        import io
        import contextlib
        import subprocess
        from mambotts import cli

        with tempfile.TemporaryDirectory(prefix="mambo_cli_audit_") as tmp:
            # Reserved device name and unpronounceable text rejected offline
            for argv in (
                ["synth", "你好", "--out", os.path.join(tmp, "CON.wav")],
                ["synth", "。。。", "--out", os.path.join(tmp, "ok.wav")],
            ):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    rc = cli.run_cli(argv)
                self.assertEqual(rc, cli.EXIT_USAGE, f"argv={argv} err={err.getvalue()}")

            # batch offline must NOT create out_dir
            in_txt = os.path.join(tmp, "script.txt")
            with open(in_txt, "w", encoding="utf-8") as fh:
                fh.write("第一行文本")
            offline_out = os.path.join(tmp, "must_not_create")
            fake_client = mock.Mock()
            fake_client.api_url = "http://127.0.0.1:9880"
            fake_client.is_api_running.return_value = False
            with (
                mock.patch("mambotts.cli._make_clients", return_value=(mock.Mock(), fake_client, mock.Mock())),
                mock.patch("mambotts.cli._ensure_engine_online", return_value=False),
            ):
                rc = cli.run_cli(["batch", in_txt, "--out-dir", offline_out])
            self.assertEqual(rc, cli.EXIT_ENGINE_OFFLINE)
            self.assertFalse(os.path.exists(offline_out))

            # engine stop --api-url filters exact loopback local-column port
            netstat_sample = (
                "  TCP    127.0.0.1:9999         0.0.0.0:0              LISTENING       4444\n"
                "  TCP    127.0.0.1:99990        0.0.0.0:0              LISTENING       5555\n"
                "  TCP    192.168.1.5:50000      127.0.0.1:9999         ESTABLISHED     6666\n"
            )
            fake_netstat = mock.Mock(stdout=netstat_sample.encode("utf-8"))
            with (
                mock.patch("mambotts.cli.sys.platform", "win32"),
                mock.patch("mambotts.cli._pid_is_engine_process", side_effect=lambda pid: pid == 4444),
                mock.patch(
                    "subprocess.run",
                    side_effect=[fake_netstat, mock.Mock(returncode=0)],
                ) as run_mock,
            ):
                rc = cli.run_cli(["engine", "stop", "--api-url", "http://127.0.0.1:9999"])
            self.assertEqual(rc, cli.EXIT_OK)
            taskkill_cmds = [call.args[0] for call in run_mock.call_args_list[1:]]
            self.assertEqual(len(taskkill_cmds), 1)
            self.assertIn("4444", taskkill_cmds[0])
            self.assertEqual(
                run_mock.call_args_list[0].kwargs.get("creationflags", 0),
                getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

            # Non-local URL on engine stop must be rejected without touching local processes.
            with mock.patch("subprocess.run") as run_guard:
                rc_remote = cli.run_cli(["engine", "stop", "--api-url", "http://example.com:9880"])
            self.assertEqual(rc_remote, cli.EXIT_USAGE)
            run_guard.assert_not_called()

            # Listening non-engine PID is skipped without calling taskkill.
            netstat_other = (
                "  TCP    127.0.0.1:9999         0.0.0.0:0              LISTENING       7777\n"
            ).encode("utf-8")
            with (
                mock.patch("mambotts.cli.sys.platform", "win32"),
                mock.patch("mambotts.cli._pid_is_engine_process", return_value=False),
                mock.patch(
                    "subprocess.run",
                    side_effect=[mock.Mock(stdout=netstat_other)],
                ) as run_skip,
            ):
                rc_skip = cli.run_cli(["engine", "stop", "--api-url", "http://127.0.0.1:9999"])
            self.assertEqual(rc_skip, cli.EXIT_OK)
            self.assertEqual(run_skip.call_count, 1)

            # Reserved Windows compound/console names rejected cleanly in synth -o.
            for bad_out in ("CON.foo.wav", "CONIN$.wav"):
                self.assertEqual(
                    cli.run_cli(["synth", "你好", "-o", os.path.join(tmp, bad_out)]),
                    cli.EXIT_USAGE,
                )

            # batch --manifest without explicit path uses <out-dir>/manifest.json (README example).
            batch_in = os.path.join(tmp, "script.txt")
            batch_out = os.path.join(tmp, "batch_out")
            with open(batch_in, "w", encoding="utf-8") as fh:
                fh.write("你好世界。")
            fake_result = mock.Mock(
                success=True,
                wav_path=os.path.join(batch_out, "script.wav"),
                srt_path=os.path.join(batch_out, "script.srt"),
                duration_sec=1.0,
                clauses=[],
                message="ok",
                cached_clauses=0,
                total_clauses=0,
            )
            with (
                mock.patch(
                    "mambotts.cli._make_clients",
                    return_value=(mock.Mock(), mock.Mock(is_api_running=lambda: True), None),
                ),
                mock.patch(
                    "mambotts.cli.SynthesisPipeline.run_job",
                    return_value=fake_result,
                ),
            ):
                rc_batch = cli.run_cli(["batch", batch_in, "--out-dir", batch_out, "--srt", "--manifest"])
            self.assertEqual(rc_batch, cli.EXIT_OK)
            self.assertTrue(os.path.isfile(os.path.join(batch_out, "manifest.json")))

    def test_segment_cache_enforces_max_bytes_limit(self):
        from mambotts.pipeline.scheduler import SegmentCache

        with tempfile.TemporaryDirectory(prefix="mambo_cache_bytes_") as tmp:
            cache = SegmentCache(cache_dir=tmp, max_entries=50, max_bytes=250)
            src = os.path.join(tmp, "src.bin")
            with open(src, "wb") as fh:
                fh.write(b"RIFF" + b"\x00" * 96)  # 100 bytes each
            p1 = cache.put("k1", src)
            os.utime(p1, (1000, 1000))
            p2 = cache.put("k2", src)
            os.utime(p2, (2000, 2000))
            p3 = cache.put("k3", src)
            os.utime(p3, (3000, 3000))
            # 3 * 100B > 250B -> oldest k1 must be evicted while k2 & k3 survive.
            self.assertFalse(os.path.exists(p1))
            self.assertTrue(os.path.exists(p2))
            self.assertTrue(os.path.exists(p3))

    def test_egm_pipeline_and_cli_regressions(self):
        import struct
        import wave
        from mambotts import cli
        from mambotts.domain.models import VoicePreset
        from mambotts.pipeline.dsp_srt import assemble_clauses_and_srt
        from mambotts.pipeline.scheduler import SynthesisPipeline

        with tempfile.TemporaryDirectory(prefix="mambo_egm_cli_") as tmp:
            # 1. Invalid Windows filename chars and punctuation-only batch fail cleanly.
            self.assertEqual(
                cli.run_cli(["synth", "你好", "-o", os.path.join(tmp, "bad<name>.wav")]),
                cli.EXIT_USAGE,
            )
            punc_txt = os.path.join(tmp, "punc.txt")
            with open(punc_txt, "w", encoding="utf-8") as fh:
                fh.write("。。。？！")
            with mock.patch("mambotts.cli._make_clients") as clients_mock:
                rc_punc = cli.run_cli(["batch", punc_txt, "--out-dir", tmp])
            self.assertEqual(rc_punc, cli.EXIT_ERROR)
            clients_mock.assert_not_called()

            # 2. Dead engine subprocess fails fast instead of returning EXIT_OK.
            dead_sup = mock.Mock()
            dead_sup._proc = mock.Mock()
            dead_sup.start.return_value = (True, "started")
            dead_sup.is_running.return_value = False
            dead_client = mock.Mock()
            dead_client.is_api_running.return_value = False
            with (
                mock.patch("mambotts.infra.engine_mgr.EngineSupervisor", return_value=dead_sup),
                mock.patch("mambotts.cli.GSVHttpClient", return_value=dead_client),
            ):
                rc_dead = cli.run_cli(["engine", "start"])
            self.assertEqual(rc_dead, cli.EXIT_ERROR)

            # 3. Parser help no longer claims removed inline tags.
            help_text = cli._build_parser().format_help()
            synth_help = [
                a for a in cli._build_parser()._subparsers._group_actions[0].choices.values()
                if a.prog.endswith("synth")
            ][0].format_help()
            self.assertNotIn("内联标签", help_text + synth_help)

            # 4. fast_path validates cached/clause WAV BEFORE overwriting save_path.
            corrupt_wav = os.path.join(tmp, "corrupt.wav")
            with open(corrupt_wav, "wb") as fh:
                fh.write(b"NOT_A_VALID_WAV")
            target_wav = os.path.join(tmp, "existing_good.wav")
            with open(target_wav, "wb") as fh:
                fh.write(b"KEEP_ME")

            class _Repo:
                def get(self, _vid):
                    return VoicePreset("mambo:default", "mambo", "default", "", "ref", "zh", "", "")

            class _BadWavGSV:
                def synthesize_clause(self, _t, clause_tmp, *_a, **_kw):
                    with open(clause_tmp, "wb") as fh:
                        fh.write(b"BAD")
                    return True, clause_tmp

                def is_api_running(self):
                    return True

            pipe = SynthesisPipeline(_Repo(), _BadWavGSV(), supervisor=None)
            res = pipe.run_job(
                "你好世界。",
                target_wav,
                trim_silence=False,
                normalize_audio=False,
                use_cache=False,
            )
            self.assertFalse(res.success)
            with open(target_wav, "rb") as fh:
                self.assertEqual(fh.read(), b"KEEP_ME")

            # 5. assemble_clauses_and_srt aborts BEFORE touching out_wav_path if out_srt_path fails.
            valid_clause = os.path.join(tmp, "ok_clause.wav")
            with wave.open(valid_clause, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(32000)
                wf.writeframes(struct.pack("<10h", *([100] * 10)))
            out_wav = os.path.join(tmp, "master.wav")
            with open(out_wav, "wb") as fh:
                fh.write(b"ORIG_WAV")
            bad_srt_dir = os.path.join(tmp, "occupied_as_dir")
            os.makedirs(bad_srt_dir, exist_ok=True)
            spec = res.clauses[0] if res.clauses else mock.Mock(index=0, text="你好", voice_id="mambo:default")
            with self.assertRaises(OSError):
                assemble_clauses_and_srt(
                    [(spec, valid_clause, False)],
                    out_wav,
                    out_srt_path=bad_srt_dir,
                )
            with open(out_wav, "rb") as fh:
                self.assertEqual(fh.read(), b"ORIG_WAV")


class TestAuditB1B2B3AndDspRegression(TempDirTestCase):
    @staticmethod
    def _parse_srt_ms(srt_text: str) -> list[tuple[int, int, str]]:
        import re

        cues: list[tuple[int, int, str]] = []
        pattern = re.compile(
            r"^\d+\s*\r?\n"
            r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*"
            r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*\r?\n(.+?)(?=\r?\n\r?\n|\Z)",
            re.MULTILINE | re.DOTALL,
        )
        for m in pattern.finditer(srt_text):
            s_ms = ((int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))) * 1000) + int(m.group(4))
            e_ms = ((int(m.group(5)) * 3600 + int(m.group(6)) * 60 + int(m.group(7))) * 1000) + int(m.group(8))
            cues.append((s_ms, e_ms, m.group(9).strip()))
        return cues

    @staticmethod
    def _tone(sr: int, ms: int, amp: int) -> list[int]:
        n = int(sr * ms / 1000)
        return [amp if (i // 16) % 2 == 0 else -amp for i in range(n)]

    def test_dsp_srt_gain_invariance_blip_merge_and_normalize_before_srt(self):
        sr = 32000

        def make_samples(scale: float) -> array.array:
            amp_hi = int(20000 * scale)
            blip_amp = int(1200 * scale)  # <= 20% peak inside <75ms gap coalesces
            samples = (
                [0] * int(sr * 0.06)
                + self._tone(sr, 260, amp_hi)
                + [0] * int(sr * 0.12)
                + self._tone(sr, 35, blip_amp)
                + [0] * int(sr * 0.12)
                + self._tone(sr, 280, amp_hi)
                + [0] * int(sr * 0.15)
                + self._tone(sr, 260, amp_hi)
                + [0] * int(sr * 0.06)
            )
            return array.array("h", samples)

        with tempfile.TemporaryDirectory(prefix="mambo_b1_gain_") as tmp:
            cues_by_scale: dict[str, list[tuple[int, int, str]]] = {}
            for tag, scale in (("hi", 1.0), ("lo", 0.15)):
                wav_p = os.path.join(tmp, f"in_{tag}.wav")
                out_w = os.path.join(tmp, f"out_{tag}.wav")
                out_s = os.path.join(tmp, f"out_{tag}.srt")
                dsp_srt.write_pcm16_mono(wav_p, sr, make_samples(scale))
                spec = SegmentSpec(index=0, text="句一，句二，句三。", voice_id="mambo:default", speed=1.0)
                dsp_srt.assemble_clauses_and_srt(
                    [(spec, wav_p, False)],
                    out_wav_path=out_w,
                    out_srt_path=out_s,
                    trim_silence=False,
                    normalize=True,
                )
                with open(out_s, "r", encoding="utf-8") as fh:
                    cues = self._parse_srt_ms(fh.read())
                cues_by_scale[tag] = cues
                self.assertEqual([c[2] for c in cues], ["句一，", "句二，", "句三。"])
                for idx, (st, ed, _) in enumerate(cues):
                    self.assertLess(st, ed)
                    if idx > 0:
                        self.assertGreaterEqual(st, cues[idx - 1][1])

            for (s_hi, e_hi, _), (s_lo, e_lo, _) in zip(cues_by_scale["hi"], cues_by_scale["lo"]):
                self.assertLessEqual(abs(s_hi - s_lo), 25)
                self.assertLessEqual(abs(e_hi - e_lo), 25)

    def test_dsp_srt_scarce_and_excess_valleys_never_collapse_to_single_cue(self):
        sr = 32000
        text_33 = "，".join(f"第{i:02d}句" for i in range(1, 33)) + "，第33句。"
        with tempfile.TemporaryDirectory(prefix="mambo_b1_33_") as tmp:
            for mode, num_valleys in (("scarce", 12), ("excess", 42)):
                samples: list[int] = [0] * int(sr * 0.05)
                for idx in range(num_valleys + 1):
                    samples.extend(self._tone(sr, 160, 18000))
                    if idx < num_valleys:
                        samples.extend([0] * int(sr * 0.14))
                samples.extend([0] * int(sr * 0.05))
                wav_p = os.path.join(tmp, f"{mode}.wav")
                out_w = os.path.join(tmp, f"out_{mode}.wav")
                out_s = os.path.join(tmp, f"out_{mode}.srt")
                dsp_srt.write_pcm16_mono(wav_p, sr, array.array("h", samples))
                spec = SegmentSpec(index=0, text=text_33, voice_id="mambo:default", speed=1.0)
                dsp_srt.assemble_clauses_and_srt(
                    [(spec, wav_p, False)],
                    out_wav_path=out_w,
                    out_srt_path=out_s,
                    trim_silence=False,
                    normalize=True,
                )
                with open(out_s, "r", encoding="utf-8") as fh:
                    cues = self._parse_srt_ms(fh.read())
                self.assertEqual(len(cues), 33)
                for i, (st, ed, txt) in enumerate(cues):
                    self.assertTrue(txt)
                    self.assertLess(st, ed)
                    if i > 0:
                        self.assertGreaterEqual(st, cues[i - 1][1])

    def test_assemble_clauses_and_srt_cancel_event_aborts_before_write(self):
        with tempfile.TemporaryDirectory(prefix="mambo_dsp_cancel_") as tmp:
            wav_p = os.path.join(tmp, "clause.wav")
            out_w = os.path.join(tmp, "out.wav")
            dsp_srt.write_pcm16_mono(wav_p, 32000, array.array("h", self._tone(32000, 100, 10000)))
            with open(out_w, "wb") as fh:
                fh.write(b"KEEP_EXISTING")
            cancel_evt = threading.Event()
            cancel_evt.set()
            spec = SegmentSpec(index=0, text="你好。", voice_id="mambo:default", speed=1.0)
            with self.assertRaises(RuntimeError):
                dsp_srt.assemble_clauses_and_srt(
                    [(spec, wav_p, False)],
                    out_wav_path=out_w,
                    cancel_event=cancel_evt,
                )
            with open(out_w, "rb") as fh:
                self.assertEqual(fh.read(), b"KEEP_EXISTING")

    def test_trim_silence_pcm16_short_cushion_keeps_speech_unattenuated(self):
        sr = 24000
        lead_2ms = [80] * 48
        speech = [10000 if i % 2 == 0 else -10000 for i in range(1200)]
        tail_2ms = [80] * 48
        out = dsp_srt.trim_silence_pcm16(array.array("h", lead_2ms + speech + tail_2ms), sr)
        self.assertEqual(len(out), 48 + 1200 + 48)
        self.assertEqual(abs(out[48]), 10000)
        self.assertEqual(abs(out[48 + 1199]), 10000)

    def test_scheduler_safe_segment_splitting_and_no_cache_skips_disk(self):
        from mambotts.pipeline.scheduler import SegmentCache, SynthesisPipeline, _bert_budget_weight

        with tempfile.TemporaryDirectory(prefix="mambo_sched_b2_") as tmp:
            cache_dir = os.path.join(tmp, "cache")
            repo = mock.Mock()
            repo.get.return_value = VoicePreset("mambo:default", "mambo", "default", "", "ref", "zh", "", "")
            seen_texts: list[str] = []

            class _FakeGSV:
                def is_api_running(self):
                    return True

                def synthesize_clause(self, text, save_path, *_a, **_kw):
                    seen_texts.append(text)
                    dsp_srt.write_pcm16_mono(save_path, 32000, array.array("h", [5000] * 3200))
                    return True, save_path

            pipe = SynthesisPipeline(repo, _FakeGSV(), cache=SegmentCache(cache_dir=cache_dir), supervisor=None)

            # 1. Short script stays in 1 single request.
            seen_texts.clear()
            r1 = pipe.run_job("第一句，第二句。", os.path.join(tmp, "short.wav"), use_cache=False)
            self.assertTrue(r1.success, r1.message)
            self.assertEqual(len(seen_texts), 1)
            self.assertEqual([f for f in os.listdir(cache_dir) if f.endswith(".wav")], [])

            # 2. 480-char unpunctuated script splits cleanly into bounded segments <= 180 weight.
            seen_texts.clear()
            long_unpunc = "甲乙丙丁" * 120  # 480 chars
            r2 = pipe.run_job(long_unpunc, os.path.join(tmp, "long_unpunc.wav"), use_cache=False)
            self.assertTrue(r2.success, r2.message)
            self.assertGreaterEqual(len(seen_texts), 4)
            for seg in seen_texts:
                self.assertLessEqual(_bert_budget_weight(seg), 180)
            self.assertEqual("".join(seen_texts), long_unpunc)
            self.assertEqual([f for f in os.listdir(cache_dir) if f.endswith(".wav")], [])

            # 3. 240-digit string (480 weighted units) splits cleanly under BERT/duration limit.
            seen_texts.clear()
            digits_240 = "1234567890" * 24
            r3 = pipe.run_job(digits_240, os.path.join(tmp, "digits.wav"), use_cache=False)
            self.assertTrue(r3.success, r3.message)
            self.assertGreaterEqual(len(seen_texts), 3)
            for seg in seen_texts:
                self.assertLessEqual(_bert_budget_weight(seg), 180)

    def test_cli_json_utf8_reconfigure_and_batch_no_cache_and_engine_cmdline(self):
        import io
        import json
        from mambotts import cli

        # 1. Forced cp936 TextIOWrapper reconfigures to UTF-8 so --json stays valid UTF-8.
        raw_out = io.BytesIO()
        raw_err = io.BytesIO()
        cp936_out = io.TextIOWrapper(raw_out, encoding="cp936", errors="strict")
        cp936_err = io.TextIOWrapper(raw_err, encoding="cp936", errors="strict")
        with (
            mock.patch.object(sys, "stdout", cp936_out),
            mock.patch.object(sys, "stderr", cp936_err),
            mock.patch("mambotts.infra.engine_mgr.is_engine_installed", return_value=True),
            mock.patch("mambotts.cli.GSVHttpClient") as gsv_cls,
        ):
            gsv_cls.return_value.is_api_running.return_value = True
            rc = cli.run_cli(["engine", "status", "--json"])
            cp936_out.flush()
            cp936_err.flush()
        self.assertEqual(rc, cli.EXIT_OK)
        parsed = json.loads(raw_out.getvalue().decode("utf-8"))
        self.assertTrue(parsed["running"])

        # Also verify non-ASCII UTF-8 JSON emission succeeds cleanly on cp936 stdout.
        raw_out2 = io.BytesIO()
        cp936_out2 = io.TextIOWrapper(raw_out2, encoding="cp936", errors="strict")
        with (
            mock.patch.object(sys, "stdout", cp936_out2),
            mock.patch("mambotts.cli.GSVHttpClient") as gsv_cls2,
            mock.patch("mambotts.cli.SynthesisPipeline") as pipe_cls2,
        ):
            gsv_cls2.return_value.is_api_running.return_value = True
            pipe_cls2.return_value.run_job.return_value = SynthesisJobResult(True, "合成成功：中文路径测试", wav_path="C:\\测试\\out.wav")
            rc2 = cli.run_cli(["synth", "你好中文。", "-o", "C:\\out.wav", "--json"])
            cp936_out2.flush()
        self.assertEqual(rc2, cli.EXIT_OK)
        parsed2 = json.loads(raw_out2.getvalue().decode("utf-8"))
        self.assertEqual(parsed2["message"], "合成成功：中文路径测试")

        # 2. batch --no-cache exports <stem>.wav and sliced <stem>_001.wav / <stem>_002.wav cleanly.
        with tempfile.TemporaryDirectory(prefix="mambo_batch_nocache_") as tmp:
            txt_path = os.path.join(tmp, "demo.txt")
            with open(txt_path, "w", encoding="utf-8") as fh:
                fh.write("甲乙丙丁。" * 45)  # 225 chars -> 2 safe segments with _001 & _002
            out_dir = os.path.join(tmp, "out")
            os.makedirs(out_dir, exist_ok=True)
            cache_dir = os.path.join(tmp, "isolated_cache")
            os.makedirs(cache_dir, exist_ok=True)

            class _FakeBatchGSV:
                api_url = "http://127.0.0.1:9880"

                def is_api_running(self):
                    return True

                def synthesize_clause(self, _text, save_path, *_a, **_kw):
                    dsp_srt.write_pcm16_mono(
                        save_path,
                        32000,
                        array.array("h", [12000 if i % 2 == 0 else -12000 for i in range(6400)]),
                    )
                    return True, save_path

            from mambotts.pipeline.scheduler import SegmentCache as _RealSegmentCache

            with mock.patch(
                "mambotts.cli._make_clients",
                return_value=(mock.Mock(get=lambda _v: VoicePreset("mambo:default", "mambo", "default", "", "ref", "zh", "", "")), _FakeBatchGSV(), None),
            ), mock.patch("mambotts.pipeline.scheduler.SegmentCache", lambda *a, **kw: _RealSegmentCache(cache_dir=cache_dir)):
                rc_batch = cli.run_cli(["batch", txt_path, "--out-dir", out_dir, "--no-cache"])
            self.assertEqual(rc_batch, cli.EXIT_OK)
            self.assertTrue(os.path.isfile(os.path.join(out_dir, "demo.wav")))
            self.assertTrue(os.path.isfile(os.path.join(out_dir, "demo_001.wav")))
            self.assertTrue(os.path.isfile(os.path.join(out_dir, "demo_002.wav")))

        # 3. Tight engine cmdline matching rejects myapi.py / webapi.py.
        self.assertTrue(cli._cmdline_is_engine(r'python.exe -u "D:\GPT-SoVITS\api.py" -p 9880'))
        self.assertTrue(cli._cmdline_is_engine("python api_v2.py"))
        self.assertFalse(cli._cmdline_is_engine("python myapi.py"))
        self.assertFalse(cli._cmdline_is_engine("python webapi.py"))

    def test_wav_and_srt_atomic_rollback_on_locked_or_readonly_target(self):
        import stat
        from mambotts import cli

        spec = mock.Mock(index=0, text="你好世界。", voice_id="mambo:default")

        def _make_valid_clause(path: str) -> None:
            dsp_srt.write_pcm16_mono(
                path,
                32000,
                array.array("h", [12000 if i % 2 == 0 else -12000 for i in range(9600)]),
            )

        def _assert_no_temp_files(directory: str) -> None:
            leftovers = [
                name for name in os.listdir(directory)
                if name.endswith(".part") or name.endswith(".bak")
            ]
            self.assertEqual(leftovers, [])

        # 1. Exact P1 repro: existing WAV + read-only & open-handle locked SRT (via CLI & DSP).
        with tempfile.TemporaryDirectory(prefix="mambo_pair_ro_") as tmp:
            clause = os.path.join(tmp, "clause.wav")
            _make_valid_clause(clause)
            pair_wav = os.path.join(tmp, "pair.wav")
            pair_srt = os.path.join(tmp, "pair.srt")
            orig_wav = b"ORIG_WAV_BYTES"
            old_srt = b"OLD SUBTITLE CONTENT\n"
            with open(pair_wav, "wb") as fh:
                fh.write(orig_wav)
            with open(pair_srt, "wb") as fh:
                fh.write(old_srt)

            os.chmod(pair_srt, stat.S_IREAD)
            try:
                with open(pair_srt, "rb"):
                    with self.assertRaises(PermissionError):
                        dsp_srt.assemble_clauses_and_srt(
                            [(spec, clause, False)],
                            pair_wav,
                            out_srt_path=pair_srt,
                        )
                    with open(pair_wav, "rb") as fh:
                        self.assertEqual(fh.read(), orig_wav)
                    with open(pair_srt, "rb") as fh:
                        self.assertEqual(fh.read(), old_srt)
                    _assert_no_temp_files(tmp)

                    # Also verify CLI `synth -o pair.wav --srt --no-cache` returns exit code 1 with pair intact.
                    class _FakePairGSV:
                        api_url = "http://127.0.0.1:9880"

                        def is_api_running(self):
                            return True

                        def synthesize_clause(self, _text, save_path, *_a, **_kw):
                            _make_valid_clause(save_path)
                            return True, save_path

                    cache_dir = os.path.join(tmp, "cache")
                    os.makedirs(cache_dir, exist_ok=True)
                    from mambotts.pipeline.scheduler import SegmentCache as _RealSegmentCache

                    import io

                    out_buf = io.StringIO()
                    err_buf = io.StringIO()
                    with (
                        mock.patch.object(sys, "stdout", out_buf),
                        mock.patch.object(sys, "stderr", err_buf),
                        mock.patch(
                            "mambotts.cli._make_clients",
                            return_value=(
                                mock.Mock(get=lambda _v: VoicePreset("mambo:default", "mambo", "default", "", "ref", "zh", "", "")),
                                _FakePairGSV(),
                                None,
                            ),
                        ),
                        mock.patch(
                            "mambotts.pipeline.scheduler.SegmentCache",
                            lambda *a, **kw: _RealSegmentCache(cache_dir=cache_dir),
                        ),
                    ):
                        rc = cli.run_cli(["synth", "全新合成内容。", "-o", pair_wav, "--srt", "--no-cache"])
                    self.assertEqual(rc, cli.EXIT_ERROR)
                    self.assertIn("目标 SRT 文件不可写", out_buf.getvalue() + err_buf.getvalue())
                    with open(pair_wav, "rb") as fh:
                        self.assertEqual(fh.read(), orig_wav)
                    with open(pair_srt, "rb") as fh:
                        self.assertEqual(fh.read(), old_srt)
                    _assert_no_temp_files(tmp)
            finally:
                os.chmod(pair_srt, stat.S_IWRITE)

        # 2. Writable SRT locked only by an active open handle (late replace failure AFTER WAV replace):
        #    (a) existing WAV must roll back to exact original bytes;
        #    (b) copy-backup fallback (when os.link raises OSError) also rolls back;
        #    (c) brand-new WAV (didn't exist beforehand) must be removed on SRT failure.
        for use_copy_fallback in (False, True):
            with tempfile.TemporaryDirectory(prefix="mambo_pair_lock_") as tmp:
                clause = os.path.join(tmp, "clause.wav")
                _make_valid_clause(clause)
                pair_wav = os.path.join(tmp, "pair.wav")
                pair_srt = os.path.join(tmp, "pair.srt")
                orig_wav = b"ORIG_WAV_DATA"
                old_srt = b"OLD SUBTITLE CONTENT\n"
                with open(pair_wav, "wb") as fh:
                    fh.write(orig_wav)
                with open(pair_srt, "wb") as fh:
                    fh.write(old_srt)

                with open(pair_srt, "rb"):
                    if use_copy_fallback:
                        with mock.patch("mambotts.pipeline.dsp_srt.os.link", side_effect=OSError("no link")):
                            with self.assertRaises(PermissionError):
                                dsp_srt.assemble_clauses_and_srt(
                                    [(spec, clause, False)],
                                    pair_wav,
                                    out_srt_path=pair_srt,
                                )
                    else:
                        with self.assertRaises(PermissionError):
                            dsp_srt.assemble_clauses_and_srt(
                                [(spec, clause, False)],
                                pair_wav,
                                out_srt_path=pair_srt,
                            )
                with open(pair_wav, "rb") as fh:
                    self.assertEqual(fh.read(), orig_wav)
                with open(pair_srt, "rb") as fh:
                    self.assertEqual(fh.read(), old_srt)
                _assert_no_temp_files(tmp)

        with tempfile.TemporaryDirectory(prefix="mambo_pair_newwav_") as tmp:
            clause = os.path.join(tmp, "clause.wav")
            _make_valid_clause(clause)
            new_wav = os.path.join(tmp, "fresh.wav")
            locked_srt = os.path.join(tmp, "fresh.srt")
            old_srt = b"OLD SUBTITLE CONTENT\n"
            with open(locked_srt, "wb") as fh:
                fh.write(old_srt)

            with open(locked_srt, "rb"):
                with self.assertRaises(PermissionError):
                    dsp_srt.assemble_clauses_and_srt(
                        [(spec, clause, False)],
                        new_wav,
                        out_srt_path=locked_srt,
                    )
            self.assertFalse(os.path.exists(new_wav))
            with open(locked_srt, "rb") as fh:
                self.assertEqual(fh.read(), old_srt)
            _assert_no_temp_files(tmp)

        # 3. Symmetry: locked/read-only WAV + unlocked SRT must leave SRT unchanged as well.
        with tempfile.TemporaryDirectory(prefix="mambo_wav_lock_") as tmp:
            clause = os.path.join(tmp, "clause.wav")
            _make_valid_clause(clause)
            pair_wav = os.path.join(tmp, "pair.wav")
            pair_srt = os.path.join(tmp, "pair.srt")
            orig_wav = b"ORIG_WAV_SYMMETRY"
            old_srt = b"OLD SUBTITLE CONTENT\n"
            with open(pair_wav, "wb") as fh:
                fh.write(orig_wav)
            with open(pair_srt, "wb") as fh:
                fh.write(old_srt)

            with open(pair_wav, "rb"):
                with self.assertRaises(PermissionError):
                    dsp_srt.assemble_clauses_and_srt(
                        [(spec, clause, False)],
                        pair_wav,
                        out_srt_path=pair_srt,
                    )
            with open(pair_wav, "rb") as fh:
                self.assertEqual(fh.read(), orig_wav)
            with open(pair_srt, "rb") as fh:
                self.assertEqual(fh.read(), old_srt)
            _assert_no_temp_files(tmp)


if __name__ == "__main__":
    unittest.main()
