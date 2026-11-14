"""Domain dataclasses & engine contract constants (spec item 5).

Pure Python stdlib module — nothing under ``mambotts/domain/`` may import
third-party packages (or PySide6), per the v2 layering invariants.
"""

from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass, field
from typing import Any

_APP_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def has_pronounceable_char(text: str) -> bool:
    """True when ``text`` contains at least one letter or digit (CJK included)."""
    return any(ch.isalnum() for ch in (text or ""))


def normalize_script_text(text: str) -> str:
    """Collapse whitespace runs and strip surrounding spaces for TTS synthesis."""
    return " ".join((text or "").split())


def _stat_sig(path: str) -> str:
    if not path:
        return "0"
    candidates = (
        [path]
        if os.path.isabs(path)
        else [path, os.path.join(_APP_ROOT, path)]
    )
    for cand in candidates:
        try:
            st = os.stat(cand)
            return f"{st.st_mtime_ns}:{st.st_size}"
        except OSError:
            continue
    return "0"


def _engine_install_sig(preset: "VoicePreset") -> str:
    roots: list[str] = []
    for model_path in (preset.gpt_model, preset.sovits_model, preset.ref_wav):
        if model_path:
            d = os.path.dirname(os.path.abspath(model_path))
            for _ in range(3):
                if d and d not in roots:
                    roots.append(d)
                d = os.path.dirname(d)
    if _APP_ROOT not in roots:
        roots.append(_APP_ROOT)
    for root in roots:
        marker = os.path.join(root, "GPT-SoVITS", ".mambo_install_ok")
        try:
            st = os.stat(marker)
            with open(marker, "rb") as fh:
                digest = hashlib.sha256(fh.read(4096)).hexdigest()[:12]
            return f"{st.st_mtime_ns}:{st.st_size}:{digest}"
        except OSError:
            continue
    return "none"

# ---------------------------------------------------------------------------
# Engine contract constants
# ---------------------------------------------------------------------------

API_HOST = "127.0.0.1"
API_PORT = 9880
DEFAULT_API_URL = "http://127.0.0.1:9880"

DEFAULT_REF_TEXT = "有些人, 见了面也不知该说些什么"
SOVITS_MODEL = "manbo_e8_s168.pth"
GPT_MODEL = "manbo-e10.ckpt"
REF_WAV = "refer.wav"
CUT_PUNC = "，。？！；：、…,.;?!"

SUPPORTED_LANGS: frozenset[str] = frozenset(
    {
        "zh", "en", "ja", "ko", "yue", "auto",
        "auto_yue", "all_zh", "all_yue", "all_ja", "all_ko",
        "中文", "粤语", "英文", "日文", "韩文",
        "中英混合", "粤英混合", "日英混合", "韩英混合",
        "多语种混合", "多语种混合(粤语)",
    }
)

# ``AppConfig`` clamp ranges (applied by ``from_dict`` and re-enforced by
# ``ConfigRepository.load``; also shared by CLI argument validation).
SPEED_MIN = 0.1
SPEED_MAX = 3.0
_SPEED_MIN = SPEED_MIN
_SPEED_MAX = SPEED_MAX


# ---------------------------------------------------------------------------
# Coercion helpers (tolerate sloppy / legacy JSON values)
# ---------------------------------------------------------------------------

def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _as_str(value: Any, default: str) -> str:
    if isinstance(value, str):
        return value
    return default


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class VoicePreset:
    """Reference audio + weights for the one built-in Mambo voice."""

    id: str
    character: str
    emotion: str
    ref_wav: str
    ref_text: str
    ref_lang: str = "zh"
    gpt_model: str = ""
    sovits_model: str = ""


