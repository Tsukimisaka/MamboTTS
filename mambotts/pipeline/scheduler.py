"""Bounded LRU clause cache & synthesis pipeline orchestrator (spec item 14).

``SegmentCache`` stores per-request WAVs under ``%TEMP%/mambotts_clause_cache``
keyed by ``SegmentSpec.cache_key(preset)`` with an LRU cap of ``max_entries``
(evicting oldest files by ``st_mtime``).  ``SynthesisPipeline.run_job``
orchestrates parse → per-request synthesis (cache-aware, cancellable) →
``assemble_clauses_and_srt`` and *always* returns a
:class:`~mambotts.domain.models.SynthesisJobResult` (never a raw tuple).
"""
from __future__ import annotations

import logging
import math
import os
import re
import time
import tempfile
import threading
import wave
from typing import TYPE_CHECKING, Callable

from mambotts.domain.models import (
    CUT_PUNC,
    SPEED_MAX,
    SPEED_MIN,
    ClauseResult,
    SegmentSpec,
    SynthesisJobResult,
    has_pronounceable_char,
    normalize_script_text,
)
from mambotts.domain.storage_repo import remove_quiet, replace_with_retry
from mambotts.infra.gsv_client import looks_like_wav
from mambotts.pipeline.dsp_srt import assemble_clauses_and_srt, read_pcm16_mono

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps infra imports lazy
    from mambotts.domain.voice_repo import VoiceRepository
    from mambotts.infra.engine_mgr import EngineSupervisor
    from mambotts.infra.gsv_client import GSVHttpClient

__all__ = ["SegmentCache", "SynthesisPipeline"]

_COPY_CHUNK_SIZE = 65536
_MAX_SEGMENT_WEIGHT = 180
_HARD_CLAUSE_WEIGHT = 140
_SEG_SPLIT_RE = re.compile(f"([{re.escape(CUT_PUNC)}\\n])")
_logger = logging.getLogger("mambotts.pipeline.scheduler")


def _bert_budget_weight(text: str) -> int:
    """Estimate BERT/duration pressure; ASCII digits expand roughly 2x in ZH TN."""
    return sum(2 if ch.isdigit() else 1 for ch in text)


def _hard_split_long_clause(clause: str, max_weight: int) -> list[str]:
    """Split an unpunctuated oversized clause at safe word/boundary offsets."""
    pieces: list[str] = []
    remaining = clause
    while _bert_budget_weight(remaining) > max_weight:
        acc = 0
        split_at = 0
        for idx, ch in enumerate(remaining):
            w = 2 if ch.isdigit() else 1
            if acc + w > max_weight and idx > 0:
                split_at = idx
                break
            acc += w
        else:
            break
        win_start = max(1, int(split_at * 0.6))
        ws_idx = -1
        for pos in range(split_at, win_start - 1, -1):
            if remaining[pos - 1].isspace():
                ws_idx = pos
                break
        if ws_idx > 0:
            cut = ws_idx
        else:
            cut = split_at
            low = max(1, split_at - 16)
            while (
                cut > low
                and remaining[cut - 1].isascii()
                and remaining[cut - 1].isalnum()
                and remaining[cut].isascii()
                and remaining[cut].isalnum()
            ):
                cut -= 1
        head = remaining[:cut].strip()
        tail = remaining[cut:].strip()
        if not has_pronounceable_char(tail):
            break
        if has_pronounceable_char(head):
            pieces.append(head)
        elif pieces:
            pieces[-1] += head
        remaining = tail
    if remaining:
        if has_pronounceable_char(remaining) or not pieces:
            pieces.append(remaining)
        else:
            pieces[-1] += remaining
    return pieces


