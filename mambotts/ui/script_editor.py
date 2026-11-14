"""Script input editor & live count / duration estimate."""

from __future__ import annotations

import re

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QTextBlockFormat, QTextCursor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from mambotts.domain.models import SPEED_MIN

from .theme import FS_PAPER, SP_LG, SP_SM, THEME
from .widgets import ModuleHeaderBar

_CHARS_PER_SECOND_1X = 4.5
_LINE_HEIGHT = 175.0

_PLACEHOLDER_LINES = (
    "在这里写下配音文案，使用正常标点即可自然停顿与断句。",
    "支持随时调节下方语速；完成后按 Ctrl+Enter 或点击「合成」导出音频。",
)


def _apply_all_block_formats(document) -> None:
    fmt = QTextBlockFormat()
    fmt.setLineHeight(_LINE_HEIGHT, QTextBlockFormat.ProportionalHeight.value)
    cursor = QTextCursor(document)
    cursor.beginEditBlock()
    block = document.begin()
    while block.isValid():
        block_cursor = QTextCursor(block)
        block_cursor.setBlockFormat(fmt)
        block = block.next()
    cursor.endEditBlock()


def _needs_line_height(document) -> bool:
    expected_type = getattr(
        QTextBlockFormat.ProportionalHeight, "value", QTextBlockFormat.ProportionalHeight
    )
    block = document.begin()
    while block.isValid():
        fmt = block.blockFormat()
        current_type = getattr(fmt.lineHeightType(), "value", fmt.lineHeightType())
        if current_type != expected_type:
            return True
        if abs(fmt.lineHeight() - _LINE_HEIGHT) > 0.1:
            return True
        block = block.next()
    return False


class ScriptTextEdit(QTextEdit):
    """Plain-text editor whose paste/replacement keeps the document line spacing."""

    def setPlainText(self, text: str) -> None:  # noqa: N802 - Qt API
        self.blockSignals(True)
        super().setPlainText(text)
        _apply_all_block_formats(self.document())
        self.blockSignals(False)
        self.textChanged.emit()

    def insertFromMimeData(self, source) -> None:  # noqa: N802 - Qt API
        # Keep the paste and the block formatting in one undo step.
        self.blockSignals(True)
        cursor = self.textCursor()
        cursor.beginEditBlock()
        super().insertFromMimeData(source)
        _apply_all_block_formats(self.document())
        cursor.endEditBlock()
        self.blockSignals(False)
        self.textChanged.emit()


_DEFAULT_SUBTITLE = "使用中文/英文正常标点控制自然停顿"
_LOCKED_SUBTITLE = "合成期间锁定修改"


