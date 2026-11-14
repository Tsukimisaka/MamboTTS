"""Small shared UI primitives that keep one visual language everywhere.

* ``ShadowLayer`` — cached, pre-rendered shadows drawn behind cards so text
  widgets keep ClearType (no per-widget graphics effects).
* ``ToggleChip`` — off = hairline pill, on = solid ink pill with a leading check.
* ``SlideSwitch`` — compact 36×20 switch used by the settings row.
* ``ThemedDialog`` — frameless modal with a shadow plate and safe focus.
* ``EmptyStateWidget`` — the one illustration allowed by the design doc.
"""

from __future__ import annotations

from collections import OrderedDict

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QDialog,
    QFrame,
    QGraphicsBlurEffect,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .icons import icon, stateful_icon
from .theme import (
    CONTROL_H,
    DIALOG_W,
    MICRO_ICON,
    R_DIALOG,
    R_LG,
    SHADOW_MARGIN_WINDOW,
    SP_LG,
    SP_MD,
    SP_SM,
    SP_XL,
    SP_XS,
    THEME,
    shadow_rgb,
    shadow_spec,
)


def _render_shadow(width: int, height: int, radius: int, level) -> QPixmap:
    """Blur a rounded rectangle once; the result is cached by ``ShadowLayer``."""
    if width <= 1 or height <= 1:
        return QPixmap()
    blur, _offset, alpha = shadow_spec(level)
    margin = blur
    base = QPixmap(width + margin * 2, height + margin * 2)
    base.fill(Qt.transparent)

    painter = QPainter(base)
    painter.setRenderHint(QPainter.Antialiasing)
    color = QColor(*shadow_rgb())
    color.setAlphaF(alpha)
    painter.setPen(Qt.NoPen)
    painter.setBrush(color)
    painter.drawRoundedRect(
        QRectF(margin, margin, width, height), float(radius), float(radius)
    )
    painter.end()

    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(base)
    effect = QGraphicsBlurEffect()
    effect.setBlurRadius(blur)
    item.setGraphicsEffect(effect)
    scene.addItem(item)

    out = QImage(base.size(), QImage.Format_ARGB32_Premultiplied)
    out.fill(Qt.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.Antialiasing)
    bounds = QRectF(0, 0, out.width(), out.height())
    scene.render(painter, bounds, bounds)
    painter.end()
    return QPixmap.fromImage(out)


class ShadowLayer(QWidget):
    """Transparent sibling layer that paints cached shadows under cards."""

    _MAX_CACHE_ENTRIES = 8

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("ShadowLayer")
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.NoFocus)
        self._entries: dict[QWidget, tuple[object, int]] = {}
        self._cache: OrderedDict[tuple, QPixmap] = OrderedDict()
        parent.installEventFilter(self)
        self.setGeometry(parent.rect())
        self.lower()

    def register(self, widget: QWidget, level=1, radius: int = R_LG) -> None:
        self._entries[widget] = (level, radius)
        widget.installEventFilter(self)
        widget.destroyed.connect(lambda _obj=None, w=widget: self._entries.pop(w, None))
        self.update()

    def set_level(self, widget: QWidget, level) -> None:
        entry = self._entries.get(widget)
        if entry is not None:
            self._entries[widget] = (level, entry[1])
            self.update()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt API
        parent = self.parentWidget()
        if obj is parent and event.type() == QEvent.Resize:
            self.setGeometry(parent.rect())
            self.update()
        elif obj in self._entries and event.type() in (
            QEvent.Move,
            QEvent.Resize,
            QEvent.Show,
            QEvent.Hide,
        ):
            self.update()
        return super().eventFilter(obj, event)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        for widget, (level, radius) in list(self._entries.items()):
            try:
                if not widget.isVisible() or widget.width() <= 1 or widget.height() <= 1:
                    continue
                rect = widget.geometry()
            except RuntimeError:
                self._entries.pop(widget, None)
                continue
            blur, offset, _alpha = shadow_spec(level)
            key = (rect.width(), rect.height(), radius, level)
            pixmap = self._cache.get(key)
            if pixmap is None:
                pixmap = _render_shadow(rect.width(), rect.height(), radius, level)
                self._cache[key] = pixmap
                while len(self._cache) > self._MAX_CACHE_ENTRIES:
                    self._cache.popitem(last=False)
            else:
                self._cache.move_to_end(key)
            painter.drawPixmap(rect.x() - blur, rect.y() - blur + offset, pixmap)
        painter.end()


