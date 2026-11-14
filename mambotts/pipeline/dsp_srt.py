"""Pure-stdlib PCM audio processor & sample-accurate ``.srt`` exporter.

Implements spec item 13 using only the standard library (``wave``, ``array``,
``os``, ``threading``) — no ``ffmpeg`` / ``pydub`` / ``audioop``.  Every clause
is resampled to the first clause's sample rate (``master_rate``) before
concatenation, and every ``.srt`` timestamp is computed from *cumulative*
master sample offsets (``round(sample_offset * 1000.0 / master_rate)``) so
subtitle timing never accumulates rounding drift.
"""
from __future__ import annotations

import array
import os
import re
import shutil
import threading
import wave

from mambotts.domain.models import CUT_PUNC, ClauseResult
from mambotts.domain.storage_repo import remove_quiet, replace_with_retry

__all__ = [
    "read_pcm16_mono",
    "write_pcm16_mono",
    "resample_pcm16",
    "trim_silence_pcm16",
    "peak_normalize_pcm16",
    "format_srt_timestamp",
    "assemble_clauses_and_srt",
]

_INT16_MIN = -32768
_INT16_MAX = 32767

# Byte-order probe without importing sys: array("h", [1]) is little-endian
# when its first byte is 0x01.  WAV files are little-endian.
_LITTLE_ENDIAN = array.array("h", [1]).tobytes()[:1] == b"\x01"


def _atomic_part_path(path: str, suffix: str = "part") -> str:
    """Process-unique temporary sibling file: ``<path>.<pid>.<tid>.<suffix>``."""
    return f"{path}.{os.getpid()}.{threading.get_ident()}.{suffix}"


def _clamp_int16(value: float) -> int:
    if value > _INT16_MAX:
        return _INT16_MAX
    if value < _INT16_MIN:
        return _INT16_MIN
    return int(round(value))


# ---------------------------------------------------------------------------
# WAV I/O
# ---------------------------------------------------------------------------

def read_pcm16_mono(wav_path: str) -> tuple[int, array.array]:
    """Read ``wav_path`` and return ``(sample_rate, int16_mono_samples)``.

    Supports 8-bit unsigned, 16-bit signed and 32-bit signed PCM; multi-channel
    audio is downmixed to mono by averaging the channel samples.
    """
    with wave.open(wav_path, "rb") as reader:
        channels = reader.getnchannels() or 1
        sampwidth = reader.getsampwidth()
        sample_rate = int(reader.getframerate())
        raw = reader.readframes(reader.getnframes())

    if sampwidth == 2:
        data = array.array("h")
        data.frombytes(raw)
        if not _LITTLE_ENDIAN:
            data.byteswap()
    elif sampwidth == 1:
        unsigned = array.array("B")
        unsigned.frombytes(raw)
        data = array.array("h", [(value - 128) * 256 for value in unsigned])
    elif sampwidth == 4:
        data32 = array.array("i")
        data32.frombytes(raw)
        if not _LITTLE_ENDIAN:
            data32.byteswap()
        data = array.array(
            "h",
            [min(_INT16_MAX, max(_INT16_MIN, value >> 16)) for value in data32],
        )
    else:
        raise ValueError(f"不支持的 WAV 位宽: {sampwidth * 8} bit (path={wav_path!r})")

    total_frames = len(data) // channels
    if channels == 1:
        return sample_rate, data[:total_frames]
    summed = [0] * total_frames
    for channel in range(channels):
        for index, value in enumerate(data[channel:total_frames * channels:channels]):
            summed[index] += value
    mono = array.array("h", [int(round(total / channels)) for total in summed])
    return sample_rate, mono


def _ensure_writable_target(path: str, label: str) -> None:
    """Fail fast before writing when ``path`` is a directory or read-only file."""
    if os.path.isdir(path):
        raise IsADirectoryError(f"目标 {label} 路径是目录：{path}")
    if os.path.isfile(path) and not os.access(path, os.W_OK):
        raise PermissionError(f"目标 {label} 文件不可写（只读或权限不足）：{path}")