@dataclass
class SegmentSpec:
    """One synthesizable request sent to the GPT-SoVITS engine.

    Sentence breaking at punctuation is owned by the engine (``cut_punc``),
    so each synthesis job builds a single spec.
    """

    index: int
    text: str
    voice_id: str = "mambo:default"
    speed: float = 1.0
    lang: str = "zh"

    def cache_key(self, preset: VoicePreset) -> str:
        """Stable 24-hex LRU cache key for this clause under ``preset``.

        Incorporates the reference audio's ``st_mtime_ns`` so editing a
        reference clip invalidates previously cached clause WAVs.
        """
        ref_sig = _stat_sig(preset.ref_wav)
        gpt_sig = _stat_sig(preset.gpt_model)
        sovits_sig = _stat_sig(preset.sovits_model)
        engine_sig = _engine_install_sig(preset)
        speed_key = f"{round(float(self.speed), 6):.6g}"
        raw = (
            f"{self.text}|{preset.id}|{preset.ref_wav}|{ref_sig}|"
            f"{preset.ref_text}|{preset.ref_lang}|"
            f"{preset.gpt_model}|{gpt_sig}|"
            f"{preset.sovits_model}|{sovits_sig}|"
            f"{engine_sig}|{speed_key}|{self.lang}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


@dataclass
class ClauseResult:
    """Per-clause synthesis outcome with sample-accurate timeline offsets."""

    index: int
    text: str
    voice_id: str
    wav_path: str
    duration_ms: int
    start_ms: int
    end_ms: int
    from_cache: bool = False


@dataclass
class SynthesisJobResult:
    """Whole-job outcome returned by ``SynthesisPipeline.run_job``."""

    success: bool
    message: str
    wav_path: str = ""
    srt_path: str = ""
    duration_sec: float = 0.0
    clauses: list[ClauseResult] = field(default_factory=list)


@dataclass
class AppConfig:
    """Persisted application configuration (``config.json``)."""

    schema_version: int = 2
    api_url: str = DEFAULT_API_URL
    speed: float = 1.0
    output_dir: str = ""
    name: str = ""
    export_srt: bool = True
    trim_silence: bool = True
    normalize_audio: bool = True
    minimize_to_tray: bool = False
    log_visible: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize known fields, then merge ``extra`` (unknown) keys back in."""
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "api_url": self.api_url,
            "speed": self.speed,
            "output_dir": self.output_dir,
            "name": self.name,
            "export_srt": self.export_srt,
            "trim_silence": self.trim_silence,
            "normalize_audio": self.normalize_audio,
            "minimize_to_tray": self.minimize_to_tray,
            "log_visible": self.log_visible,
        }
        for key, value in self.extra.items():
            payload.setdefault(key, value)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AppConfig":
        """Coerce, clamp, and keep unknown keys in ``extra`` (forward compat)."""
        data = data if isinstance(data, dict) else {}
        known = set(cls.__dataclass_fields__.keys())
        cfg = cls()
        # Drop removed v1/theme/director config keys non-destructively so upgrades never retain dead keys.
        _retired_keys = frozenset({"theme", "split_clauses", "default_voice_id", "pause_ms"})
        cfg.extra = {
            k: v for k, v in data.items() if k not in known and k not in _retired_keys
        }

        cfg.api_url = _as_str(data.get("api_url"), cfg.api_url) or DEFAULT_API_URL
        cfg.speed = _clamp(_as_float(data.get("speed"), cfg.speed), _SPEED_MIN, _SPEED_MAX)
        cfg.output_dir = _as_str(data.get("output_dir"), cfg.output_dir)
        cfg.name = _as_str(data.get("name"), cfg.name)
        cfg.export_srt = _as_bool(data.get("export_srt"), cfg.export_srt)
        cfg.trim_silence = _as_bool(data.get("trim_silence"), cfg.trim_silence)
        cfg.normalize_audio = _as_bool(data.get("normalize_audio"), cfg.normalize_audio)
        cfg.minimize_to_tray = _as_bool(data.get("minimize_to_tray"), cfg.minimize_to_tray)
        cfg.log_visible = _as_bool(data.get("log_visible"), cfg.log_visible)
        return cfg


@dataclass
class HistoryRecord:
    """One synthesized-output entry shown in the history panel."""

    record_id: int
    path: str
    text: str
    time: str
    duration: float
    rel_path: str = ""
    srt_path: str = ""
    voice_id: str = "mambo:default"
    speed: float = 1.0
    missing: bool = False

    def to_dict(self) -> dict[str, Any]:
        # ``missing`` is a transient flag recomputed from the filesystem on
        # every ``HistoryRepository.load`` and is intentionally not persisted.
        return {
            "record_id": self.record_id,
            "path": self.path,
            "text": self.text,
            "time": self.time,
            "duration": self.duration,
            "rel_path": self.rel_path,
            "srt_path": self.srt_path,
            "voice_id": self.voice_id,
            "speed": self.speed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "HistoryRecord":
        data = data if isinstance(data, dict) else {}
        return cls(
            record_id=_as_int(data.get("record_id"), 0),
            path=_as_str(data.get("path"), ""),
            text=_as_str(data.get("text"), ""),
            time=_as_str(data.get("time"), ""),
            duration=_as_float(data.get("duration"), 0.0),
            rel_path=_as_str(data.get("rel_path"), ""),
            srt_path=_as_str(data.get("srt_path"), ""),
            voice_id=_as_str(data.get("voice_id"), "mambo:default") or "mambo:default",
            speed=_as_float(data.get("speed"), 1.0),
            missing=_as_bool(data.get("missing"), False),
        )
