"""Inline SVG line-icon set (no binary assets in the repo).

Every icon is a 24x24 path body rendered at a uniform stroke.  Small icons
(<14px) use a slightly heavier 1.8px stroke for crispness, large ones (>=18px)
drop to 1.5px to stay delicate; 14-17px uses the base 1.6px.  ``icon`` returns
a themed ``QIcon``; ``stateful_icon`` adds hover/disabled tints; ``svg_file``
writes a colored copy to the temp directory so QSS ``image: url(...)`` rules
(combo arrow, checkbox tick) can reference it.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

_SVG_STROKE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
    'stroke="{color}" stroke-width="{width}" stroke-linecap="round" '
    'stroke-linejoin="round">{body}</svg>'
)
_SVG_FILL = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
    'fill="{color}" stroke="none">{body}</svg>'
)

# name -> (body, filled)
_ICONS: dict[str, tuple[str, bool]] = {
    # Editorial line icons: thin strokes, rounded joins, no solid fills --
    # matches the paper/ink design language of ui/theme.py.
    "play": ('<path d="M9.4 6.7v10.6a.7.7 0 0 0 1.07.6l8.6-5.3a.7.7 0 0 0 0-1.2l-8.6-5.3a.7.7 0 0 0-1.07.6z"/>', False),
    "pause": ('<path d="M9 5.5v13M15 5.5v13"/>', False),
    "settings": ('<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>', False),
    "folder": ('<path d="M3.5 7.5a2 2 0 0 1 2-2h3.6l2 2h7.4a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>', False),
    "download": ('<path d="M12 4.5v9.5M8.5 10.5L12 14l3.5-3.5M5.5 16.2v2A1.8 1.8 0 0 0 7.3 20h9.4a1.8 1.8 0 0 0 1.8-1.8v-2"/>', False),
    "trash": ('<path d="M4.5 7h15M10 11.2v5.3M14 11.2v5.3M6.6 7l.75 10.6a2 2 0 0 0 2 1.9h5.3a2 2 0 0 0 2-1.9L17.4 7M9.7 7V4.9a1.4 1.4 0 0 1 1.4-1.4h1.8a1.4 1.4 0 0 1 1.4 1.4V7"/>', False),
    "close": ('<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>', False),
    "log": ('<path d="M5 6.5h14M5 12h14M5 17.5h9"/>', False),
    "chevron-down": ('<path d="M7 10l5 5 5-5"/>', False),
    "chevron-up": ('<path d="M7 14l5-5 5 5"/>', False),
    "chevron-right": ('<path d="M10 7l5 5-5 5"/>', False),
    "check": ('<path d="M5.5 12.5l4 4L18.5 7.5"/>', False),
    "alert": ('<path d="M12 4.2L3.3 19.4a1 1 0 0 0 .87 1.5h15.66a1 1 0 0 0 .87-1.5z"/><path d="M12 9.5v4.6M12 17.4v.1"/>', False),
    # Empty-state illustration: nib + sound wave, one continuous line drawing.
    "scribe": ('<path d="M4.2 18.6l-.8 3 3-.8L16.2 11a2 2 0 0 0-2.8-2.8z"/><path d="M12.7 9.3l2.8 2.8"/><path d="M17.6 14.2v2.2M20.3 12.4v5.6M23 14.2v2.2"/>', False),
    "gpu-chip": ('<rect x="4" y="6" width="16" height="12" rx="2"/><path d="M9 10v4M12 9v6M15 11v2M2 10h2M2 14h2M20 10h2M20 14h2"/>', False),
    "gpu-card": ('<rect x="3" y="4" width="18" height="14" rx="2"/><path d="M8 21h8M12 18v3"/>', False),
}


def _stroke_width(size: int, width: float | None) -> float:
    if width is not None:
        return width
    if size < 14:
        return 1.8
    if size >= 18:
        return 1.5
    return 1.6


def svg_markup(name: str, color: str, width: float = 1.6) -> str:
    """Return the full SVG document for ``name`` drawn in ``color``."""
    body, filled = _ICONS[name]
    template = _SVG_FILL if filled else _SVG_STROKE
    return template.format(color=color, width=width, body=body)


def _pixmap(name: str, color: str, size: int, width: float | None = None):
    from PySide6.QtCore import QByteArray, Qt
    from PySide6.QtGui import QPainter, QPixmap
    from PySide6.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(
        QByteArray(svg_markup(name, color, _stroke_width(size, width)).encode("utf-8"))
    )
    scale = 2
    pixmap = QPixmap(size * scale, size * scale)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    renderer.render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(scale)
    return pixmap


def icon(name: str, color: str, size: int = 16, width: float | None = None):
    """Render ``name`` into a crisp (2x) ``QIcon`` tinted ``color``."""
    from PySide6.QtGui import QIcon

    return QIcon(_pixmap(name, color, size, width))


def stateful_icon(
    name: str,
    normal: str,
    active: str,
    disabled: str | None = None,
    size: int = 16,
    width: float | None = None,
):
    """QIcon with hover (Active) and optional disabled pixmaps.

    Buttons automatically switch to ``QIcon::Active`` while hovered, so row
    actions can stay muted until the pointer reaches them.
    """
    from PySide6.QtGui import QIcon

    result = QIcon()
    result.addPixmap(_pixmap(name, normal, size, width), QIcon.Normal)
    result.addPixmap(_pixmap(name, active, size, width), QIcon.Active)
    if disabled is not None:
        result.addPixmap(_pixmap(name, disabled, size, width), QIcon.Disabled)
    return result


def _icon_cache_dir() -> str:
    return os.path.join(tempfile.gettempdir(), "mambotts_icons", str(os.getpid()))


def svg_file(name: str, color: str, width: float = 1.6) -> str:
    """Write a colored SVG to the temp dir; returns a QSS-safe path or ``""``."""
    try:
        folder = _icon_cache_dir()
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{name}_{color.lstrip('#')}_{width}.svg")
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(svg_markup(name, color, width))
        return path.replace("\\", "/")
    except OSError:
        return ""


def _cleanup_icon_cache() -> None:
    try:
        shutil.rmtree(_icon_cache_dir())
    except OSError:
        pass


atexit.register(_cleanup_icon_cache)
