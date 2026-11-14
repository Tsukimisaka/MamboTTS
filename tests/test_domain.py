"""Automated unit tests for ``mambotts/domain/`` (spec item 23, Phase 1).

Self-contained: everything runs inside throwaway temp directories and never
touches the shared ``models/`` or ``GPT-SoVITS/`` junctions.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

# Make ``mambotts`` importable regardless of the caller's working directory
# (``python -m unittest discover -s D:\\MamboTTS-v2\\tests`` from anywhere).
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from mambotts.domain.models import (  # noqa: E402
    AppConfig,
    HistoryRecord,
    SegmentSpec,
    VoicePreset,
)
from mambotts.domain.naming import (  # noqa: E402
    DEFAULT_BASENAME,
    ensure_wav_suffix,
    filename_from_text,
    resolve_output_dir,
    sanitize_stem,
    unique_path,
)
from mambotts.domain.storage_repo import (  # noqa: E402
    ConfigRepository,
    HistoryRepository,
    atomic_write_json,
    remove_quiet,
    replace_with_retry,
)
from mambotts.domain.voice_repo import VoiceRepository  # noqa: E402


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="mambotts_domain_test_")
        self.addCleanup(shutil.rmtree, self._tmp, True)

    def tmp_path(self, *parts: str) -> str:
        return os.path.join(self._tmp, *parts)


# ---------------------------------------------------------------------------
# naming.sanitize_stem
# ---------------------------------------------------------------------------

class TestSanitizeStem(TempDirTestCase):
    def test_reserved_device_names_get_prefixed(self):
        self.assertEqual(sanitize_stem("con"), "_con")
        self.assertEqual(sanitize_stem("NUL.txt"), "_NUL.txt")
        self.assertEqual(sanitize_stem("com1"), "_com1")
        self.assertEqual(sanitize_stem("LPT9.log"), "_LPT9.log")

    def test_control_chars_removed(self):
        self.assertEqual(sanitize_stem("a\x00b\x07c\x1f"), "abc")
        self.assertEqual(sanitize_stem("line\nbreak\ttab"), "linebreaktab")

    def test_forbidden_chars_removed(self):
        self.assertEqual(sanitize_stem('a<b>c:d"e/f\\g|h?i*j'), "abcdefghij")

    def test_strips_leading_trailing_dots_and_spaces(self):
        self.assertEqual(sanitize_stem("  name. "), "name")
        self.assertEqual(sanitize_stem("...name..."), "name")

    def test_empty_falls_back_to_default_basename(self):
        self.assertEqual(sanitize_stem(""), DEFAULT_BASENAME)
        self.assertEqual(sanitize_stem("   "), DEFAULT_BASENAME)
        self.assertEqual(sanitize_stem("///"), DEFAULT_BASENAME)

    def test_normal_name_unchanged(self):
        self.assertEqual(sanitize_stem("第01话-重逢"), "第01话-重逢")


# ---------------------------------------------------------------------------
# naming.filename_from_text
# ---------------------------------------------------------------------------

class TestFilenameFromText(TempDirTestCase):
    def test_preserves_literal_brackets_and_strips_spaces(self):
        self.assertEqual(filename_from_text("  [Ctrl+C] 保留原样  "), "[Ctrl+C] 保留原样.wav")
        self.assertEqual(filename_from_text("*Ctrl+C* 保留"), "Ctrl+C 保留.wav")

    def test_slices_to_max_prefix(self):
        long_text = "一二三四五六七八九十" * 4  # 40 chars
        expected = "一二三四五六七八九十" * 2  # first 20 chars
        self.assertEqual(filename_from_text(long_text), f"{expected}.wav")
        self.assertEqual(filename_from_text(long_text, max_prefix=5), "一二三四五.wav")

    def test_empty_or_sanitized_only_falls_back(self):
        self.assertEqual(filename_from_text("   "), f"{DEFAULT_BASENAME}.wav")
        self.assertEqual(filename_from_text("???***"), f"{DEFAULT_BASENAME}.wav")
        self.assertEqual(filename_from_text(""), f"{DEFAULT_BASENAME}.wav")


# ---------------------------------------------------------------------------
# naming.ensure_wav_suffix / unique_path / resolve_output_dir
# ---------------------------------------------------------------------------

class TestEnsureWavSuffix(TempDirTestCase):
    def test_replaces_other_audio_extensions(self):
        self.assertEqual(ensure_wav_suffix("song.mp3"), "song.wav")
        self.assertEqual(ensure_wav_suffix("SONG.FLAC"), "SONG.wav")
        self.assertEqual(ensure_wav_suffix("tune.ogg"), "tune.wav")
        self.assertEqual(ensure_wav_suffix("voice.M4A"), "voice.wav")

    def test_appends_wav_when_missing(self):
        self.assertEqual(ensure_wav_suffix("noext"), "noext.wav")
        self.assertEqual(ensure_wav_suffix(""), ".wav")

    def test_keeps_existing_wav(self):
        self.assertEqual(ensure_wav_suffix("already.wav"), "already.wav")
        self.assertEqual(ensure_wav_suffix("ALREADY.WAV"), "ALREADY.WAV")


class TestUniquePath(TempDirTestCase):
    def test_free_path_returned_unchanged(self):
        path = self.tmp_path("ep.wav")
        self.assertEqual(unique_path(path), path)

    def test_appends_counter_before_extension(self):
        first = self.tmp_path("ep.wav")
        with open(first, "wb"):
            pass
        self.assertEqual(unique_path(first), self.tmp_path("ep_1.wav"))
        with open(self.tmp_path("ep_1.wav"), "wb"):
            pass
        self.assertEqual(unique_path(first), self.tmp_path("ep_2.wav"))

    def test_extensionless_path(self):
        base = self.tmp_path("bundle")
        with open(base, "wb"):
            pass
        self.assertEqual(unique_path(base), self.tmp_path("bundle_1"))


class TestResolveOutputDir(TempDirTestCase):
    def test_prefers_dirname_of_input_path(self):
        out_dir = self.tmp_path("outdir")
        os.makedirs(out_dir)
        self.assertEqual(resolve_output_dir(os.path.join(out_dir, "ep01.wav"), ""), out_dir)

    def test_falls_back_to_saved_dir(self):
        saved = self.tmp_path("saved")
        os.makedirs(saved)
        missing = os.path.join(self._tmp, "no_such_dir")
        self.assertEqual(resolve_output_dir(os.path.join(missing, "a.wav"), saved), saved)
        self.assertEqual(resolve_output_dir("", saved), saved)

    def test_terminal_fallback_is_desktop(self):
        from unittest import mock
        from mambotts.domain.naming import _known_desktop

        self.assertEqual(resolve_output_dir("", ""), _known_desktop())
        with mock.patch("mambotts.domain.naming._known_desktop", return_value=r"D:\OneDrive\Desktop"):
            self.assertEqual(resolve_output_dir("", ""), r"D:\OneDrive\Desktop")


# ---------------------------------------------------------------------------
# storage_repo.atomic_write_json
# ---------------------------------------------------------------------------

class TestAtomicWriteJson(TempDirTestCase):
    def test_writes_utf8_json_with_unicode(self):
        target = self.tmp_path("data.json")
        atomic_write_json(target, {"曼波": "你好", "items": [1, 2, 3]})
        with open(target, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["曼波"], "你好")
        self.assertEqual(data["items"], [1, 2, 3])

    def test_replaces_existing_file_and_leaves_no_tmp(self):
        target = self.tmp_path("data.json")
        atomic_write_json(target, {"n": 1})
        atomic_write_json(target, {"n": 2}, indent=None)
        with open(target, "r", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"n": 2})
        leftovers = [name for name in os.listdir(self._tmp) if ".tmp" in name]
        self.assertEqual(leftovers, [])

    def test_writes_into_new_subdirectory_parents(self):
        target = self.tmp_path("nested", "data.json")
        os.makedirs(os.path.dirname(target))
        atomic_write_json(target, [])
        self.assertTrue(os.path.isfile(target))


# ---------------------------------------------------------------------------
# storage_repo.ConfigRepository
# ---------------------------------------------------------------------------

class TestConfigRepository(TempDirTestCase):
    def test_unknown_field_preservation_via_extra(self):
        cfg_path = self.tmp_path("config.json")
        cfg = AppConfig()
        cfg.speed = 1.5
        cfg.extra["custom_key"] = "custom_value"
        cfg.extra["nested"] = {"a": 1}
        ConfigRepository.save(cfg_path, cfg)

        with open(cfg_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertEqual(raw["custom_key"], "custom_value")
        self.assertEqual(raw["nested"], {"a": 1})

        loaded = ConfigRepository.load(cfg_path)
        self.assertEqual(loaded.extra.get("custom_key"), "custom_value")
        self.assertEqual(loaded.extra.get("nested"), {"a": 1})
        self.assertAlmostEqual(loaded.speed, 1.5)
        self.assertEqual(loaded.schema_version, 2)

    def test_save_then_load_round_trip(self):
        cfg_path = self.tmp_path("config.json")
        cfg = AppConfig(
            api_url="http://127.0.0.1:9880",
            speed=1.25,
            output_dir=self._tmp,
            name="阿尼",
            export_srt=False,
            trim_silence=False,
            normalize_audio=False,
            minimize_to_tray=True,
        )
        ConfigRepository.save(cfg_path, cfg)
        loaded = ConfigRepository.load(cfg_path)
        self.assertEqual(loaded, cfg)

    def test_clamps_out_of_range_values(self):
        cfg_path = self.tmp_path("config.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump({"speed": 99.0, "pause_ms": 9999, "theme": "solarized"}, fh)
        cfg = ConfigRepository.load(cfg_path)
        self.assertAlmostEqual(cfg.speed, 3.0)
        self.assertFalse(hasattr(cfg, "theme"))
        self.assertFalse(hasattr(cfg, "pause_ms"))

        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump({"speed": -5, "pause_ms": -100, "theme": "dark"}, fh)
        cfg = ConfigRepository.load(cfg_path)
        self.assertAlmostEqual(cfg.speed, 0.1)
        self.assertFalse(hasattr(cfg, "theme"))
        self.assertFalse(hasattr(cfg, "pause_ms"))

    def test_corrupt_json_falls_back_to_defaults(self):
        cfg_path = self.tmp_path("config.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            fh.write("{ this is not valid json !!")
        warnings: list[str] = []
        cfg = ConfigRepository.load(cfg_path, warn_cb=warnings.append)
        self.assertEqual(cfg.schema_version, 2)
        self.assertAlmostEqual(cfg.speed, 1.0)
        self.assertEqual(cfg.api_url, "http://127.0.0.1:9880")
        self.assertTrue(warnings)

    def test_non_object_json_falls_back_to_defaults(self):
        cfg_path = self.tmp_path("config.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump([1, 2, 3], fh)
        warnings: list[str] = []
        cfg = ConfigRepository.load(cfg_path, warn_cb=warnings.append)
        self.assertEqual(cfg, AppConfig())
        self.assertTrue(warnings)

    def test_missing_file_returns_defaults(self):
        cfg = ConfigRepository.load(self.tmp_path("absent.json"))
        self.assertEqual(cfg, AppConfig())

    def test_legacy_output_file_migrated_to_output_dir(self):
        cfg_path = self.tmp_path("config.json")
        legacy_file = self.tmp_path("old_out", "ep01.wav")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump({"output_file": legacy_file}, fh)
        cfg = ConfigRepository.load(cfg_path)
        self.assertEqual(cfg.output_dir, os.path.dirname(legacy_file))

    def test_legacy_output_file_kept_when_output_dir_present(self):
        cfg_path = self.tmp_path("config.json")
        existing_dir = self.tmp_path("current_out")
        os.makedirs(existing_dir)
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump({"output_file": self.tmp_path("old.wav"), "output_dir": existing_dir}, fh)
        cfg = ConfigRepository.load(cfg_path)
        self.assertEqual(cfg.output_dir, existing_dir)

    def test_legacy_key_dropped_on_save(self):
        cfg_path = self.tmp_path("config.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump({"output_file": self.tmp_path("old.wav")}, fh)
        cfg = ConfigRepository.load(cfg_path)
        ConfigRepository.save(cfg_path, cfg)
        with open(cfg_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertNotIn("output_file", raw)

    def test_bool_coercion(self):
        cfg_path = self.tmp_path("config.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "export_srt": 0,
                    "minimize_to_tray": 1,
                    "trim_silence": "false",
                },
                fh,
            )
        cfg = ConfigRepository.load(cfg_path)
        self.assertFalse(cfg.export_srt)
        self.assertTrue(cfg.minimize_to_tray)
        self.assertFalse(cfg.trim_silence)


# ---------------------------------------------------------------------------
# storage_repo.HistoryRepository
# ---------------------------------------------------------------------------

class TestHistoryRepository(TempDirTestCase):
    def _make_record(self, path: str, text: str = "第一集", **overrides) -> HistoryRecord:
        record = HistoryRecord(
            record_id=0,
            path=path,
            text=text,
            time="2026-11-03 10:00:00",
            duration=12.5,
            voice_id="mambo:happy",
            speed=1.2,
        )
        for key, value in overrides.items():
            setattr(record, key, value)
        return record

    def test_save_load_round_trip_with_rel_path(self):
        history_path = self.tmp_path("history.json")
        out_dir = self.tmp_path("out")
        os.makedirs(out_dir)
        wav = os.path.join(out_dir, "ep01.wav")
        with open(wav, "wb"):
            pass

        record = self._make_record(
            wav,
            srt_path=os.path.join(out_dir, "ep01.srt"),
        )
        HistoryRepository.save(history_path, [record], base_output_dir=self._tmp)
        self.assertEqual(record.rel_path, os.path.join("out", "ep01.wav"))

        loaded = HistoryRepository.load(history_path, base_output_dir=self._tmp)
        self.assertEqual(len(loaded), 1)
        rec = loaded[0]
        self.assertEqual(rec.path, wav)
        self.assertEqual(rec.rel_path, os.path.join("out", "ep01.wav"))
        self.assertEqual(rec.text, "第一集")
        self.assertEqual(rec.time, "2026-11-03 10:00:00")
        self.assertAlmostEqual(rec.duration, 12.5)
        self.assertEqual(rec.srt_path, os.path.join(out_dir, "ep01.srt"))
        self.assertEqual(rec.voice_id, "mambo:happy")
        self.assertAlmostEqual(rec.speed, 1.2)
        self.assertEqual(rec.record_id, 0)
        self.assertFalse(rec.missing)

    def test_rel_path_healing_after_move(self):
        history_path = self.tmp_path("history.json")
        base = self.tmp_path("out_base")
        out_dir = os.path.join(base, "out")
        os.makedirs(out_dir)
        wav = os.path.join(out_dir, "a.wav")
        with open(wav, "wb"):
            pass

        # Absolute path points at a stale location; rel_path resolves under base.
        stale = [
            {
                "record_id": 0,
                "path": self.tmp_path("elsewhere", "a.wav"),
                "text": "你好",
                "time": "2026-11-03 12:00:00",
                "duration": 1.5,
                "rel_path": os.path.join("out", "a.wav"),
            }
        ]
        with open(history_path, "w", encoding="utf-8") as fh:
            json.dump(stale, fh)

        records = HistoryRepository.load(history_path, base_output_dir=base)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].path, wav)
        self.assertFalse(records[0].missing)

    def test_missing_flag_and_filtering(self):
        history_path = self.tmp_path("history.json")
        stale = [
            {
                "record_id": 1,
                "path": self.tmp_path("gone.wav"),
                "text": "x",
                "time": "t",
                "duration": 0.0,
                "rel_path": os.path.join("no_such_sub", "x.wav"),
            }
        ]
        with open(history_path, "w", encoding="utf-8") as fh:
            json.dump(stale, fh)

        records = HistoryRepository.load(history_path, base_output_dir=self._tmp)
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0].missing)

        kept = HistoryRepository.load(
            history_path, base_output_dir=self._tmp, include_missing=False
        )
        self.assertEqual(kept, [])

    def test_max_records_cap_on_save_and_load(self):
        history_path = self.tmp_path("history.json")
        records = [
            self._make_record(
                self.tmp_path(f"f{i}.wav"), text=f"t{i}", record_id=i
            )
            for i in range(60)
        ]
        HistoryRepository.save(history_path, records)
        with open(history_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertEqual(len(raw), HistoryRepository.MAX_RECORDS)
        self.assertEqual(raw[0]["text"], "t0")

        loaded = HistoryRepository.load(history_path)
        self.assertEqual(len(loaded), HistoryRepository.MAX_RECORDS)
        self.assertEqual(loaded[0].text, "t0")

    def test_cross_drive_rel_path_guard(self):
        history_path = self.tmp_path("history.json")
        # "Q:" never matches the drive of the temp dir, so rel_path must not
        # be recomputed (os.path.relpath cannot span drives).
        record = HistoryRecord(
            record_id=0,
            path="Q:\\elsewhere\\ep01.wav",
            text="x",
            time="t",
            duration=1.0,
            rel_path="keep.wav",
        )
        HistoryRepository.save(history_path, [record], base_output_dir=self._tmp)
        with open(history_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertEqual(raw[0]["rel_path"], "keep.wav")

        loaded = HistoryRepository.load(history_path, base_output_dir=self._tmp)
        self.assertEqual(loaded[0].rel_path, "keep.wav")
        self.assertTrue(loaded[0].missing)

    def test_absolute_rel_path_never_used_for_healing(self):
        history_path = self.tmp_path("history.json")
        foreign = "Q:\\somewhere\\else\\a.wav"
        stale = [
            {
                "record_id": 0,
                "path": self.tmp_path("gone.wav"),
                "text": "x",
                "time": "t",
                "duration": 0.0,
                "rel_path": foreign,
            }
        ]
        with open(history_path, "w", encoding="utf-8") as fh:
            json.dump(stale, fh)
        records = HistoryRepository.load(history_path, base_output_dir=self._tmp)
        self.assertEqual(records[0].path, self.tmp_path("gone.wav"))
        self.assertTrue(records[0].missing)

    def test_corrupt_or_non_list_history_returns_empty(self):
        history_path = self.tmp_path("history.json")
        with open(history_path, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        self.assertEqual(HistoryRepository.load(history_path), [])
        with open(history_path, "w", encoding="utf-8") as fh:
            json.dump({"not": "a list"}, fh)
        self.assertEqual(HistoryRepository.load(history_path), [])
        self.assertEqual(HistoryRepository.load(self.tmp_path("absent.json")), [])

    def test_non_dict_entries_skipped(self):
        history_path = self.tmp_path("history.json")
        with open(history_path, "w", encoding="utf-8") as fh:
            json.dump(["junk", 42, {"record_id": 2, "path": "p.wav", "text": "ok"}], fh)
        records = HistoryRepository.load(history_path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].text, "ok")


# ---------------------------------------------------------------------------
# domain.models
# ---------------------------------------------------------------------------

class TestSegmentSpecCacheKey(TempDirTestCase):
    def _preset(self, ref_wav: str = "") -> VoicePreset:
        return VoicePreset(
            id="mambo:default",
            character="mambo",
            emotion="default",
            ref_wav=ref_wav,
            ref_text="参考文本",
            gpt_model="gpt.ckpt",
            sovits_model="sovits.pth",
        )

    def test_key_is_24_hex_chars(self):
        wav = self.tmp_path("refer.wav")
        with open(wav, "wb"):
            pass
        key = SegmentSpec(index=0, text="你好", speed=1.2).cache_key(self._preset(wav))
        self.assertEqual(len(key), 24)
        int(key, 16)  # must parse as hex

    def test_key_stable_for_identical_inputs(self):
        wav = self.tmp_path("refer.wav")
        with open(wav, "wb"):
            pass
        preset = self._preset(wav)
        k1 = SegmentSpec(index=0, text="你好", speed=1.2).cache_key(preset)
        k2 = SegmentSpec(index=7, text="你好", speed=1.2).cache_key(preset)
        self.assertEqual(k1, k2)

    def test_key_reflects_ref_wav_mtime(self):
        wav = self.tmp_path("refer.wav")
        with open(wav, "wb"):
            pass
        preset = self._preset(wav)
        spec = SegmentSpec(index=0, text="hi")
        k1 = spec.cache_key(preset)
        os.utime(wav, ns=(1_000_000_000, 1_000_000_000))
        k2 = spec.cache_key(preset)
        self.assertNotEqual(k1, k2)

    def test_key_handles_missing_ref_wav(self):
        preset = self._preset(self.tmp_path("does_not_exist.wav"))
        spec = SegmentSpec(index=0, text="hi")
        key = spec.cache_key(preset)
        self.assertEqual(len(key), 24)
        self.assertEqual(key, spec.cache_key(preset))

    def test_key_distinguishes_speed(self):
        preset = self._preset()
        k1 = SegmentSpec(index=0, text="hi", speed=1.0).cache_key(preset)
        k1b = SegmentSpec(index=0, text="hi", speed=1.00).cache_key(preset)
        k2 = SegmentSpec(index=0, text="hi", speed=1.25).cache_key(preset)
        self.assertEqual(k1, k1b)  # speed formatted to 2 decimals
        self.assertNotEqual(k1, k2)


class TestDataclassRoundTrips(TempDirTestCase):
    def test_appconfig_round_trip(self):
        cfg = AppConfig(speed=2.0, output_dir="D:/out", extra={"k": "v"})
        restored = AppConfig.from_dict(cfg.to_dict())
        self.assertEqual(restored, cfg)

    def test_appconfig_from_dict_tolerates_garbage(self):
        cfg = AppConfig.from_dict(
            {"speed": "abc", "pause_ms": None, "theme": 5, "export_srt": "yes", "junk": 1}
        )
        self.assertAlmostEqual(cfg.speed, 1.0)
        self.assertFalse(hasattr(cfg, "theme"))
        self.assertFalse(hasattr(cfg, "pause_ms"))
        self.assertTrue(cfg.export_srt)
        self.assertEqual(cfg.extra, {"junk": 1})

    def test_appconfig_from_dict_none(self):
        self.assertEqual(AppConfig.from_dict(None), AppConfig())

    def test_history_record_round_trip(self):
        record = HistoryRecord(
            record_id=3,
            path="p.wav",
            text="t",
            time="2026",
            duration=2.5,
            rel_path="r.wav",
            srt_path="p.srt",
            voice_id="mambo:happy",
            speed=0.8,
        )
        self.assertEqual(HistoryRecord.from_dict(record.to_dict()), record)

    def test_history_record_from_dict_defaults(self):
        record = HistoryRecord.from_dict({})
        self.assertEqual(record.record_id, 0)
        self.assertEqual(record.path, "")
        self.assertEqual(record.voice_id, "mambo:default")
        self.assertAlmostEqual(record.speed, 1.0)
        self.assertFalse(record.missing)


# ---------------------------------------------------------------------------
# domain.voice_repo
# ---------------------------------------------------------------------------

class TestVoiceRepository(TempDirTestCase):
    """Single-voice registry: built-in mambo:default, models/refer.txt aware."""

    def test_builtin_default_registered_even_without_dirs(self):
        from mambotts.domain.models import DEFAULT_REF_TEXT

        repo = VoiceRepository(project_dir=self._tmp)
        builtin = repo.get("mambo:default")
        self.assertEqual(builtin.id, "mambo:default")
        self.assertEqual(builtin.character, "mambo")
        self.assertEqual(builtin.emotion, "default")
        self.assertEqual(builtin.ref_text, DEFAULT_REF_TEXT)
        self.assertEqual(
            builtin.gpt_model,
            os.path.abspath(self.tmp_path("models", "manbo-e10.ckpt")),
        )
        self.assertEqual(
            builtin.sovits_model,
            os.path.abspath(self.tmp_path("models", "manbo_e8_s168.pth")),
        )
        self.assertEqual(
            builtin.ref_wav,
            os.path.abspath(self.tmp_path("models", "refer.wav")),
        )

    def test_scan_returns_exactly_one_preset(self):
        repo = VoiceRepository(project_dir=self._tmp)
        presets = repo.scan()
        self.assertEqual(len(presets), 1)
        self.assertEqual(presets[0].id, "mambo:default")

    def test_get_ignores_query_and_returns_builtin(self):
        repo = VoiceRepository(project_dir=self._tmp)
        for query in ("", "no_such_voice", "开心", "xiaoya:happy", "随便什么"):
            self.assertEqual(repo.get(query).id, "mambo:default", query)

    def test_refer_txt_override_refreshed_on_scan(self):
        models = self.tmp_path("models")
        os.makedirs(models, exist_ok=True)
        with open(os.path.join(models, "refer.txt"), "w", encoding="utf-8") as fh:
            fh.write("有些人, 见了面也不知该说些什么")
        repo = VoiceRepository(project_dir=self._tmp)
        self.assertEqual(repo.get().ref_text, "有些人, 见了面也不知该说些什么")
        with open(os.path.join(models, "refer.txt"), "w", encoding="utf-8") as fh:
            fh.write("换一段新的参考文本")
        self.assertEqual(repo.scan()[0].ref_text, "换一段新的参考文本")


class TestDomainMigrationAndHealing(TempDirTestCase):
    def test_legacy_theme_dark_ignored_and_stripped_on_save(self):
        cfg_path = self.tmp_path("config.json")
        raw_input = {"speed": 1.25, "theme": "dark", "custom_flag": True}
        snapshot = dict(raw_input)
        parsed = AppConfig.from_dict(raw_input)
        self.assertEqual(raw_input, snapshot)  # never mutates caller dict
        self.assertFalse(hasattr(parsed, "theme"))
        self.assertNotIn("theme", parsed.extra)
        self.assertNotIn("theme", parsed.to_dict())

        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(raw_input, fh)
        loaded = ConfigRepository.load(cfg_path)
        self.assertNotIn("theme", loaded.extra)
        self.assertEqual(loaded.extra, {"custom_flag": True})
        ConfigRepository.save(cfg_path, loaded)
        with open(cfg_path, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertNotIn("theme", saved)
        self.assertTrue(saved["custom_flag"])

    def test_non_finite_speed_falls_back_to_default(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            cfg = AppConfig.from_dict({"speed": bad})
            self.assertAlmostEqual(cfg.speed, 1.0)

    def test_rel_path_healing_also_heals_paired_srt(self):
        out_dir = self.tmp_path("moved_out")
        sub = os.path.join(out_dir, "episode")
        os.makedirs(sub, exist_ok=True)
        wav_moved = os.path.join(sub, "line.wav")
        srt_moved = os.path.join(sub, "line.srt")
        wav_no_srt = os.path.join(sub, "nosrt.wav")
        stale_extra_srt = os.path.join(sub, "nosrt.srt")
        for p in (wav_moved, srt_moved, wav_no_srt, stale_extra_srt):
            with open(p, "wb") as fh:
                fh.write(b"x")

        history_path = self.tmp_path("history.json")
        with open(history_path, "w", encoding="utf-8") as fh:
            json.dump(
                [
                    {
                        "record_id": 1,
                        "path": "C:/old_root/episode/line.wav",
                        "rel_path": os.path.join("episode", "line.wav"),
                        "srt_path": "C:/old_root/episode/line.srt",
                        "text": "paired",
                    },
                    {
                        "record_id": 2,
                        "path": "C:/old_root/episode/nosrt.wav",
                        "rel_path": os.path.join("episode", "nosrt.wav"),
                        "srt_path": "",
                        "text": "unpaired",
                    },
                ],
                fh,
            )

        records = HistoryRepository.load(history_path, base_output_dir=out_dir)
        self.assertEqual(len(records), 2)
        self.assertEqual(os.path.abspath(records[0].path), os.path.abspath(wav_moved))
        self.assertEqual(os.path.abspath(records[0].srt_path), os.path.abspath(srt_moved))
        self.assertFalse(records[0].missing)
        self.assertEqual(os.path.abspath(records[1].path), os.path.abspath(wav_no_srt))
        self.assertEqual(records[1].srt_path, "")


class TestBuiltinRefTextOverride(unittest.TestCase):
    """models/refer.txt (non-empty) must override DEFAULT_REF_TEXT for the
    built-in mambo:default preset; missing/empty file must not."""

    def _make_repo(self, base, refer_txt: str | None):
        models = os.path.join(base, "models")
        os.makedirs(models, exist_ok=True)
        for name in ("manbo-e10.ckpt", "manbo_e8_s168.pth", "refer.wav"):
            open(os.path.join(models, name), "wb").close()
        if refer_txt is not None:
            with open(os.path.join(models, "refer.txt"), "w", encoding="utf-8") as fh:
                fh.write(refer_txt)
        return VoiceRepository(project_dir=base)

    def _ref_text_of_default(self, repo):
        return next(p.ref_text for p in repo.scan() if p.id == "mambo:default")

    def test_refer_txt_overrides_default(self):
        with tempfile.TemporaryDirectory() as base:
            repo = self._make_repo(base, "新参考音频的逐字转写内容")
            self.assertEqual(
                self._ref_text_of_default(repo), "新参考音频的逐字转写内容"
            )

    def test_missing_refer_txt_keeps_default(self):
        from mambotts.domain.models import DEFAULT_REF_TEXT
        with tempfile.TemporaryDirectory() as base:
            repo = self._make_repo(base, None)
            self.assertEqual(self._ref_text_of_default(repo), DEFAULT_REF_TEXT)

    def test_empty_refer_txt_keeps_default(self):
        from mambotts.domain.models import DEFAULT_REF_TEXT
        with tempfile.TemporaryDirectory() as base:
            repo = self._make_repo(base, "   " + chr(10))
            self.assertEqual(self._ref_text_of_default(repo), DEFAULT_REF_TEXT)


class TestDomainAuditRegressions(unittest.TestCase):
    """Regressions from the post-edit Goldfish audit."""

    def test_voice_repo_get_refreshes_refer_txt_live(self):
        from mambotts.domain.models import DEFAULT_REF_TEXT

        with tempfile.TemporaryDirectory() as base:
            models = os.path.join(base, "models")
            os.makedirs(models, exist_ok=True)
            for name in ("manbo-e10.ckpt", "manbo_e8_s168.pth", "refer.wav"):
                open(os.path.join(models, name), "wb").close()
            refer_txt = os.path.join(models, "refer.txt")
            with open(refer_txt, "w", encoding="utf-8") as fh:
                fh.write("初代转写")
            repo = VoiceRepository(project_dir=base)
            self.assertEqual(repo.get("mambo:default").ref_text, "初代转写")

            with open(refer_txt, "w", encoding="utf-8") as fh:
                fh.write("热更新后的转写")
            self.assertEqual(repo.get("mambo:default").ref_text, "热更新后的转写")

            os.remove(refer_txt)
            self.assertEqual(repo.get("mambo:default").ref_text, DEFAULT_REF_TEXT)

    def test_history_rel_path_rejects_traversal_drive_rel_and_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = os.path.join(tmp, "out")
            os.makedirs(out_dir, exist_ok=True)
            outside = os.path.join(tmp, "outside.wav")
            with open(outside, "wb") as fh:
                fh.write(b"RIFF")
            drive, _ = os.path.splitdrive(os.path.abspath(outside))
            drive_rel = f"{drive}outside.wav" if drive else "outside.wav"
            history_path = os.path.join(tmp, "history.json")
            with open(history_path, "w", encoding="utf-8") as fh:
                json.dump(
                    [
                        {
                            "record_id": 1,
                            "path": os.path.join(tmp, "missing1.wav"),
                            "rel_path": "../outside.wav",
                            "text": "traversal",
                        },
                        {
                            "record_id": 2,
                            "path": os.path.join(tmp, "missing2.wav"),
                            "rel_path": drive_rel,
                            "text": "drive-relative",
                        },
                        {
                            "record_id": 3,
                            "path": os.path.join(tmp, "missing3.wav"),
                            "rel_path": ".",
                            "text": "directory-not-file",
                        },
                    ],
                    fh,
                )

            records = HistoryRepository.load(history_path, base_output_dir=out_dir)
            self.assertEqual(len(records), 3)
            for rec in records:
                self.assertTrue(rec.missing)
                self.assertNotEqual(os.path.abspath(rec.path), os.path.abspath(outside))
                self.assertNotEqual(os.path.abspath(rec.path), os.path.abspath(out_dir))

    def test_remove_quiet_and_replace_with_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src.tmp")
            dst = os.path.join(tmp, "dst.txt")
            with open(dst, "w", encoding="utf-8") as fh:
                fh.write("old")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write("new")
            replace_with_retry(src, dst)
            self.assertFalse(os.path.exists(src))
            with open(dst, "r", encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "new")
            remove_quiet(dst)
            remove_quiet(dst)
            remove_quiet(None)
            self.assertFalse(os.path.exists(dst))

    def test_retired_config_keys_dropped_and_reserved_names_unified(self):
        from mambotts.domain.models import AppConfig, SegmentSpec, VoicePreset
        from mambotts.domain.naming import is_reserved_windows_name

        cfg = AppConfig.from_dict(
            {
                "theme": "dark",
                "split_clauses": True,
                "default_voice_id": "old",
                "pause_ms": 250,
                "custom_plugin_flag": 1,
            }
        )
        self.assertEqual(cfg.extra, {"custom_plugin_flag": 1})

        for bad in ("CON.wav", "con.foo.wav", "con .wav", "CONIN$.wav", "conout$.wav", "LPT1.part.wav"):
            self.assertTrue(is_reserved_windows_name(bad), bad)
        for ok in ("hello.wav", "foo.con.wav", "my_nul.wav"):
            self.assertFalse(is_reserved_windows_name(ok), ok)

        from mambotts.domain.naming import resolve_output_dir

        with tempfile.TemporaryDirectory(prefix="mambo_normdir_") as norm_tmp:
            slash_dir = norm_tmp.replace("\\", "/")
            self.assertEqual(resolve_output_dir("", slash_dir), os.path.normpath(norm_tmp))

        with tempfile.TemporaryDirectory(prefix="mambo_ck_") as tmp:
            gsv_dir = os.path.join(tmp, "GPT-SoVITS")
            models_dir = os.path.join(tmp, "models")
            os.makedirs(gsv_dir, exist_ok=True)
            os.makedirs(models_dir, exist_ok=True)
            ref_wav = os.path.join(models_dir, "refer.wav")
            gpt = os.path.join(models_dir, "manbo.ckpt")
            sovits = os.path.join(models_dir, "manbo.pth")
            marker = os.path.join(gsv_dir, ".mambo_install_ok")
            for fp, content in (
                (ref_wav, b"wav1"),
                (gpt, b"gpt1"),
                (sovits, b"sovits1"),
                (marker, b"v5-1"),
            ):
                with open(fp, "wb") as fh:
                    fh.write(content)
            preset = VoicePreset(
                id="mambo:default",
                character="mambo",
                emotion="default",
                ref_wav=ref_wav,
                ref_text="ref",
                ref_lang="zh",
                gpt_model=gpt,
                sovits_model=sovits,
            )
            s1 = SegmentSpec(index=0, text="你好", speed=1.234)
            s2 = SegmentSpec(index=0, text="你好", speed=1.239)
            self.assertNotEqual(s1.cache_key(preset), s2.cache_key(preset))

            k_before = s1.cache_key(preset)
            with open(gpt, "wb") as fh:
                fh.write(b"gpt_updated_size")
            k_after_gpt = s1.cache_key(preset)
            self.assertNotEqual(k_before, k_after_gpt)

            with open(marker, "wb") as fh:
                fh.write(b"v5-2-reinstalled")
            self.assertNotEqual(k_after_gpt, s1.cache_key(preset))


if __name__ == "__main__":
    unittest.main()
