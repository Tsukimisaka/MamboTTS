"""Audio-player-ready history cards & structured log buffer (spec 21).

- ``HistoryItemWidget``: one synthesized-output row; handles ``missing=True``
  records with a quiet danger tag (title stays intact), an expandable full-text
  viewer (max 160px, 150ms height animation) and a "playing" state that draws a
  2px vermilion rule on the left.
- ``StructuredLogBuffer``: bounded ``(timestamp, level, message)`` ring buffer
  whose ``render_all`` re-renders the whole log cleanly without losing entries.
"""

from __future__ import annotations

import html
import threading
import time
from datetime import datetime, timedelta
from typing import Callable

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from mambotts.domain.models import HistoryRecord

from .icons import icon, stateful_icon
from .theme import SP_MD, SP_SM, THEME, log_color, log_tag_color
from .widgets import ElidedLabel

_BOTTOM_STICK_EPSILON = 6
_DETAIL_ANIM_MS = 150
_DETAIL_MAX_H = 160


# ---------------------------------------------------------------------------
# Shared display helpers
# ---------------------------------------------------------------------------


def friendly_time(timestamp: str) -> str:
    """``2026-11-06 14:32[:00]`` -> ``今天 14:32`` / ``11-04 09:10``."""
    raw = (timestamp or "").strip()
    moment = None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            moment = datetime.strptime(raw, fmt)
            break
        except (TypeError, ValueError):
            continue
    if moment is None:
        return timestamp or "--"
    today = datetime.now().date()
    day = moment.date()
    if day == today:
        return f"今天 {moment:%H:%M}"
    if day == today - timedelta(days=1):
        return f"昨天 {moment:%H:%M}"
    return f"{moment:%m-%d %H:%M}"


# ---------------------------------------------------------------------------
# History row
# ---------------------------------------------------------------------------


