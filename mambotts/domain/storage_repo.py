"""Atomic configuration & history persistence repositories (spec item 7).

Pure Python stdlib module. All writes go through :func:`atomic_write_json`
(process-unique temp file + ``os.replace`` + PermissionError backoff retry)
to survive transient Windows file locks and never corrupt a JSON store.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable

from .models import AppConfig, HistoryRecord

__all__ = [
    "ConfigRepository",
    "HistoryRepository",
    "atomic_write_json",
    "remove_quiet",
    "replace_with_retry",
]


def remove_quiet(path: str | None) -> None:
    """Remove ``path`` if present, suppressing filesystem errors."""
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def replace_with_retry(
    src_path: str,
    dst_path: str,
    retries: int = 3,
    backoff_sec: float = 0.05,
) -> None:
    """Atomically replace ``dst_path`` with ``src_path`` retrying Windows locks."""
    attempts = max(1, int(retries))
    last_error: PermissionError | None = None
    for attempt in range(attempts):
        try:
            os.replace(src_path, dst_path)
            return
        except PermissionError as exc:
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(backoff_sec)
    if last_error is not None:
        raise last_error


def atomic_write_json(
    path: str,
    payload: Any,
    indent: int = 2,
    retries: int = 3,
    backoff_sec: float = 0.05,
) -> None:
    """Atomically write ``payload`` as UTF-8 JSON to ``path``.

    Streams into ``<path>.<pid>.<tid>.tmp`` first, then swaps into place via
    :func:`replace_with_retry`. Any leftover temp file is removed in ``finally``.
    """
    tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=indent)
        replace_with_retry(tmp_path, path, retries=retries, backoff_sec=backoff_sec)
    finally:
        remove_quiet(tmp_path)


class ConfigRepository:
    """Load / save :class:`AppConfig` with coercion, clamping and migration.

    ``load`` tolerates a missing, corrupt, or non-object JSON file by falling
    back to defaults (reporting via ``warn_cb``), clamps ``speed``,
    drops obsolete keys (``theme`` / ``pause_ms`` / v1 ``output_file``), and
    preserves unknown keys inside ``AppConfig.extra`` for forward compatibility.
    """

    @staticmethod
    def load(config_path: str, warn_cb: Callable[[str], None] | None = None) -> AppConfig:
        def _warn(message: str) -> None:
            if warn_cb is not None:
                try:
                    warn_cb(message)
                except Exception:  # noqa: BLE001 - a warning callback must never raise
                    pass

        data: dict[str, Any] = {}
        if config_path and os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
            except (OSError, ValueError) as exc:
                _warn(f"配置读取失败，已回退默认设置: {exc}")
            else:
                if isinstance(loaded, dict):
                    data = loaded
                else:
                    _warn(f"配置根节点不是 JSON 对象，已回退默认设置: {config_path}")

        # Legacy migrations: drop obsolete ``theme`` key and migrate v1
        # ``output_file`` -> ``output_dir`` so neither leaks into ``extra``.
        data.pop("theme", None)
        legacy_output_file = data.pop("output_file", None)
        if isinstance(legacy_output_file, str) and legacy_output_file.strip():
            if not str(data.get("output_dir") or "").strip():
                migrated_dir = os.path.dirname(os.path.abspath(legacy_output_file))
                data["output_dir"] = migrated_dir
                _warn(f"已迁移旧配置 output_file -> output_dir: {migrated_dir}")

        return AppConfig.from_dict(data)

    @staticmethod
    def save(config_path: str, cfg: AppConfig) -> None:
        atomic_write_json(config_path, cfg.to_dict())


class HistoryRepository:
    """Persist the synthesized-output history list (capped at MAX_RECORDS)."""

    MAX_RECORDS = 50

    @staticmethod
    def load(
        path: str,
        base_output_dir: str = "",
        include_missing: bool = True,
    ) -> list[HistoryRecord]:
        """Read history records, healing ``path`` via ``rel_path`` when possible.

        A record whose absolute ``path`` no longer exists is re-anchored at
        ``os.path.join(base_output_dir, rel_path)`` when that file exists.
        Healing only considers relative ``rel_path`` values, so an absolute
        ``rel_path`` left over from a save on another drive can never hijack
        the lookup. Each record's ``missing`` flag is recomputed here; when
        ``include_missing`` is False the missing records are filtered out.
        """
        raw: list[Any] = []
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, list):
                    raw = loaded
            except (OSError, ValueError):
                raw = []

        records: list[HistoryRecord] = []
        for item in raw:
            if len(records) >= HistoryRepository.MAX_RECORDS:
                break
            if not isinstance(item, dict):
                continue
            rec = HistoryRecord.from_dict(item)
            if (
                not os.path.isfile(rec.path)
                and rec.rel_path
                and base_output_dir
                and not os.path.isabs(rec.rel_path)
                and not os.path.splitdrive(rec.rel_path)[0]
            ):
                base_abs = os.path.abspath(base_output_dir)
                healed = os.path.abspath(os.path.join(base_abs, rec.rel_path))
                try:
                    within_base = os.path.commonpath([base_abs, healed]) == base_abs
                except ValueError:
                    within_base = False
                if within_base and os.path.isfile(healed):
                    rec.path = healed
                    if rec.srt_path and not os.path.isfile(rec.srt_path):
                        healed_srt = os.path.splitext(healed)[0] + ".srt"
                        if os.path.isfile(healed_srt):
                            rec.srt_path = healed_srt
            rec.missing = not os.path.isfile(rec.path)
            records.append(rec)

        if not include_missing:
            records = [rec for rec in records if not rec.missing]
        return records

    @staticmethod
    def save(
        path: str,
        records: list[HistoryRecord],
        base_output_dir: str = "",
    ) -> None:
        """Write up to MAX_RECORDS entries, refreshing ``rel_path`` per record.

        ``rel_path`` is recomputed only when ``rec.path`` sits on the same
        drive as ``base_output_dir`` (``os.path.relpath`` cannot span drives);
        otherwise the existing value is kept untouched.
        """
        payload: list[dict[str, Any]] = []
        base_drive = os.path.splitdrive(base_output_dir or "")[0].lower()
        for rec in records[: HistoryRepository.MAX_RECORDS]:
            if base_output_dir and rec.path:
                rec_drive = os.path.splitdrive(rec.path)[0].lower()
                if rec_drive == base_drive:
                    try:
                        rec.rel_path = os.path.relpath(rec.path, base_output_dir)
                    except ValueError:
                        pass
            payload.append(rec.to_dict())
        atomic_write_json(path, payload)