def _split_safe_segments(cleaned: str) -> list[str]:
    """Keep short scripts in one request; split long/unpunctuated scripts safely.

    Short scripts (weighted length <= 180 and every clause <= 140) stay as a
    single HTTP request to preserve cross-sentence prosody. Longer or
    unpunctuated scripts split into bounded segments so GPT-SoVITS never hits
    s1 ``max_sec: 54`` (~60 s truncation) or the 512-token BERT limit.
    """
    raw_parts = _SEG_SPLIT_RE.split(cleaned)
    atoms: list[str] = []
    buf = ""
    for idx in range(0, len(raw_parts), 2):
        body = raw_parts[idx]
        delim = raw_parts[idx + 1] if idx + 1 < len(raw_parts) else ""
        piece = body + delim
        if not piece:
            continue
        if has_pronounceable_char(piece):
            if buf:
                atoms.append(buf + piece)
                buf = ""
            else:
                atoms.append(piece)
        elif atoms:
            atoms[-1] += piece
        else:
            buf += piece
    if buf:
        if atoms:
            atoms[-1] += buf
        else:
            atoms.append(buf)

    if (
        _bert_budget_weight(cleaned) <= _MAX_SEGMENT_WEIGHT
        and all(_bert_budget_weight(a) <= _HARD_CLAUSE_WEIGHT for a in atoms)
    ):
        return [cleaned]

    expanded: list[str] = []
    for atom in atoms:
        if _bert_budget_weight(atom) > _HARD_CLAUSE_WEIGHT:
            expanded.extend(_hard_split_long_clause(atom, _HARD_CLAUSE_WEIGHT))
        else:
            expanded.append(atom)

    segments: list[str] = []
    cur = ""
    cur_w = 0
    for piece in expanded:
        pw = _bert_budget_weight(piece)
        if cur and cur_w + pw > _MAX_SEGMENT_WEIGHT and has_pronounceable_char(piece):
            stripped = cur.strip()
            if has_pronounceable_char(stripped):
                segments.append(stripped)
            cur = piece
            cur_w = pw
        else:
            cur += piece
            cur_w += pw
    if cur:
        stripped = cur.strip()
        if has_pronounceable_char(stripped):
            segments.append(stripped)
        elif segments:
            segments[-1] += stripped
    return segments or [cleaned]


def _atomic_copy(src_path: str, dst_path: str) -> None:
    """Chunked copy into ``<dst>.<pid>.<tid>.part`` then atomic replace."""
    part_path = f"{dst_path}.{os.getpid()}.{threading.get_ident()}.part"
    try:
        with open(src_path, "rb") as src, open(part_path, "wb") as dst:
            while True:
                chunk = src.read(_COPY_CHUNK_SIZE)
                if not chunk:
                    break
                dst.write(chunk)
        replace_with_retry(part_path, dst_path)
    except BaseException:
        remove_quiet(part_path)
        raise


class SegmentCache:
    """Bounded on-disk LRU cache of per-clause WAV files.

    Keys are the 24-hex ``SegmentSpec.cache_key(preset)`` digests; entries live
    as ``<cache_dir>/<key>.wav``.  ``get`` validates the RIFF/WAVE structure via
    ``looks_like_wav`` and touches the file mtime (LRU recency); ``put`` writes
    through a process-unique ``.part`` file plus ``os.replace`` and prunes the
    oldest entries by ``st_mtime`` beyond ``max_entries``.
    """

    def __init__(
        self,
        cache_dir: str | None = None,
        max_entries: int = 200,
        max_bytes: int = 2 * 1024 * 1024 * 1024,
    ) -> None:
        self.cache_dir = cache_dir or os.path.join(
            tempfile.gettempdir(), "mambotts_clause_cache"
        )
        self._sweep_orphans()  # crash leftovers: *.part / job_* older than 24h
        self.max_entries = max(1, int(max_entries))
        self.max_bytes = max(1, int(max_bytes))
        self._lock = threading.Lock()
        os.makedirs(self.cache_dir, exist_ok=True)

    # ------------------------------------------------------------------

    def path_for(self, key: str) -> str:
        return os.path.join(self.cache_dir, f"{key}.wav")

    def get(self, key: str) -> str | None:
        """Return the cached WAV path for ``key``, or ``None`` on any miss."""
        path = self.path_for(key)
        try:
            with open(path, "rb") as handle:
                head = handle.read(8192)
        except OSError:
            return None
        if not looks_like_wav(head):
            return None
        try:
            os.utime(path, None)  # mtime touch for LRU recency
        except OSError:
            pass
        return path

    def put(self, key: str, wav_path: str, prune: bool = True) -> str:
        """Copy ``wav_path`` into the cache under ``key`` (atomic), optionally pruning."""
        dest = self.path_for(key)
        part_path = f"{dest}.{os.getpid()}.{threading.get_ident()}.part"
        try:
            with open(wav_path, "rb") as src, open(part_path, "wb") as dst:
                while True:
                    chunk = src.read(_COPY_CHUNK_SIZE)
                    if not chunk:
                        break
                    dst.write(chunk)
            replace_with_retry(part_path, dest)
        except BaseException:
            remove_quiet(part_path)
            raise
        if prune:
            self._prune()
        return dest

    def prune(self) -> None:
        """Evict oldest cached WAVs exceeding ``max_entries``."""
        self._prune()

    def invalidate(self, key: str) -> None:
        remove_quiet(self.path_for(key))

    def clear(self) -> None:
        try:
            names = os.listdir(self.cache_dir)
        except OSError:
            return
        for name in names:
            path = os.path.join(self.cache_dir, name)
            if os.path.isfile(path):
                remove_quiet(path)

    def _sweep_orphans(self) -> None:
        """Startup cleanup of crash leftovers: stale .part / job_* temp wavs."""
        try:
            now = time.time()
            for name in os.listdir(self.cache_dir):
                if not (name.endswith(".part") or name.startswith("job_")):
                    continue
                fp = os.path.join(self.cache_dir, name)
                try:
                    if abs(now - os.path.getmtime(fp)) > 86400:
                        os.remove(fp)
                except OSError:
                    pass
        except OSError:
            pass

    def _prune(self) -> None:
        with self._lock:
            try:
                names = os.listdir(self.cache_dir)
            except OSError:
                return
            entries: list[tuple[float, int, str]] = []
            for name in names:
                if not name.endswith(".wav"):
                    continue
                path = os.path.join(self.cache_dir, name)
                try:
                    st = os.stat(path)
                    entries.append((st.st_mtime, st.st_size, path))
                except OSError:
                    continue
            total_count = len(entries)
            total_bytes = sum(size for _, size, _ in entries)
            if total_count <= self.max_entries and total_bytes <= self.max_bytes:
                return
            entries.sort(key=lambda item: item[0])  # oldest first
            for _, size, path in entries:
                if total_count <= self.max_entries and total_bytes <= self.max_bytes:
                    break
                remove_quiet(path)
                total_count -= 1
                total_bytes -= size


