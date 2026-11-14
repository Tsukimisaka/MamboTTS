"""File naming & path sanitization utilities (spec item 6).

Pure Python stdlib module.
"""

from __future__ import annotations

import os

# Windows-forbidden filename characters plus all ASCII control chars (0x00-0x1F).
_INVALID_CHARS = '<>:"/\\|?*' + "".join(chr(i) for i in range(32))

DEFAULT_BASENAME = "mambo_output"

# Reserved Windows device names (checked against the first dot-segment, uppercase).
_RESERVED = {
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
WINDOWS_RESERVED_STEMS = frozenset(_RESERVED)

# Compressed/lossy audio extensions replaced with ".wav" by ``ensure_wav_suffix``.
_OTHER_AUDIO_EXTS = (".mp3", ".flac", ".ogg", ".m4a", ".aac", ".wma", ".opus")

__all__ = [
    "DEFAULT_BASENAME",
    "WINDOWS_RESERVED_STEMS",
    "is_reserved_windows_name",
    "sanitize_stem",
    "filename_from_text",
    "ensure_wav_suffix",
    "unique_path",
    "resolve_output_dir",
]


def is_reserved_windows_name(name: str) -> bool:
    """True when ``name``'s first dot-segment matches a Windows reserved device name."""
    stem = os.path.basename((name or "").strip()).strip(". ")
    if not stem:
        return False
    return stem.split(".")[0].strip(" ").upper() in WINDOWS_RESERVED_STEMS


def sanitize_stem(name: str) -> str:
    """Turn ``name`` into a safe Windows filename stem.

    Removes forbidden/control characters, strips leading/trailing dots and
    spaces, prefixes ``_`` when the first dot-segment is a reserved device
    name, and falls back to :data:`DEFAULT_BASENAME` when nothing remains.
    """
    stem = "".join(ch for ch in (name or "") if ch not in _INVALID_CHARS)
    stem = stem.strip(". ")
    if is_reserved_windows_name(stem):
        stem = "_" + stem
    return stem or DEFAULT_BASENAME


def filename_from_text(text: str, max_prefix: int = 20) -> str:
    """Derive ``<stem>.wav`` from the leading slice of ``text``."""
    prefix = (text or "").strip()[:max_prefix]
    return f"{sanitize_stem(prefix)}.wav"


def ensure_wav_suffix(name: str) -> str:
    """Normalize ``name`` to end with ``.wav``.

    Any trailing extension in :data:`_OTHER_AUDIO_EXTS` (case-insensitive) is
    removed; ``.wav`` is appended when not already present.
    """
    base = name or ""
    changed = True
    while changed:
        changed = False
        lower = base.lower()
        for ext in _OTHER_AUDIO_EXTS:
            if lower.endswith(ext):
                base = base[: -len(ext)]
                changed = True
                break
    if not base.lower().endswith(".wav"):
        base = base + ".wav"
    return base


def unique_path(path: str) -> str:
    """Return ``path`` unchanged if free, else ``<stem>_1``/``_2``/... variant."""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    counter = 1
    while True:
        candidate = f"{root}_{counter}{ext}"
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def _known_desktop() -> str:
    """Real Desktop path via the shell (OneDrive KFM-aware), else ~/Desktop."""
    try:
        from ctypes import create_unicode_buffer, windll
        buf = create_unicode_buffer(260)
        # CSIDL_DESKTOPDIRECTORY = 0x10; SHGetFolderPathW exists on every NT5+
        if windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf) == 0 and buf.value:
            return buf.value
    except Exception:  # noqa: BLE001 - fallback path is fine
        pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


def resolve_output_dir(path_input_text: str, saved_dir: str) -> str:
    """Pick the output directory.

    Returns the first existing directory among: the directory part of the
    user-typed output path, the previously saved ``output_dir``, and the
    user's Desktop. The Desktop path is the terminal fallback even when it
    does not exist yet (writers create it on demand).
    """
    candidates = (
        # Users paste "Copy as path" results that carry surrounding quotes.
        os.path.dirname((path_input_text or "").strip().strip('"')),
        saved_dir or "",
        _known_desktop(),  # OneDrive KFM-aware; ~/Desktop is a ghost on Win11
    )
    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            return os.path.normpath(candidate)
    return os.path.normpath(candidates[-1]) if candidates[-1] else candidates[-1]