class HistoryItemWidget(QFrame):
    """One history record row (play / save / delete / expand)."""

    def __init__(
        self,
        record: HistoryRecord,
        play_cb: Callable[[HistoryRecord], None] | None = None,
        save_cb: Callable[[HistoryRecord], None] | None = None,
        delete_cb: Callable[[HistoryRecord], None] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("HistoryItem")
        self.record = record
        self._play_cb = play_cb
        self._save_cb = save_cb
        self._delete_cb = delete_cb
        self._playing = False
        self._detail_open = False
        self._detail_anim: QPropertyAnimation | None = None

        outer = QHBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(SP_SM)

        self._play_bar = QFrame()
        self._play_bar.setObjectName("HistoryPlayBar")
        self._play_bar.setFixedSize(3, 32)
        self._play_bar.setVisible(False)
        outer.addWidget(self._play_bar, 0, Qt.AlignVCenter)

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        outer.addLayout(layout, 1)

        title_row = QHBoxLayout()
        title_row.setSpacing(4)
        self.title_label = ElidedLabel(self._title_text())
        self.title_label.setObjectName("HistoryText")
        self.title_label.setWordWrap(False)
        self.title_label.setToolTip(self._title_text())
        # Ignored width: a long one-line title must never stretch the card
        # past the scroll viewport (which would clip the row action buttons).
        self.title_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        title_row.addWidget(self.title_label, 1)

        self.play_btn = QPushButton()
        self.play_btn.setObjectName("HisPlay")
        self.play_btn.setFixedSize(24, 24)
        self.play_btn.setCursor(Qt.PointingHandCursor)
        self.play_btn.setToolTip("播放")
        self.play_btn.clicked.connect(self._on_play)
        title_row.addWidget(self.play_btn)

        self.save_btn = QPushButton()
        self.save_btn.setObjectName("HisSave")
        self.save_btn.setFixedSize(24, 24)
        self.save_btn.setCursor(Qt.PointingHandCursor)
        self.save_btn.setToolTip("另存")
        self.save_btn.clicked.connect(self._on_save)
        title_row.addWidget(self.save_btn)

        self.delete_btn = QPushButton()
        self.delete_btn.setObjectName("HisDel")
        self.delete_btn.setFixedSize(24, 24)
        self.delete_btn.setCursor(Qt.PointingHandCursor)
        self.delete_btn.setToolTip("从历史移除（不删除音频文件）")
        self.delete_btn.clicked.connect(self._on_delete)
        title_row.addWidget(self.delete_btn)

        self.btn_expand = QPushButton()
        self.btn_expand.setObjectName("HisExpand")
        self.btn_expand.setFixedSize(24, 24)
        self.btn_expand.setCursor(Qt.PointingHandCursor)
        self.btn_expand.setToolTip("详情")
        self.btn_expand.clicked.connect(self._toggle_detail)
        title_row.addWidget(self.btn_expand)
        layout.addLayout(title_row)

        meta_row = QHBoxLayout()
        meta_row.setSpacing(SP_SM)
        self.meta_label = ElidedLabel(self._meta_text())
        self.meta_label.setObjectName("HistoryMeta")
        self.meta_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        meta_row.addWidget(self.meta_label, 1)

        self.playing_tag = QLabel("正在试听")
        self.playing_tag.setObjectName("BadgeVerm")
        self.playing_tag.setFixedHeight(20)
        self.playing_tag.setAlignment(Qt.AlignCenter)
        self.playing_tag.setVisible(False)
        meta_row.addWidget(self.playing_tag)

        self.missing_tag = QLabel("文件已移动")
        self.missing_tag.setObjectName("HistoryMissing")
        self.missing_tag.setFixedHeight(20)
        self.missing_tag.setAlignment(Qt.AlignCenter)
        self.missing_tag.setToolTip("音频文件已被移动或删除，播放与另存不可用")
        self.missing_tag.setVisible(bool(record.missing))
        meta_row.addWidget(self.missing_tag)
        layout.addLayout(meta_row)

        # Expandable full-text viewer (collapsed by default, max 160px).
        self._detail_scroll = QScrollArea()
        self._detail_scroll.setObjectName("HistoryDetail")
        self._detail_scroll.setWidgetResizable(True)
        self._detail_scroll.setMaximumHeight(0)
        self._detail_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        detail_body = QWidget()
        detail_body.setObjectName("HistoryDetailBody")
        detail_layout = QVBoxLayout(detail_body)
        detail_layout.setContentsMargins(SP_MD, SP_MD, SP_MD, SP_MD)
        detail_layout.setSpacing(0)
        self._full_label = QLabel()
        self._full_label.setObjectName("HistoryFull")
        self._full_label.setTextFormat(Qt.RichText)
        self._full_label.setWordWrap(True)
        self._set_detail_text(record.text or "（无文本）")
        detail_layout.addWidget(self._full_label)
        self._detail_scroll.setWidget(detail_body)
        self._detail_scroll.setVisible(False)
        layout.addWidget(self._detail_scroll)

        self._apply_missing_state()
        self._apply_history_icons()

    # ------------------------------------------------------------------

    def _title_text(self) -> str:
        text = (self.record.text or "").strip().replace("\n", " ")
        if len(text) > 60:
            text = text[:60] + "…"
        return text or "(无文本)"

    def _meta_text(self) -> str:
        duration = f"{self.record.duration:.1f}s" if self.record.duration else "--"
        parts = [friendly_time(self.record.time), duration]
        if self.record.speed and self.record.speed != 1.0:
            parts.append(f"{self.record.speed:.2f}x")
        return " · ".join(parts)

    def _set_detail_text(self, text: str) -> None:
        body = html.escape(text).replace("\n", "<br>")
        self._full_label.setText(f'<div style="line-height:160%;">{body}</div>')

    def _apply_missing_state(self) -> None:
        missing = bool(self.record.missing)
        self.missing_tag.setVisible(missing)
        self.playing_tag.setVisible(self._playing and not missing)
        self.play_btn.setEnabled(not missing)
        self.save_btn.setEnabled(not missing)
        self.delete_btn.setEnabled(True)
        self.title_label.setProperty("missing", "true" if missing else "false")
        self._repolish(self.title_label)

    def set_missing(self, missing: bool) -> None:
        """Update missing-file state on the fly when an operation detects a missing file."""
        self.record.missing = bool(missing)
        self._apply_missing_state()

    def _apply_history_icons(self) -> None:
        """Tint row icons for the live palette (quiet at rest until hovered)."""
        t = THEME
        dim, text = t["dim"], t["text"]
        if self._playing:
            self.play_btn.setIcon(icon("pause", t["accent"], 13))
            self.play_btn.setToolTip("正在播放")
        else:
            self.play_btn.setIcon(
                stateful_icon("play", dim, text, dim, size=13)
            )
            self.play_btn.setToolTip("播放")
        self.save_btn.setIcon(
            stateful_icon("download", dim, text, dim, size=13)
        )
        self.delete_btn.setIcon(
            stateful_icon("trash", dim, t["danger"], dim, size=13)
        )
        expanded = self._detail_open
        self.btn_expand.setIcon(
            stateful_icon(
                "chevron-up" if expanded else "chevron-down",
                dim,
                text,
                dim,
                size=13,
            )
        )

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()

    def set_playing(self, playing: bool) -> None:
        """Mark the row as currently playing (paper surface + vermilion bookmark)."""
        value = "true" if playing else "false"
        if self.property("playing") == value and self._playing == bool(playing):
            return
        self._playing = bool(playing)
        self.setProperty("playing", value)
        self.title_label.setProperty("playing", value)
        self._play_bar.setVisible(self._playing)
        self.playing_tag.setVisible(self._playing and not bool(self.record.missing))
        self.meta_label.setFullText(self._meta_text())
        self._apply_history_icons()
        self._repolish(self)
        self._repolish(self.title_label)
        self._repolish(self.meta_label)

    def _toggle_detail(self) -> None:
        self._detail_open = not self._detail_open
        self.btn_expand.setToolTip("收起" if self._detail_open else "详情")
        self._apply_history_icons()
        self._animate_detail(self._detail_open)

    def _animate_detail(self, opening: bool) -> None:
        previous = self._detail_anim
        if previous is not None:
            previous.stop()
        if opening:
            self._detail_scroll.setVisible(True)
        anim = QPropertyAnimation(self._detail_scroll, b"maximumHeight", self)
        anim.setDuration(_DETAIL_ANIM_MS)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setStartValue(self._detail_scroll.maximumHeight() if opening else _DETAIL_MAX_H)
        anim.setEndValue(_DETAIL_MAX_H if opening else 0)
        if not opening:
            anim.finished.connect(self._detail_scroll.hide)
        self._detail_anim = anim
        anim.start()

    def _on_play(self) -> None:
        if not self.record.missing and self._play_cb is not None:
            self._play_cb(self.record)

    def _on_save(self) -> None:
        if not self.record.missing and self._save_cb is not None:
            self._save_cb(self.record)

    def _on_delete(self) -> None:
        if self._delete_cb is not None:
            self._delete_cb(self.record)


# ---------------------------------------------------------------------------
# Structured log buffer
# ---------------------------------------------------------------------------


class StructuredLogBuffer:
    """Bounded in-memory log of ``(timestamp, level, message)`` tuples.

    ``render_all`` re-renders *everything* from stored entries without clearing
    them.  Auto-scrolls only when the view was already at the bottom.
    """

    def __init__(self, max_entries: int = 2000):
        self._max_entries = max(1, int(max_entries))
        self._entries: list[tuple[str, str, str]] = []
        self._lock = threading.Lock()

    def append(self, level: str, message: str) -> tuple[str, str, str]:
        """Store one entry (thread-safe); drops the oldest beyond the cap."""
        entry = (time.strftime("%H:%M:%S"), str(level), str(message))
        with self._lock:
            self._entries.append(entry)
            if len(self._entries) > self._max_entries:
                del self._entries[: len(self._entries) - self._max_entries]
        return entry

    def entries(self) -> list[tuple[str, str, str]]:
        with self._lock:
            return list(self._entries)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    # ------------------------------------------------------------------

    def _format_line(self, ts: str, level: str, message: str) -> str:
        tag = {"ok": "OK", "warn": "警告", "error": "错误", "engine": "引擎"}.get(
            level, "信息"
        )
        tag_color = log_tag_color(level)
        msg_color = log_color(level)
        dim = THEME["dim"]
        return (
            f'<span style="color:{dim};">[{html.escape(ts)}]</span> '
            f'<span style="color:{tag_color};">'
            f"{html.escape(tag)}</span> "
            f'<span style="color:{msg_color};">{html.escape(message)}</span>'
        )

    def render_line(self, text_edit: QPlainTextEdit, entry: tuple[str, str, str]) -> None:
        """Append a single formatted entry (incremental logging path)."""
        if text_edit is None:
            return
        scrollbar = text_edit.verticalScrollBar()
        stick = scrollbar.value() >= scrollbar.maximum() - _BOTTOM_STICK_EPSILON
        ts, level, message = entry
        text_edit.appendHtml(self._format_line(ts, level, message))
        if stick:
            scrollbar.setValue(scrollbar.maximum())

    def render_all(self, text_edit: QPlainTextEdit) -> None:
        """Re-render every stored entry into ``text_edit`` (HTML colors).

        Never clears the buffer; the target widget is only refreshed.  Keeps
        the view pinned to the bottom if it was there before the render.
        """
        if text_edit is None:
            return
        scrollbar = text_edit.verticalScrollBar()
        stick = scrollbar.value() >= scrollbar.maximum() - _BOTTOM_STICK_EPSILON
        text_edit.clear()
        for ts, level, message in self.entries():
            text_edit.appendHtml(self._format_line(ts, level, message))
        if stick:
            scrollbar.setValue(scrollbar.maximum())
