"""Infrastructure layer utilities."""

from __future__ import annotations

import sys
from typing import TextIO

__all__ = ["reconfigure_stdio_utf8", "safe_print"]


def reconfigure_stdio_utf8() -> None:
    """Best-effort switch of stdout/stderr to UTF-8 with replacement on narrow consoles."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def safe_print(
    message: object = "",
    *,
    file: TextIO | None = None,
    flush: bool = False,
) -> None:
    """Print text without crashing when the target stream uses a narrow encoding (e.g. cp1252)."""
    stream = sys.stdout if file is None else file
    if stream is None:
        return
    text = str(message)
    try:
        print(text, file=stream, flush=flush)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "ascii"
        try:
            safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        except LookupError:
            safe_text = text.encode("ascii", errors="replace").decode("ascii", errors="replace")
        print(safe_text, file=stream, flush=flush)