def attach_shadow(widget: QWidget, level=1, radius: int = R_LG):
    """Ensure a shadow layer exists on ``widget``'s parent and register it."""
    parent = widget.parentWidget()
    if parent is None:
        return None
    layer = next(
        (child for child in parent.children() if isinstance(child, ShadowLayer)),
        None,
    )
    if layer is None:
        layer = ShadowLayer(parent)
    layer.register(widget, level, radius)
    layer.lower()
    return layer


def set_shadow_level(widget: QWidget, level) -> None:
    parent = widget.parentWidget()
    if parent is None:
        return
    for child in parent.children():
        if isinstance(child, ShadowLayer):
            child.set_level(widget, level)
            return


class ToggleChip(QPushButton):
    """Checkable pill: off = hairline pill, on = solid ink pill + check glyph."""

    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setObjectName("ToggleChip")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(CONTROL_H)
        self.setIconSize(QSize(MICRO_ICON, MICRO_ICON))
        self.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.toggled.connect(self.sync_check_icon)
        self.sync_check_icon(self.isChecked())

    def sync_check_icon(self, checked: bool) -> None:
        """Re-render the leading check glyph on toggle / enable changes."""
        if not checked:
            self.setIcon(QIcon())
            return
        t = THEME
        color = t["btn_primary_text"] if self.isEnabled() else t["dim"]
        self.setIcon(icon("check", color, MICRO_ICON, width=2.4))