def _snapshot_backup(dst_path: str, bak_path: str) -> None:
    """Create a sibling backup via hardlink when supported, else byte copy."""
    remove_quiet(bak_path)
    try:
        os.link(dst_path, bak_path)
    except OSError:
        try:
            shutil.copyfile(dst_path, bak_path)
        except BaseException:
            remove_quiet(bak_path)
            raise


def _commit_staged_outputs(stages: list[tuple[str, str]]) -> None:
    """Atomically commit ``stages`` in order, rolling back prior files on error."""
    remaining_parts = [src for src, _ in stages]
    backups: list[str] = []
    committed: list[tuple[str, str | None]] = []

    try:
        last_index = len(stages) - 1
        for index, (src_part, dst_path) in enumerate(stages):
            bak_path: str | None = None
            if index < last_index and os.path.exists(dst_path):
                bak_path = _atomic_part_path(dst_path, "bak")
                _snapshot_backup(dst_path, bak_path)
                backups.append(bak_path)
            replace_with_retry(src_part, dst_path)
            remaining_parts[index] = ""
            committed.append((dst_path, bak_path))
    except BaseException as commit_exc:
        for dst_path, bak_path in reversed(committed):
            try:
                if bak_path and os.path.exists(bak_path):
                    replace_with_retry(bak_path, dst_path, retries=5, backoff_sec=0.05)
                    backups = [path for path in backups if path != bak_path]
                else:
                    remove_quiet(dst_path)
            except OSError as rollback_exc:
                raise OSError(
                    f"提交输出失败且回滚 {dst_path} 失败 ({rollback_exc})；原因为：{commit_exc}"
                ) from commit_exc
        raise
    finally:
        for src_part in remaining_parts:
            remove_quiet(src_part)
        for bak_path in backups:
            remove_quiet(bak_path)