def _clamp_speed(speed: float) -> float:
    try:
        value = 1.0 if speed is None else float(speed)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(value):
        return 1.0
    return max(SPEED_MIN, min(SPEED_MAX, value))


class SynthesisPipeline:
    """Single-request cache-aware synthesis pipeline & DSP/SRT post-processor."""

    def __init__(
        self,
        voice_repo: "VoiceRepository",
        gsv_client: "GSVHttpClient",
        supervisor: "EngineSupervisor | None" = None,
        cache: SegmentCache | None = None,
    ) -> None:
        self.voice_repo = voice_repo
        self.gsv_client = gsv_client
        self.supervisor = supervisor
        self.cache = cache if cache is not None else SegmentCache()

    # ------------------------------------------------------------------

    def run_job(
        self,
        text: str,
        save_path: str,
        speed: float = 1.0,
        api_url: str | None = None,
        export_srt: bool = False,
        trim_silence: bool = True,
        normalize_audio: bool = True,
        use_cache: bool = True,
        cancel_event: threading.Event | None = None,
        progress_cb: Callable[[int, int, str], None] | None = None,
    ) -> SynthesisJobResult:
        """Synthesize ``text`` to ``save_path`` (and optional ``.srt``).

        Always returns a :class:`SynthesisJobResult`.  The full normalized
        text is sent to the engine in one request (punctuation pauses are
        owned by GPT-SoVITS via ``cut_punc``).  ``use_cache`` skips the
        engine when the identical text and speed were already synthesized.
        """
        text = text or ""
        if not text.strip():
            return SynthesisJobResult(success=False, message="文本为空，没有可合成的内容。")

        def report(done: int, total: int, stage: str) -> None:
            if progress_cb is not None:
                try:
                    progress_cb(done, total, stage)
                except Exception:  # noqa: BLE001 - callbacks must not break the job
                    pass

        try:
            cleaned = normalize_script_text(text)
            if not has_pronounceable_char(cleaned):
                return SynthesisJobResult(success=False, message="没有可朗读的文本内容。")
            seg_texts = _split_safe_segments(cleaned)
            clamped_speed = _clamp_speed(speed)
            specs = [
                SegmentSpec(
                    index=idx,
                    text=seg_text,
                    voice_id="mambo:default",
                    speed=clamped_speed,
                    lang="zh",
                )
                for idx, seg_text in enumerate(seg_texts)
            ]

            # Punctuation breaking is the engine's job, always at the full
            # Chinese + ASCII punctuation set (0.3s silence per break).
            cut_punc = CUT_PUNC
            save_dir = os.path.dirname(os.path.abspath(save_path)) or "."
            os.makedirs(save_dir, exist_ok=True)

            if self.supervisor is not None:
                self.supervisor.set_synthesis_busy(True)
            tmp_paths: list[str] = []
            try:
                return self._synthesize_specs(
                    specs, cut_punc, save_path, save_dir,
                    api_url, export_srt, trim_silence, normalize_audio,
                    use_cache, cancel_event, report, tmp_paths,
                )
            finally:
                for tmp_path in tmp_paths:
                    remove_quiet(tmp_path)
                if use_cache:
                    self.cache.prune()
                if self.supervisor is not None:
                    self.supervisor.set_synthesis_busy(False)
        except Exception as exc:  # noqa: BLE001 - library API always returns a result
            if cancel_event is not None and cancel_event.is_set():
                return SynthesisJobResult(success=False, message="合成已取消。")
            return SynthesisJobResult(success=False, message=f"合成失败：{exc}")

    # ------------------------------------------------------------------

    def _synthesize_specs(
        self,
        specs,
        cut_punc: str,
        save_path: str,
        save_dir: str,
        api_url: str | None,
        export_srt: bool,
        trim_silence: bool,
        normalize_audio: bool,
        use_cache: bool,
        cancel_event: threading.Event | None,
        report: Callable[[int, int, str], None],
        tmp_paths: list[str],
    ) -> SynthesisJobResult:
        total = len(specs)
        report(0, total, "开始合成")
        clause_wavs: list[tuple[object, str, bool]] = []

        for spec in specs:
            if cancel_event is not None and cancel_event.is_set():
                return SynthesisJobResult(success=False, message="合成已取消。")

            spec_preset = self.voice_repo.get(spec.voice_id)
            key = spec.cache_key(spec_preset)

            clause_path = self.cache.get(key) if use_cache else None
            from_cache = clause_path is not None
            if not from_cache:
                tmp_path = os.path.join(
                    self.cache.cache_dir,
                    f"job_{key}.{os.getpid()}.{threading.get_ident()}.wav",
                )
                ok, message = self.gsv_client.synthesize_clause(
                    spec.text,
                    tmp_path,
                    spec_preset,
                    speed=spec.speed,
                    text_lang=spec.lang,
                    api_url=api_url,
                    cut_punc=cut_punc,
                    cancel_event=cancel_event,
                )
                if not ok:
                    remove_quiet(tmp_path)
                    if cancel_event is not None and cancel_event.is_set():
                        return SynthesisJobResult(success=False, message="合成已取消。")
                    return SynthesisJobResult(
                        success=False,
                        message=f"第 {spec.index + 1} 句合成失败：{message}",
                    )
                # A verified HTTP response is the only online-state writer.
                if self.supervisor is not None:
                    self.supervisor.mark_online(True)
                tmp_paths.append(tmp_path)
                if use_cache:
                    # Defer pruning until after job assembly so >max_entries jobs cannot evict active clauses.
                    clause_path = self.cache.put(key, tmp_path, prune=False)
                else:
                    clause_path = tmp_path

            try:
                with wave.open(clause_path, "rb") as wf:
                    seg_rate = wf.getframerate() or 1
                    seg_dur = wf.getnframes() / float(seg_rate)
                if seg_dur >= 53.5 and sum(1 for c in spec.text if has_pronounceable_char(c)) >= 200:
                    _logger.warning(
                        "分段 %d 输出时长 %.1fs 接近引擎单段上限（~54-60s），可能存在尾部截断。",
                        spec.index + 1, seg_dur,
                    )
            except (OSError, wave.Error):
                pass

            clause_wavs.append((spec, clause_path, from_cache))
            report(spec.index + 1, total, f"已合成 {spec.index + 1}/{total} 段")

        if cancel_event is not None and cancel_event.is_set():
            return SynthesisJobResult(success=False, message="合成已取消。")

        fast_path = (
            total == 1
            and not export_srt
            and not trim_silence
            and not normalize_audio
        )
        if fast_path:
            spec, clause_path, from_cache = clause_wavs[0]
            rate, samples = read_pcm16_mono(clause_path)
            _atomic_copy(clause_path, save_path)
            end_ms = round(len(samples) * 1000.0 / rate)
            clause = ClauseResult(
                index=spec.index,
                text=spec.text,
                voice_id=spec.voice_id,
                wav_path=save_path,
                duration_ms=end_ms,
                start_ms=0,
                end_ms=end_ms,
                from_cache=from_cache,
            )
            report(total, total, "导出完成")
            message_out = "合成完成"
            return SynthesisJobResult(
                success=True,
                message=message_out,
                wav_path=save_path,
                srt_path="",
                duration_sec=len(samples) / float(rate),
                clauses=[clause],
            )

        srt_target = os.path.splitext(save_path)[0] + ".srt" if export_srt else None
        report(total, total, "导出音频与字幕")
        duration_sec, clause_results = assemble_clauses_and_srt(
            clause_wavs,
            save_path,
            srt_target,
            trim_silence=trim_silence,
            normalize=normalize_audio,
            cancel_event=cancel_event,
        )
        message_out = "合成完成"
        return SynthesisJobResult(
            success=True,
            message=message_out,
            wav_path=save_path,
            srt_path=srt_target or "",
            duration_sec=duration_sec,
            clauses=clause_results,
        )