class SlideSwitch(QAbstractButton):
    """36×20 sliding switch; on = ink track + paper knob, off = warm stone track."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFixedSize(36, 20)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return QSize(36, 20)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        track = QRectF(0.0, 0.0, 36.0, 20.0)
        enabled = self.isEnabled()
        if self.isChecked():
            track_color = QColor(THEME["btn_primary"] if enabled else THEME["disabled"])
        else:
            track_color = QColor("#DCD5C8" if enabled else THEME["surface2"])
        painter.setPen(Qt.NoPen)
        painter.setBrush(track_color)
        painter.drawRoundedRect(track, 10.0, 10.0)

        knob = QColor(THEME["paper"] if enabled else THEME["surface"])
        painter.setBrush(knob)
        x = 18.0 if self.isChecked() else 3.0
        painter.drawEllipse(QPointF(x + 7.0, 10.0), 7.0, 7.0)

        if self.hasFocus():
            painter.setPen(QPen(QColor(THEME["border_active"]), 1.0))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(track.adjusted(0.5, 0.5, -0.5, -0.5), 10.0, 10.0)
        painter.end()


class ElidedLabel(QLabel):
    """Single-line label that elides at paint time instead of clipping."""

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        *,
        mode: Qt.TextElideMode = Qt.ElideRight,
    ):
        super().__init__(text, parent)
        self._full_text = text
        self._mode = mode
        self.setToolTip(text)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def setFullText(self, text: str) -> None:  # noqa: N802 - Qt naming style
        self._full_text = text or ""
        super().setText(self._full_text)
        self.setToolTip(self._full_text)
        self.update()

    def setText(self, text: str) -> None:  # noqa: N802 - Qt API
        self.setFullText(text)

    def fullText(self) -> str:  # noqa: N802 - Qt naming style
        return self._full_text

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.setFont(self.font())
        rect = self.contentsRect()
        elided = self.fontMetrics().elidedText(
            self._full_text, self._mode, max(0, rect.width())
        )
        painter.drawText(rect, int(self.alignment().value), elided)


class _PaperSlider(QSlider):
    """Custom-painted 3px horizontal slider avoiding Windows Vista handle clipping."""

    _INSET = 6.0

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        fill_token: str = "accent",
        groove_hex: str = "#E2DCD0",
        always_show_knob: bool = False,
        knob_radius: float = 5.0,
    ):
        super().__init__(Qt.Horizontal, parent)
        self._fill_token = fill_token
        self._groove_hex = groove_hex
        self._always_show_knob = always_show_knob
        self._knob_radius = knob_radius
        self.setMinimumHeight(20)
        self.setMouseTracking(True)
        self.valueChanged.connect(self.update)
        self.rangeChanged.connect(lambda *_: self.update())

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return QSize(180, 20)

    def _track_bounds(self) -> tuple[float, float]:
        return self._INSET, max(self._INSET + 1.0, float(self.width()) - self._INSET)

    def _value_x(self) -> float:
        left, right = self._track_bounds()
        span = max(1, self.maximum() - self.minimum())
        ratio = (self.value() - self.minimum()) / span
        return left + ratio * (right - left)

    def _value_at(self, x: float) -> int:
        left, right = self._track_bounds()
        ratio = min(1.0, max(0.0, (x - left) / (right - left)))
        span = self.maximum() - self.minimum()
        return int(round(self.minimum() + ratio * span))

    def _should_draw_knob(self) -> bool:
        if not self.isEnabled():
            return self._always_show_knob
        if self._always_show_knob:
            return True
        return self.maximum() > self.minimum() and (
            self.underMouse() or self.isSliderDown() or self.value() > self.minimum()
        )

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        left, right = self._track_bounds()
        cy = float(self.height()) / 2.0

        groove = QColor(self._groove_hex)
        if not self.isEnabled():
            groove.setAlphaF(0.55)
        painter.setPen(Qt.NoPen)
        painter.setBrush(groove)
        painter.drawRoundedRect(
            QRectF(left, cy - 1.5, max(0.0, right - left), 3.0), 1.5, 1.5
        )

        fill_color = QColor(THEME[self._fill_token] if self.isEnabled() else THEME["disabled"])
        x = self._value_x()
        if x > left:
            painter.setBrush(fill_color)
            painter.drawRoundedRect(
                QRectF(left, cy - 1.5, max(0.0, x - left), 3.0), 1.5, 1.5
            )

        if self._should_draw_knob():
            radius = self._knob_radius + (0.6 if self.underMouse() or self.isSliderDown() else 0.0)
            painter.setPen(QPen(fill_color, 1.8))
            painter.setBrush(QColor(THEME["paper"]))
            painter.drawEllipse(QPointF(x, cy), radius, radius)
        painter.end()

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().leaveEvent(event)
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.LeftButton and self.isEnabled():
            self.setSliderDown(True)
            self.setValue(self._value_at(event.position().x()))
            self.sliderPressed.emit()
            self.sliderMoved.emit(self.value())
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self.isSliderDown():
            self.setValue(self._value_at(event.position().x()))
            self.sliderMoved.emit(self.value())
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self.isSliderDown():
            self.setValue(self._value_at(event.position().x()))
            self.sliderMoved.emit(self.value())
            self.setSliderDown(False)
            self.sliderReleased.emit()
            return
        super().mouseReleaseEvent(event)


class SeekSlider(_PaperSlider):
    """Player seek bar painted by hand with crisp vermilion progress."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(
            parent,
            fill_token="accent",
            groove_hex="#E5DFD3",
            always_show_knob=False,
            knob_radius=5.0,
        )
        self.setObjectName("SeekSlider")