def _write_pcm16_part(part_path: str, sample_rate: int, samples: array.array) -> None:
    """Write 16-bit mono PCM samples into a staging ``.part`` WAV file."""
    if isinstance(samples, array.array) and samples.typecode == "h":
        payload = samples
    else:
        payload = array.array("h", samples)
    with wave.open(part_path, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(int(sample_rate))
        writer.writeframes(payload.tobytes())


def write_pcm16_mono(wav_path: str, sample_rate: int, samples: array.array) -> None:
    """Write a 16-bit mono WAV atomically (``.part`` + ``os.replace`` + retry)."""
    _ensure_writable_target(wav_path, "WAV")
    directory = os.path.dirname(os.path.abspath(wav_path))
    os.makedirs(directory, exist_ok=True)
    part_path = _atomic_part_path(wav_path)
    try:
        _write_pcm16_part(part_path, sample_rate, samples)
        replace_with_retry(part_path, wav_path)
    except BaseException:
        remove_quiet(part_path)
        raise


# ---------------------------------------------------------------------------
# DSP
# ---------------------------------------------------------------------------

def resample_pcm16(samples: array.array, src_rate: int, dst_rate: int) -> array.array:
    """Linear-interpolation resample; ``dst_len = max(1, round(n * dst / src))``."""
    if src_rate == dst_rate:
        return samples
    count = len(samples)
    if count == 0:
        return array.array("h")
    dst_len = max(1, int(round(count * dst_rate / src_rate)))
    out = array.array("h", bytes(2 * dst_len))
    step = float(src_rate) / float(dst_rate)
    last = count - 1
    for index in range(dst_len):
        position = index * step
        low = int(position)
        if low >= last:
            value = float(samples[last])
        else:
            fraction = position - low
            value = samples[low] + (samples[low + 1] - samples[low]) * fraction
        out[index] = _clamp_int16(value)
    return out


def trim_silence_pcm16(
    samples: array.array,
    sample_rate: int,
    threshold_db: float = -45.0,
    pad_head_ms: int = 30,
    pad_tail_ms: int = 60,
) -> array.array:
    """Trim leading/trailing silence below ``threshold_db`` dBFS.

    Keeps ``pad_head_ms`` / ``pad_tail_ms`` of the original silence as padding
    and applies a 10 ms linear fade at both cut boundaries.  If no sample
    exceeds the threshold (all silence) the original samples are returned
    unchanged.
    """
    count = len(samples)
    if count == 0:
        return samples
    threshold = int(32767 * (10.0 ** (threshold_db / 20.0)))

    head = 0
    while head < count and abs(samples[head]) <= threshold:
        head += 1
    if head >= count:  # all-silence guard
        return samples
    tail = count - 1
    while tail > head and abs(samples[tail]) <= threshold:
        tail -= 1

    start = max(0, head - int(sample_rate * pad_head_ms / 1000.0))
    end = min(count, tail + 1 + int(sample_rate * pad_tail_ms / 1000.0))
    out = samples[start:end]
    if len(out) < 2:
        return out

    # Clamp fade length to the preserved silence cushion so active speech is never attenuated.
    head_margin = max(0, head - start)
    tail_margin = max(0, end - 1 - tail)
    max_fade = min(int(sample_rate * 0.010), len(out) // 2)
    fade_in_len = min(max_fade, head_margin)
    fade_out_len = min(max_fade, tail_margin)
    for index in range(fade_in_len):  # fade-in inside preserved leading cushion only
        out[index] = int(out[index] * (index + 1) / fade_in_len)
    for index in range(fade_out_len):  # fade-out inside preserved trailing cushion only
        position = len(out) - 1 - index
        out[position] = int(out[position] * (index + 1) / fade_out_len)
    return out


def peak_normalize_pcm16(samples: array.array, target_db: float = -1.0) -> array.array:
    """Peak-normalize to ``target_db`` dBFS with a maximum gain of 4.0.

    Returns the input unchanged when the peak is zero or the gain is ~1.0.
    Hot loops stay out of the Python interpreter: peak detection is two
    C-speed ``min``/``max`` scans over the ``array`` buffer, and the gain is
    applied through a 65536-entry lookup table driven by ``map`` (a 10-minute
    48 kHz master is ~29M samples; per-sample Python arithmetic there costs
    whole seconds).

    Index trick: Python negative list indices make ``table[v]`` valid for
    every int16 value -- ``table[-32768]`` reads ``table[32768]`` of the
    65536-entry list, which was written as the scaled value of ``-32768``.
    """
    count = len(samples)
    if count == 0:
        return samples
    peak = max(-min(samples), max(samples))
    if peak == 0:  # zero-peak guard
        return samples
    target_amplitude = 32767 * (10.0 ** (target_db / 20.0))
    gain = target_amplitude / peak
    if gain > 4.0:
        gain = 4.0
    if 0.999 <= gain <= 1.001:
        return samples  # sub-0.1% gain is inaudible -- skip the rewrite
    # Negative assignments land at 65536+value, so ``table[v]`` reads
    # scaled(v) for the whole int16 domain, negatives included.
    table = [0] * 65536
    for value in range(-32768, 32768):
        table[value] = _clamp_int16(value * gain)
    return array.array("h", map(table.__getitem__, samples))


# ---------------------------------------------------------------------------
# .srt
# ---------------------------------------------------------------------------

def format_srt_timestamp(ms: int) -> str:
    """Format milliseconds as ``HH:MM:SS,mmm``."""
    total = int(ms)
    if total < 0:
        total = 0
    hours, remainder = divmod(total, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


# GPT-SoVITS/api.py cut_text() punctuation set & deterministic zero_wav split helpers
# Note: v4/v5 packs 9600 samples at 48 kHz = 200 ms; v2Pro at 32 kHz = 300 ms.
_ZERO_WAV_MIN_MS = 160
_ZERO_WAV_SOFT_MS = 85
_BLIP_MERGE_MS = 75
_CUT_SPLIT_RE = re.compile(f"([{re.escape(CUT_PUNC)}])")


def _split_text_like_api(text: str) -> list[str]:
    """Mirror ``GPT-SoVITS/api.py:cut_text`` + ``only_punc`` filtering."""
    stripped = (text or "").strip("\n")
    if not stripped:
        return []
    parts = _CUT_SPLIT_RE.split(stripped)
    merged = ["".join(pair) for pair in zip(parts[::2], parts[1::2])]
    if len(parts) % 2 == 1 and parts[-1]:
        merged.append(parts[-1])
    out: list[str] = []
    for piece in merged:
        for line in piece.splitlines():
            item = line.strip()
            if not item:
                continue
            if any(ch.isalnum() or ch.isalpha() for ch in item):
                out.append(item)
            elif out:
                # Fold consecutive trailing punctuation (e.g. "？！") onto previous cue.
                out[-1] += item
    return out


def _find_first_active(samples: array.array, abs_threshold: int) -> int | None:
    n = len(samples)
    step = 4096
    for base in range(0, n, step):
        blk = samples[base:min(n, base + step)]
        if max(blk) > abs_threshold or min(blk) < -abs_threshold:
            for idx in range(base, min(n, base + step)):
                val = samples[idx]
                if val > abs_threshold or val < -abs_threshold:
                    return idx
    return None


def _find_last_active(samples: array.array, first_active: int, abs_threshold: int) -> int:
    end = len(samples)
    step = 4096
    while end > first_active:
        start = max(first_active, end - step)
        blk = samples[start:end]
        if max(blk) > abs_threshold or min(blk) < -abs_threshold:
            for idx in range(end - 1, start - 1, -1):
                val = samples[idx]
                if val > abs_threshold or val < -abs_threshold:
                    return idx
        end = start
    return first_active


def _coalesce_valleys(
    samples: array.array,
    sample_rate: int,
    raw_runs: list[tuple[int, int]],
    eff_threshold: int,
    abs_peak: int,
    min_len: int,
    blip_merge_ms: int,
) -> list[tuple[int, int]]:
    """Merge quiet runs separated only by brief residual pause blips."""
    if not raw_runs:
        return []
    blip_max = max(0, int(sample_rate * blip_merge_ms / 1000.0))
    tiny_blip = max(0, int(sample_rate * 0.045))
    flank_min = max(1, min(int(sample_rate * 0.030), min_len // 2))
    bridge_amp_cap = max(eff_threshold * 12, int(abs_peak * 0.20))

    merged: list[tuple[int, int]] = [raw_runs[0]]
    for run_start, run_end in raw_runs[1:]:
        prev_start, prev_end = merged[-1]
        gap = run_start - prev_end
        if gap <= blip_max and (prev_end - prev_start) >= flank_min and (run_end - run_start) >= flank_min:
            if gap <= tiny_blip:
                merged[-1] = (prev_start, run_end)
                continue
            bridge = samples[prev_end:run_start]
            b_hi = max(bridge)
            b_lo = min(bridge)
            if (b_hi if b_hi >= -b_lo else -b_lo) <= bridge_amp_cap:
                merged[-1] = (prev_start, run_end)
                continue
        elif gap <= tiny_blip and ((prev_end - prev_start) >= flank_min or (run_end - run_start) >= flank_min):
            merged[-1] = (prev_start, run_end)
            continue
        merged.append((run_start, run_end))
    return [(s, e) for s, e in merged if e - s >= min_len]


def _find_internal_silence_valleys(
    samples: array.array,
    sample_rate: int,
    min_silence_ms: int = _ZERO_WAV_MIN_MS,
    abs_threshold: int | None = None,
    blip_merge_ms: int = _BLIP_MERGE_MS,
) -> tuple[list[tuple[int, int]], int | None]:
    """Locate internal near-zero runs strictly between active speech regions.

    Returns ``(internal_valleys, last_active_index)``. Uses a gain-invariant
    relative threshold by default and merges brief vocoder blips inside pauses.
    """
    n = len(samples)
    if n == 0 or sample_rate <= 0:
        return [], None
    hi = max(samples)
    lo = min(samples)
    abs_peak = hi if hi >= -lo else -lo
    if abs_peak == 0:
        return [], None
    eff_threshold = (
        max(4, int(abs_peak * 0.0015))
        if abs_threshold is None
        else int(abs_threshold)
    )

    first_active = _find_first_active(samples, eff_threshold)
    if first_active is None:
        return [], None
    last_active = _find_last_active(samples, first_active, eff_threshold)

    min_len = max(1, int(sample_rate * min_silence_ms / 1000.0))
    limit = last_active + 1
    raw_runs: list[tuple[int, int]] = []
    if min_len < 64:
        run_start: int | None = None
        for idx in range(first_active + 1, limit):
            val = samples[idx]
            if -eff_threshold <= val <= eff_threshold:
                if run_start is None:
                    run_start = idx
            elif run_start is not None:
                raw_runs.append((run_start, idx))
                run_start = None
        valleys = _coalesce_valleys(
            samples, sample_rate, raw_runs, eff_threshold, abs_peak, min_len, blip_merge_ms
        )
        return valleys, last_active

    # Scan with ~4 ms blocks so residual clicks inside a 200-300 ms zero_wav don't hide flanking silences.
    block = max(16, min(int(sample_rate * 0.004), min_len // 4))
    pos = first_active + 1
    while pos + block <= limit:
        blk = samples[pos:pos + block]
        if max(blk) <= eff_threshold and min(blk) >= -eff_threshold:
            run_start = pos
            while run_start > first_active + 1 and -eff_threshold <= samples[run_start - 1] <= eff_threshold:
                run_start -= 1
            run_end = pos + block
            while run_end + block <= limit:
                nxt = samples[run_end:run_end + block]
                if max(nxt) <= eff_threshold and min(nxt) >= -eff_threshold:
                    run_end += block
                else:
                    break
            while run_end < limit and -eff_threshold <= samples[run_end] <= eff_threshold:
                run_end += 1
            raw_runs.append((run_start, run_end))
            pos = run_end + 1
        else:
            pos += block
    valleys = _coalesce_valleys(
        samples, sample_rate, raw_runs, eff_threshold, abs_peak, min_len, blip_merge_ms
    )
    return valleys, last_active


def _chunk_weights(chunks: list[str]) -> list[int]:
    return [max(1, sum(1 for ch in chunk if ch.isalnum() or ch.isalpha())) for chunk in chunks]


def _proportional_span_cues(
    chunks: list[str],
    span_start: int,
    span_end: int,
    weights: list[int],
    offset: int,
    master_rate: int,
) -> list[tuple[int, int, str]]:
    """Distribute a sample span across ``chunks`` proportionally by readable character weight."""
    total_w = max(1, sum(weights))
    span_len = max(0, span_end - span_start)
    out: list[tuple[int, int, str]] = []
    cum = 0
    for idx, chunk in enumerate(chunks):
        s_off = span_start + round(span_len * cum / total_w)
        cum += weights[idx]
        e_off = span_start + round(span_len * cum / total_w) if idx < len(chunks) - 1 else span_end
        s_ms = round((offset + s_off) * 1000.0 / master_rate)
        e_ms = round((offset + max(s_off, e_off)) * 1000.0 / master_rate)
        out.append((s_ms, e_ms, chunk))
    return out


def _select_excess_valleys(
    valleys: list[tuple[int, int]],
    expected_samples: list[float],
) -> list[tuple[int, int]]:
    """Select an order-preserving subset of valleys matching ``len(expected_samples)`` boundaries."""
    need = len(expected_samples)
    m = len(valleys)
    inf = float("inf")
    mids = [0.5 * (s + e) for s, e in valleys]
    widths = [e - s for s, e in valleys]

    dp = [[inf] * m for _ in range(need)]
    parent = [[-1] * m for _ in range(need)]
    for j in range(m):
        dp[0][j] = abs(mids[j] - expected_samples[0]) - 0.12 * widths[j]

    for b in range(1, need):
        best_prev = inf
        best_idx = -1
        for j in range(b, m - (need - 1 - b)):
            if dp[b - 1][j - 1] < best_prev:
                best_prev = dp[b - 1][j - 1]
                best_idx = j - 1
            if best_idx >= 0:
                dp[b][j] = best_prev + abs(mids[j] - expected_samples[b]) - 0.12 * widths[j]
                parent[b][j] = best_idx

    last_j = min(range(need - 1, m), key=lambda j: dp[need - 1][j])
    chosen: list[tuple[int, int]] = []
    cur_j = last_j
    for b in range(need - 1, -1, -1):
        chosen.append(valleys[cur_j])
        cur_j = parent[b][cur_j]
    chosen.reverse()
    return chosen


def _assign_scarce_valleys(
    valleys: list[tuple[int, int]],
    expected_samples: list[float],
) -> list[int]:
    """Assign all ``k = len(valleys)`` valleys monotonically to the closest split boundaries."""
    k = len(valleys)
    need = len(expected_samples)
    inf = float("inf")
    mids = [0.5 * (s + e) for s, e in valleys]

    dp = [[inf] * need for _ in range(k)]
    parent = [[-1] * need for _ in range(k)]
    for b in range(need):
        dp[0][b] = abs(mids[0] - expected_samples[b])

    for i in range(1, k):
        best_prev = inf
        best_b = -1
        for b in range(i, need - (k - 1 - i)):
            if dp[i - 1][b - 1] < best_prev:
                best_prev = dp[i - 1][b - 1]
                best_b = b - 1
            if best_b >= 0:
                dp[i][b] = best_prev + abs(mids[i] - expected_samples[b])
                parent[i][b] = best_b

    last_b = min(range(k - 1, need), key=lambda b: dp[k - 1][b])
    chosen_bounds: list[int] = []
    cur_b = last_b
    for i in range(k - 1, -1, -1):
        chosen_bounds.append(cur_b)
        cur_b = parent[i][cur_b]
    chosen_bounds.reverse()
    return chosen_bounds


def _cues_from_exact_valleys(
    chunks: list[str],
    valleys: list[tuple[int, int]],
    tail_end: int,
    offset: int,
    master_rate: int,
) -> list[tuple[int, int, str]]:
    cues: list[tuple[int, int, str]] = []
    cur_start = 0
    for idx, chunk in enumerate(chunks):
        if idx < len(valleys):
            v_start, v_end = valleys[idx]
            cue_end = max(cur_start, v_start)
            next_start = max(cue_end, v_end)
        else:
            cue_end = max(cur_start + 1, tail_end)
            next_start = cue_end
        cues.append((
            round((offset + cur_start) * 1000.0 / master_rate),
            round((offset + cue_end) * 1000.0 / master_rate),
            chunk,
        ))
        cur_start = next_start
    return cues


def _normalize_cue_monotonicity(
    cues: list[tuple[int, int, str]],
    start_ms: int,
    end_ms: int,
) -> list[tuple[int, int, str]]:
    if not cues:
        return []
    out: list[tuple[int, int, str]] = []
    prev_end = start_ms
    total = len(cues)
    for idx, (c_start, c_end, c_text) in enumerate(cues):
        s_ms = max(prev_end, c_start)
        max_allow_end = end_ms if idx == total - 1 else max(s_ms + 1, end_ms - (total - 1 - idx))
        if total > 1 and end_ms - start_ms >= total:
            s_ms = min(s_ms, max_allow_end - 1)
            e_ms = min(max_allow_end, max(s_ms + 1, c_end))
        else:
            e_ms = min(end_ms, max(s_ms, c_end))
        out.append((s_ms, e_ms, c_text))
        prev_end = e_ms
    return out


def _clause_srt_cues(
    text: str,
    samples: array.array,
    master_rate: int,
    offset: int,
    start_ms: int,
    end_ms: int,
) -> list[tuple[int, int, str]]:
    """Split a multi-sentence clause SRT cue using detected silence valleys with resilient alignment."""
    chunks = _split_text_like_api(text)
    if len(chunks) < 2:
        return [(start_ms, end_ms, text)]

    need = len(chunks) - 1
    valleys, last_active = _find_internal_silence_valleys(samples, master_rate)
    if len(valleys) < need:
        soft_valleys, soft_last = _find_internal_silence_valleys(
            samples, master_rate, min_silence_ms=_ZERO_WAV_SOFT_MS
        )
        if len(soft_valleys) > len(valleys):
            valleys, last_active = soft_valleys, soft_last

    tail_end = len(samples)
    if last_active is not None:
        min_tail_silence = max(1, int(master_rate * _ZERO_WAV_MIN_MS / 1000.0))
        if tail_end - 1 - last_active >= min_tail_silence:
            tail_end = min(tail_end, last_active + 1 + int(master_rate * 60 / 1000.0))

    if len(valleys) == need and last_active is not None:
        return _cues_from_exact_valleys(chunks, valleys, tail_end, offset, master_rate)

    weights = _chunk_weights(chunks)
    total_w = max(1, sum(weights))
    cum_w = 0
    expected_samples: list[float] = []
    for idx in range(need):
        cum_w += weights[idx]
        expected_samples.append(tail_end * (cum_w / total_w))

    if len(valleys) > need:
        chosen = _select_excess_valleys(valleys, expected_samples)
        cues = _cues_from_exact_valleys(chunks, chosen, tail_end, offset, master_rate)
        return _normalize_cue_monotonicity(cues, start_ms, end_ms)

    if 0 < len(valleys) < need:
        bound_indices = _assign_scarce_valleys(valleys, expected_samples)
        cues: list[tuple[int, int, str]] = []
        chunk_cursor = 0
        sample_cursor = 0
        for v_idx, b_idx in enumerate(bound_indices):
            v_start, v_end = valleys[v_idx]
            group_end_chunk = b_idx + 1
            sub_chunks = chunks[chunk_cursor:group_end_chunk]
            sub_weights = weights[chunk_cursor:group_end_chunk]
            cues.extend(
                _proportional_span_cues(
                    sub_chunks, sample_cursor, max(sample_cursor, v_start),
                    sub_weights, offset, master_rate,
                )
            )
            chunk_cursor = group_end_chunk
            sample_cursor = max(sample_cursor, v_end)
        if chunk_cursor < len(chunks):
            cues.extend(
                _proportional_span_cues(
                    chunks[chunk_cursor:], sample_cursor, max(sample_cursor, tail_end),
                    weights[chunk_cursor:], offset, master_rate,
                )
            )
        return _normalize_cue_monotonicity(cues, start_ms, end_ms)

    cues = _proportional_span_cues(chunks, 0, tail_end, weights, offset, master_rate)
    return _normalize_cue_monotonicity(cues, start_ms, end_ms)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("合成已取消。")


def assemble_clauses_and_srt(
    clause_wavs: list[tuple[object, str, bool]],
    out_wav_path: str,
    out_srt_path: str | None = None,
    trim_silence: bool = True,
    normalize: bool = True,
    cancel_event: threading.Event | None = None,
) -> tuple[float, list[ClauseResult]]:
    """Finalize per-clause WAVs into one output WAV plus an optional ``.srt``.

    ``clause_wavs`` is a list of ``(SegmentSpec, wav_path, from_cache)``.
    All entries are resampled to the first entry's sample rate
    (``master_rate``) and concatenated seamlessly; punctuation pauses live
    inside the engine-generated audio itself.

    ``start_ms`` / ``end_ms`` come from cumulative master sample offsets via
    ``round(sample_offset * 1000.0 / master_rate)`` for zero cumulative drift.

    Returns ``(duration_sec, [ClauseResult, ...])``.
    """
    if not clause_wavs:
        raise ValueError("assemble_clauses_and_srt 需要至少一个分句 WAV。")

    master_rate: int | None = None
    processed: list[tuple[object, array.array, str, bool]] = []
    for spec, wav_path, from_cache in clause_wavs:
        _check_cancel(cancel_event)
        rate, samples = read_pcm16_mono(wav_path)
        if master_rate is None:
            master_rate = rate
        elif rate != master_rate:
            samples = resample_pcm16(samples, rate, master_rate)
        if trim_silence:
            samples = trim_silence_pcm16(samples, master_rate)
        processed.append((spec, samples, wav_path, from_cache))

    combined = array.array("h")
    offset = 0  # cumulative master sample offset
    results: list[ClauseResult] = []
    pending_cues: list[tuple[str, int, int, int, int]] = []
    for spec, samples, wav_path, from_cache in processed:
        _check_cancel(cancel_event)
        clause_offset = offset
        sample_count = len(samples)
        start_ms = round(clause_offset * 1000.0 / master_rate)
        combined.extend(samples)
        offset += sample_count
        end_ms = round(offset * 1000.0 / master_rate)
        if out_srt_path:
            pending_cues.append((spec.text, clause_offset, sample_count, start_ms, end_ms))
        results.append(
            ClauseResult(
                index=spec.index,
                text=spec.text,
                voice_id=spec.voice_id,
                wav_path=wav_path,
                duration_ms=end_ms - start_ms,
                start_ms=start_ms,
                end_ms=end_ms,
                from_cache=from_cache,
            )
        )

    _check_cancel(cancel_event)
    if normalize and combined:
        combined = peak_normalize_pcm16(combined, target_db=-1.0)

    srt_cues: list[tuple[int, int, str]] = []
    if out_srt_path:
        # Detect SRT pauses on the final delivered (normalized) waveform so gain stays consistent.
        for cue_text, clause_offset, sample_count, start_ms, end_ms in pending_cues:
            _check_cancel(cancel_event)
            clause_slice = combined[clause_offset:clause_offset + sample_count]
            srt_cues.extend(
                _clause_srt_cues(cue_text, clause_slice, master_rate, clause_offset, start_ms, end_ms)
            )

    _ensure_writable_target(out_wav_path, "WAV")
    if out_srt_path:
        _ensure_writable_target(out_srt_path, "SRT")

    _check_cancel(cancel_event)
    duration_sec = len(combined) / float(master_rate)

    wav_dir = os.path.dirname(os.path.abspath(out_wav_path))
    os.makedirs(wav_dir, exist_ok=True)
    wav_part = _atomic_part_path(out_wav_path, "wav.part")
    srt_part: str | None = None

    try:
        _write_pcm16_part(wav_part, master_rate, combined)
        stages: list[tuple[str, str]] = [(wav_part, out_wav_path)]
        if out_srt_path:
            blocks: list[str] = []
            for number, (cue_start, cue_end, cue_text) in enumerate(srt_cues, start=1):
                blocks.append(
                    f"{number}\n"
                    f"{format_srt_timestamp(cue_start)} --> "
                    f"{format_srt_timestamp(cue_end)}\n"
                    f"{cue_text}\n"
                )
            srt_dir = os.path.dirname(os.path.abspath(out_srt_path))
            os.makedirs(srt_dir, exist_ok=True)
            srt_part = _atomic_part_path(out_srt_path, "srt.part")
            with open(srt_part, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("\n".join(blocks))
            stages.append((srt_part, out_srt_path))
        _check_cancel(cancel_event)
        _commit_staged_outputs(stages)
    except BaseException:
        remove_quiet(wav_part)
        remove_quiet(srt_part)
        raise

    return duration_sec, results