class ScriptEditorWidget(QWidget):
    """Script editor with live character count and duration estimate."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._speed = 1.0
        self._formatting = False
        self._locked = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header_bar = ModuleHeaderBar(
            "配音脚本文案",
            _DEFAULT_SUBTITLE,
            height=44,
            parent=self,
        )

        self.btn_clear = QPushButton("清空")
        self.btn_clear.setObjectName("SoftBtn")
        self.btn_clear.setFixedHeight(24)
        self.btn_clear.setFocusPolicy(Qt.NoFocus)
        self.btn_clear.setToolTip("清空当前脚本文案")
        self.btn_clear.clicked.connect(self.clear_text)
        self.btn_clear.setVisible(False)
        self.header_bar.add_right_widget(self.btn_clear)

        stats_pill = QFrame()
        stats_pill.setObjectName("BadgeNeutral")
        stats_pill.setFixedHeight(24)
        stats_row = QHBoxLayout(stats_pill)
        stats_row.setContentsMargins(SP_SM + 2, 0, SP_SM + 2, 0)
        stats_row.setSpacing(4)
        self.count_label = QLabel("0 字")
        self.count_label.setObjectName("PaperCount")
        stats_row.addWidget(self.count_label)
        sep = QLabel("·")
        sep.setObjectName("PaperCount")
        stats_row.addWidget(sep)
        self.estimate_label = QLabel("约 0 秒")
        self.estimate_label.setObjectName("PaperCount")
        stats_row.addWidget(self.estimate_label)
        self.header_bar.add_right_widget(stats_pill)

        # Keep key_hint populated for test/shortcut introspection; visual cue
        # lives beside the single primary CTA in the desk action strip.
        self.key_hint = QLabel("Ctrl+Enter", self)
        self.key_hint.setVisible(False)
        layout.addWidget(self.header_bar)

        body = QWidget()
        body.setObjectName("TransparentSlot")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(SP_LG, 8, SP_LG, 10)
        body_layout.setSpacing(0)

        self.text_input = ScriptTextEdit()
        self.text_input.setObjectName("ScriptInput")
        self.text_input.document().setDocumentMargin(0)
        self.text_input.setAcceptRichText(False)
        self.text_input.setMinimumHeight(100)
        self.text_input.setCursorWidth(2)
        self.text_input.textChanged.connect(self._on_text_changed)
        body_layout.addWidget(self.text_input, 1)
        layout.addWidget(body, 1)

        # QTextEdit's native placeholder only renders one plain line cleanly,
        # so multi-line warm-ink guidance uses a transparent viewport overlay.
        self._placeholder = QLabel(self.text_input.viewport())
        self._placeholder.setObjectName("EditorPlaceholder")
        self._placeholder.setTextFormat(Qt.RichText)
        self._placeholder.setWordWrap(True)
        self._placeholder.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self._placeholder.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._placeholder.setStyleSheet("background: transparent;")
        self.text_input.viewport().installEventFilter(self)
        self._refresh_placeholder()
        self._sync_placeholder()

        self._apply_line_height()
        self._update_count()

    # ------------------------------------------------------------------

    def clear_text(self) -> None:
        self.text_input.clear()
        self.text_input.setFocus()

    def set_locked(self, locked: bool) -> None:
        self._locked = bool(locked)
        self.header_bar.set_subtitle(_LOCKED_SUBTITLE if self._locked else _DEFAULT_SUBTITLE)
        self._update_clear_button()

    def _update_clear_button(self) -> None:
        has_text = bool((self.text_input.toPlainText() or "").strip())
        self.btn_clear.setVisible((not self._locked) and has_text)

    def set_speed(self, speed: float) -> None:
        self._speed = max(SPEED_MIN, float(speed))
        self._update_count()

    def _on_text_changed(self) -> None:
        self._update_count()
        self._sync_placeholder()
        if self._formatting or not self.text_input.document().isEmpty():
            return
        self._formatting = True
        try:
            self._apply_line_height()
        finally:
            self._formatting = False

    def _apply_line_height(self) -> None:
        if not _needs_line_height(self.text_input.document()):
            return
        _apply_all_block_formats(self.text_input.document())

    def _refresh_placeholder(self) -> None:
        t = THEME
        spans = [
            f'<span style="color:{t["muted"] if idx == 0 else t["dim"]};">{line}</span>'
            for idx, line in enumerate(_PLACEHOLDER_LINES)
        ]
        self._placeholder.setStyleSheet(
            f"background: transparent; font-size: {FS_PAPER}px;"
        )
        self._placeholder.setText(
            f'<div style="line-height:175%;">{"<br>".join(spans)}</div>'
        )

    def _sync_placeholder(self) -> None:
        self._placeholder.setGeometry(self.text_input.viewport().rect())
        self._placeholder.setVisible(not self.text_input.toPlainText().strip())

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt API
        if obj is self.text_input.viewport() and event.type() == QEvent.Resize:
            self._sync_placeholder()
        return super().eventFilter(obj, event)

    def _update_count(self) -> None:
        raw = self.text_input.toPlainText() or ""
        count = len(re.sub(r"\s+", "", raw))
        self.count_label.setText(f"{count} 字")
        seconds = 0 if count == 0 else max(
            1, int(round(count / (_CHARS_PER_SECOND_1X * self._speed)))
        )
        self.estimate_label.setText(f"约 {seconds} 秒")
        self._update_clear_button()