class WarmSpeedSlider(_PaperSlider):
    """Hand-painted 3px charcoal ink speed slider with persistent circular handle."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(
            parent,
            fill_token="text",
            groove_hex="#DDD6C7",
            always_show_knob=True,
            knob_radius=5.0,
        )
        self.setObjectName("SpeedSlider")


class ModuleHeaderBar(QFrame):
    """Unified top header strip used by ModuleCard, ScriptEditorWidget, and dialogs."""

    def __init__(
        self,
        title: str,
        subtitle: str = "",
        *,
        dot_color: str = "",
        height: int = 44,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("ModuleCardHeader")
        self.setFixedHeight(height)

        row = QHBoxLayout(self)
        row.setContentsMargins(SP_LG, 0, SP_LG, 0)
        row.setSpacing(SP_SM)

        self.dot_frame = QFrame()
        self.dot_frame.setFixedSize(8, 8)
        row.addWidget(self.dot_frame, 0, Qt.AlignVCenter)
        self.set_dot_color(dot_color)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("SectionTitle")
        row.addWidget(self.title_label, 0, Qt.AlignVCenter)

        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setObjectName("WeakMeta")
        self.subtitle_label.setVisible(bool(subtitle))
        row.addWidget(self.subtitle_label, 0, Qt.AlignVCenter)

        row.addStretch(1)

        self.header_right = QHBoxLayout()
        self.header_right.setContentsMargins(0, 0, 0, 0)
        self.header_right.setSpacing(SP_SM)
        row.addLayout(self.header_right)

    def add_right_widget(self, widget: QWidget) -> None:
        self.header_right.addWidget(widget, 0, Qt.AlignVCenter)

    def set_dot_color(self, color: str) -> None:
        has_color = bool(color)
        self.dot_frame.setVisible(has_color)
        if has_color:
            self.dot_frame.setStyleSheet(
                f"QFrame {{ background: {color}; border: none; border-radius: 4px; }}"
            )

    def set_subtitle(self, text: str) -> None:
        self.subtitle_label.setText(text)
        self.subtitle_label.setVisible(bool(text))


class ModuleCard(QFrame):
    """Bordered studio module container with header bar and padded body layout."""

    def __init__(
        self,
        title: str,
        subtitle: str = "",
        *,
        dot_color: str = "",
        header_height: int = 44,
        body_margins: tuple[int, int, int, int] = (SP_LG, SP_MD, SP_LG, SP_MD),
        body_spacing: int = SP_SM,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("ModuleCard")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)

        self.header_bar = ModuleHeaderBar(
            title, subtitle, dot_color=dot_color, height=header_height, parent=self
        )
        outer.addWidget(self.header_bar)

        self.body_widget = QWidget()
        self.body_widget.setObjectName("TransparentSlot")
        self.body_layout = QVBoxLayout(self.body_widget)
        self.body_layout.setContentsMargins(*body_margins)
        self.body_layout.setSpacing(body_spacing)
        outer.addWidget(self.body_widget, 1)

    @property
    def dot_frame(self) -> QFrame:
        return self.header_bar.dot_frame

    @property
    def title_label(self) -> QLabel:
        return self.header_bar.title_label

    @property
    def subtitle_label(self) -> QLabel:
        return self.header_bar.subtitle_label

    @property
    def header_right(self) -> QHBoxLayout:
        return self.header_bar.header_right

    def add_header_widget(self, widget: QWidget) -> None:
        self.header_bar.add_right_widget(widget)

    def set_dot_color(self, color: str) -> None:
        self.header_bar.set_dot_color(color)

    def set_subtitle(self, text: str) -> None:
        self.header_bar.set_subtitle(text)


class EmptyStateWidget(QWidget):
    """Soft centered empty card + calm editorial guidance for empty history."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SP_MD, SP_MD, SP_MD, SP_MD)
        outer.setAlignment(Qt.AlignCenter)

        card = QFrame()
        card.setObjectName("SubCard")
        outer.addWidget(card)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(SP_XL, SP_XL, SP_XL, SP_XL)
        layout.setSpacing(SP_SM)
        layout.setAlignment(Qt.AlignCenter)

        circle_row = QHBoxLayout()
        circle_row.setContentsMargins(0, 0, 0, 0)
        circle = QFrame()
        circle.setObjectName("EmptyCircle")
        circle.setFixedSize(64, 64)
        circle_inner = QVBoxLayout(circle)
        circle_inner.setContentsMargins(0, 0, 0, 0)
        self.art = QLabel()
        self.art.setAlignment(Qt.AlignCenter)
        circle_inner.addWidget(self.art)
        circle_row.addStretch(1)
        circle_row.addWidget(circle)
        circle_row.addStretch(1)
        layout.addLayout(circle_row)
        layout.addSpacing(SP_XS)

        self.title = QLabel("还没有合成记录")
        self.title.setObjectName("EmptyHint")
        self.title.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.title)

        self.subtitle = QLabel("写好脚本，按 Ctrl+Enter 录下第一段台词")
        self.subtitle.setObjectName("EmptyHintSub")
        self.subtitle.setWordWrap(True)
        self.subtitle.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.subtitle)

        pixmap = icon("scribe", THEME["dim"], 24, width=1.4).pixmap(24, 24)
        self.art.setPixmap(pixmap)


class ThemedDialog(QDialog):
    """Frameless modal with a shadow plate and a safe initial focus."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        title: str,
        subtitle: str = "",
        dot_color: str = "",
        width: int = DIALOG_W,
    ):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        # ``width`` is the visible panel width; the transparency margin sits outside.
        self.setFixedWidth(width + SHADOW_MARGIN_WINDOW * 2)
        self.first_focus_widget: QWidget | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            SHADOW_MARGIN_WINDOW,
            SHADOW_MARGIN_WINDOW,
            SHADOW_MARGIN_WINDOW,
            SHADOW_MARGIN_WINDOW,
        )
        outer.setSpacing(0)

        self.panel = QFrame()
        self.panel.setObjectName("DialogPanel")
        outer.addWidget(self.panel)
        self._shadow_layer = attach_shadow(self.panel, "focus", R_DIALOG)

        panel_root = QVBoxLayout(self.panel)
        panel_root.setContentsMargins(1, 1, 1, 1)
        panel_root.setSpacing(0)

        self.header_bar = ModuleHeaderBar(
            title,
            subtitle,
            dot_color=dot_color,
            height=48,
            parent=self.panel,
        )
        self.btn_close = QPushButton()
        self.btn_close.setObjectName("GhostBtn")
        self.btn_close.setFixedSize(28, 28)
        self.btn_close.setFocusPolicy(Qt.NoFocus)
        self.btn_close.setToolTip("关闭")
        self.btn_close.setIcon(
            stateful_icon("close", THEME["muted"], THEME["text"], size=14)
        )
        self.btn_close.clicked.connect(self.reject)
        self.header_bar.add_right_widget(self.btn_close)
        panel_root.addWidget(self.header_bar)

        content = QWidget()
        content.setObjectName("TransparentSlot")
        panel_layout = QVBoxLayout(content)
        panel_layout.setContentsMargins(SP_XL, SP_LG, SP_XL, SP_XL)
        panel_layout.setSpacing(SP_MD)
        panel_root.addWidget(content)

        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(SP_MD)
        panel_layout.addLayout(self.body)

        self.footer = QHBoxLayout()
        self.footer.setContentsMargins(0, SP_SM, 0, 0)
        self.footer.setSpacing(SP_SM)
        self.footer.addStretch(1)
        panel_layout.addLayout(self.footer)

        self._drag_offset = None

    # ------------------------------------------------------------------

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        QTimer.singleShot(0, self._apply_initial_focus)

    def _apply_initial_focus(self) -> None:
        if self.first_focus_widget is not None:
            self.first_focus_widget.setFocus()
            return
        for cls in (QLineEdit, QPlainTextEdit, QComboBox):
            widget = self.panel.findChild(cls)
            if widget is not None and widget.isVisible():
                widget.setFocus()
                return
        for button in self.panel.findChildren(QPushButton):
            if button is not self.btn_close and button.isVisible():
                button.setFocus()
                return

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._drag_offset = None
        super().mouseReleaseEvent(event)


__all__ = [
    "ElidedLabel",
    "EmptyStateWidget",
    "ModuleCard",
    "ModuleHeaderBar",
    "SeekSlider",
    "ShadowLayer",
    "SlideSwitch",
    "ThemedDialog",
    "ToggleChip",
    "WarmSpeedSlider",
    "attach_shadow",
    "set_shadow_level",
]
