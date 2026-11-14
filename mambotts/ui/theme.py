"""Paper / ink QSS & palette engine (spec item 16).

Single global warm-paper palette exposed as ``THEME``.  All QSS builders
read ``THEME`` directly.

Design language: hierarchy comes from whitespace, type size and 1px
hairlines rather than filled cards.  Ink is the only "strong" color; the
single accent (朱砂 vermilion) is reserved for live state.

朱砂使用边界（严格枚举，不得扩张）：
  品牌小方点 / 合成进度条 / 播放进度 / 正在播放的历史竖线 / 编辑器光标。
不得用于按钮底色、图标常态色或链接色。

Every radius, spacing, type size and shadow below comes from the token block
at the top of this module; UI code imports the same tokens so no widget
invents its own numbers.
"""

from __future__ import annotations

from .icons import svg_file

# ---------------------------------------------------------------------------
# Design tokens (4px baseline grid)
# ---------------------------------------------------------------------------

SP_XS = 4
SP_SM = 8
SP_MD = 12
SP_LG = 16
SP_XL = 24
SP_XXL = 32

R_SM = 6
R_MD = 10
R_STRIP = 12
R_LG = 16
R_SHEET = 16
R_DIALOG = 18

FS_DISPLAY = 24
FS_BRAND = 18
FS_PAPER = 17
FS_BODY = 14
FS_SECTION = 15
FS_AUX = 13
FS_MICRO = 12

# Shadow plates need this much room around the card so the blur never clips.
SHADOW_MARGIN_2 = 24
SHADOW_MARGIN_WINDOW = 32

# Fixed control metrics shared by the widgets (QSS can't express all of them).
CONTROL_H = 30
INPUT_H = 34
PRIMARY_H = 36
ICON_SIZE = 16
MICRO_ICON = 14
TOPBAR_H = 58
PLAYER_H = 60
PLAYER_BTN = 36
COL_W = 360
DIALOG_W = 400
SETUP_COL_W = 720

# ---------------------------------------------------------------------------
# Palettes
# ---------------------------------------------------------------------------

# Light theme: warm paper, ink text, one vermilion accent.
THEME: dict[str, str] = {
    "bg": "#F6F4EF",
    "surface": "#FBFAF7",
    "surface2": "#F1EEE7",
    "surface3": "#E7E2D7",
    "recessed": "#EFECE4",
    "well": "#F2EFE8",
    "paper": "#FDFCF9",
    "card_header": "#F8F6F1",
    "shadow": "60, 45, 20",
    "border": "#E6E1D7",
    "border_card": "#DDD7CA",
    "border_soft": "rgba(60, 45, 20, 0.08)",
    "hairline": "rgba(60, 45, 20, 0.07)",
    "border_hover": "#BDB5A6",
    "border_active": "#A69E8F",
    "text": "#1F1D1A",
    "muted": "#6B655E",
    "dim": "#8E877B",
    "disabled": "#BDB6A8",
    "accent": "#B5472B",
    "accent_text": "#FBFAF7",
    "accent_hover": "#9E3C23",
    "accent_pressed": "#87331E",
    "accent_tint": "#FAF2EE",
    "accent_border": "#DFC2B8",
    "btn_primary": "#1F1D1A",
    "btn_primary_text": "#FBFAF7",
    "btn_primary_hover": "#3A3631",
    "selection": "#B5D3F2",
    "selection_inactive": "#D2E2F2",
    "selection_menu": "#D7E6F5",
    "danger": "#B23A32",
    "danger_bg": "#FCECEB",
    "danger_border": "#E5BDB9",
    "warning": "#8A621A",
    "warning_bg": "#FBF3DF",
    "warning_border": "#EAD8B1",
    "ok": "#44703F",
    "ok_bg": "#EAF3E8",
    "ok_border": "#C5DBC2",
    "toast_bg": "#24211D",
}


def apply_selection_palette(app) -> None:
    """Apply clear high-contrast text selection colors on QApplication palette."""
    from PySide6.QtGui import QColor, QPalette

    if app is None:
        return
    palette = app.palette()
    active = QColor(THEME["selection"])
    inactive = QColor(THEME["selection_inactive"])
    text = QColor(THEME["text"])
    for role in (QPalette.Highlight,):
        palette.setColor(QPalette.Active, role, active)
        palette.setColor(QPalette.Inactive, role, inactive)
    for role in (QPalette.HighlightedText,):
        palette.setColor(QPalette.Active, role, text)
        palette.setColor(QPalette.Inactive, role, text)
    app.setPalette(palette)


def _rgba(hex_color: str, alpha: float) -> str:
    """``#RRGGBB`` -> ``rgba(r, g, b, a)`` for soft QSS backgrounds."""
    value = hex_color.lstrip("#")
    r = int(value[0:2], 16)
    g = int(value[2:4], 16)
    b = int(value[4:6], 16)
    return f"rgba({r}, {g}, {b}, {alpha})"


# ---------------------------------------------------------------------------
# Soft shadows (warm brown, never pure black)
# ---------------------------------------------------------------------------

# level -> (blur, vertical offset, alpha)
_SHADOW_LEVELS = {
    1: (24, 4, 0.08),
    2: (32, 8, 0.09),
    "focus": (40, 12, 0.10),
}


def shadow_spec(level) -> tuple[int, int, float]:
    """``(blur, vertical_offset, alpha)`` for a shadow level."""
    return _SHADOW_LEVELS.get(level, _SHADOW_LEVELS[1])


def shadow_rgb() -> tuple[int, int, int]:
    """Warm-brown shadow base color (never pure black)."""
    parts = [int(part.strip()) for part in THEME["shadow"].split(",")]
    return parts[0], parts[1], parts[2]


# ---------------------------------------------------------------------------
# QSS builders (all read the live THEME at call time)
# ---------------------------------------------------------------------------

_FONT_STACK = '"Microsoft YaHei UI", "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif'
_BRAND_STACK = '"Segoe UI Semibold", "Segoe UI", "Microsoft YaHei UI", "PingFang SC", sans-serif'
_NUM_STACK = '"Segoe UI Semibold", "Segoe UI", "Microsoft YaHei UI", sans-serif'
_MONO_STACK = '"Cascadia Mono", "Consolas", "Courier New", monospace'


def _url(path: str) -> str:
    if not path:
        return ""
    safe = path.replace("\\", "/").replace('"', '\\"')
    return f'image: url("{safe}");'


def base_qss() -> str:
    """Global stylesheet for the whole studio window."""
    t = THEME
    chevron = _url(svg_file("chevron-down", t["muted"], 2.4))
    tick = _url(svg_file("check", t["btn_primary_text"], 2.4))
    return f"""
* {{
    outline: none;
}}
QWidget {{
    background: {t['bg']};
    color: {t['text']};
    font-family: {_FONT_STACK};
    font-size: {FS_BODY}px;
}}
QLabel, QCheckBox, QRadioButton {{
    background: transparent;
}}
QMainWindow#MainWindow, QWidget#StudioPage {{
    background: {t['bg']};
}}
QDialog {{
    background: transparent;
}}

/* ---------- structural surfaces (modular paper cards & inner wells) ---------- */
QFrame#TopBar {{
    background: {t['bg']};
    border: none;
}}
QFrame#EnginePill {{
    background: {t['paper']};
    border: 1px solid {t['border_card']};
    border-radius: 15px;
}}
QFrame#EnginePill[state="ok"] {{
    background: {t['ok_bg']};
    border: 1px solid {t['ok_border']};
}}
QFrame#EnginePill[state="warn"] {{
    background: {t['warning_bg']};
    border: 1px solid {t['warning_border']};
}}
QFrame#EnginePill[state="error"] {{
    background: {t['danger_bg']};
    border: 1px solid {t['danger_border']};
}}
QFrame#ModuleCard {{
    background: {t['paper']};
    border: 1px solid {t['border_card']};
    border-radius: {R_SHEET}px;
}}
QFrame#ModuleCardHeader {{
    background: {t['card_header']};
    border: none;
    border-bottom: 1px solid {t['border']};
    border-top-left-radius: 15px;
    border-top-right-radius: 15px;
}}
QFrame#WellBox {{
    background: {t['well']};
    border: 1px solid {t['border']};
    border-radius: {R_STRIP}px;
}}
QFrame#SubCard {{
    background: {t['paper']};
    border: 1px solid {t['border_card']};
    border-radius: {R_STRIP}px;
}}
QFrame#PlayerBar {{
    background: {t['paper']};
    border: 1px solid {t['border_card']};
    border-radius: {R_LG}px;
}}
QFrame#Paper {{
    background: {t['paper']};
    border: 1px solid {t['border_card']};
    border-radius: {R_SHEET}px;
}}
QFrame#RecessedStrip {{
    background: {t['recessed']};
    border: none;
    border-radius: {R_STRIP}px;
}}
QFrame#LogPanel {{
    background: {t['paper']};
    border: 1px solid {t['border_card']};
    border-radius: {R_SHEET}px;
}}
QFrame#SetupCardSelected {{
    background: {t['paper']};
    border: 1.5px solid {t['text']};
    border-radius: {R_STRIP}px;
}}
QFrame#SetupCardIdle {{
    background: {t['paper']};
    border: 1px solid {t['border_card']};
    border-radius: {R_STRIP}px;
}}
QFrame#SetupCardIdle:hover {{
    background: {t['surface']};
    border-color: {t['border_hover']};
}}
QFrame#GroupedPaperCard {{
    background: {t['paper']};
    border: 1px solid {t['border_card']};
    border-radius: {R_SHEET}px;
}}
QFrame#SourceRow {{
    background: {t['paper']};
    border: 1px solid {t['border']};
    border-radius: 10px;
}}
QFrame#SourceRow:hover {{
    background: {t['surface']};
    border-color: {t['border_hover']};
}}
QFrame#SourceRow[selected="true"] {{
    background: {t['accent_tint']};
    border: 1px solid {t['accent_border']};
    border-radius: 10px;
}}
QFrame#GroupRow {{
    background: transparent;
    border: none;
    border-bottom: 1px solid {t['hairline']};
}}
QFrame#GroupRow[last="true"] {{
    border-bottom: none;
}}
QFrame#GroupRow[selected="true"] {{
    background: {t['accent_tint']};
}}
QFrame#GroupRow[selected="true"][pos="top"] {{
    border-top-left-radius: 15px;
    border-top-right-radius: 15px;
}}
QFrame#GroupRow[selected="true"][pos="bottom"] {{
    border-bottom-left-radius: 15px;
    border-bottom-right-radius: 15px;
    border-bottom: none;
}}
QFrame#GroupRow[selected="true"][pos="only"] {{
    border-radius: 15px;
    border-bottom: none;
}}
QFrame#EmptyCircle {{
    background: {t['recessed']};
    border: none;
    border-radius: 32px;
}}
QFrame#ToastDark {{
    background: {t['toast_bg']};
    border: none;
    border-radius: 17px;
}}
QWidget#ShadowLayer {{
    background: transparent;
}}
QFrame#WarningBar {{
    background: {t['warning_bg']};
    border: 1px solid {t['warning_border']};
    border-radius: {R_STRIP}px;
}}
QLabel#WarningText {{
    color: {t['warning']};
    font-size: {FS_AUX}px;
}}
QFrame#Hairline {{
    background: {t['hairline']};
    border: none;
    max-height: 1px;
    min-height: 1px;
}}
QFrame#VHairline {{
    background: #DCD5C7;
    border: none;
    max-width: 1px;
    min-width: 1px;
}}
QFrame#Popover {{
    background: {t['paper']};
    border: 1px solid {t['border_card']};
    border-radius: {R_SHEET}px;
}}
QFrame#DialogPanel {{
    background: {t['paper']};
    border: 1px solid {t['border_card']};
    border-radius: {R_DIALOG}px;
}}
QFrame#BrandDot {{
    background: {t['accent']};
    border: none;
    border-radius: 2px;
    min-width: 7px;
    max-width: 7px;
    min-height: 7px;
    max-height: 7px;
}}
QFrame#BulletDot {{
    background: {t['dim']};
    border: none;
    border-radius: 3px;
    min-width: 5px;
    max-width: 5px;
    min-height: 5px;
    max-height: 5px;
}}
QFrame#SetupFooter {{
    background: {t['paper']};
    border: none;
    border-top: 1px solid {t['border_card']};
}}
QWidget#SetupOptionRow, QWidget#SetupScrollContent {{
    background: transparent;
}}
QScrollArea#SetupScroll {{
    background: transparent;
    border: none;
}}
QWidget#VoiceBar, QWidget#OutputSummary,
QWidget#ActionRow, QWidget#ActionWide, QWidget#ActionCompact,
QWidget#TransparentSlot {{
    background: transparent;
}}
QLineEdit#CleanInput {{
    background: {t['paper']};
    color: {t['text']};
    border: 1px solid {t['border_card']};
    border-radius: 8px;
    padding: 0 10px;
}}
QLineEdit#CleanInput:hover {{
    border-color: {t['border_hover']};
    background: {t['paper']};
}}
QLineEdit#CleanInput:focus {{
    border: 1px solid {t['border_active']};
    background: {t['paper']};
}}
QLineEdit#CleanInput:disabled {{
    color: {t['disabled']};
    background: {t['surface2']};
}}

/* ---------- inputs ---------- */
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox {{
    background: {t['surface2']};
    color: {t['text']};
    border: 1px solid transparent;
    border-radius: {R_MD}px;
    padding: 6px 12px;
    selection-background-color: {t['selection']};
    selection-color: {t['text']};
}}
QLineEdit:hover, QComboBox:hover {{
    background: {t['recessed']};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{
    border: 1px solid {t['border_active']};
    background: {t['paper']};
}}
QLineEdit:disabled, QTextEdit:disabled, QPlainTextEdit:disabled, QComboBox:disabled {{
    color: {t['disabled']};
    background: {t['surface2']};
}}
QLineEdit[readOnly="true"] {{
    color: {t['muted']};
}}
QLineEdit#InlineDocName, QLineEdit#InlineDocName:hover, QLineEdit#InlineDocName:focus {{
    background: transparent;
    color: {t['text']};
    border: none;
    border-radius: 0;
    padding: 2px 6px;
    font-size: {FS_AUX}px;
}}
QLineEdit#InlineDocName:disabled {{
    background: transparent;
    color: {t['disabled']};
}}
QComboBox {{
    border-radius: 15px;
    padding: 0 26px 0 12px;
}}
QComboBox::drop-down {{
    border: none;
    width: 24px;
    subcontrol-origin: padding;
    subcontrol-position: center right;
}}
QComboBox::down-arrow {{
    {chevron}
    width: 13px;
    height: 13px;
}}
QComboBox QAbstractItemView {{
    background: {t['paper']};
    color: {t['text']};
    border: 1px solid {t['border']};
    border-radius: {R_MD}px;
    selection-background-color: {t['selection_menu']};
    selection-color: {t['text']};
    outline: 0;
    padding: {SP_XS}px;
}}
QComboBox QAbstractItemView::item {{
    min-height: 30px;
    padding: 0 {SP_SM}px;
    border-radius: {R_SM}px;
}}

/* script editor: the page itself, borderless inside the Paper frame */
QTextEdit#ScriptInput, QTextEdit#ScriptInput:focus, QTextEdit#ScriptInput:disabled {{
    background: transparent;
    border: none;
    border-radius: 0;
    padding: 0;
    font-size: {FS_PAPER}px;
}}
QTextEdit#ScriptInput:disabled {{
    color: {t['muted']};
}}

/* ---------- checkbox / radio ---------- */
QCheckBox, QRadioButton {{
    background: transparent;
    color: {t['muted']};
    spacing: 8px;
}}
QCheckBox:hover, QRadioButton:hover {{
    color: {t['text']};
}}
QCheckBox::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {t['border_hover']};
    border-radius: 4px;
    background: {t['paper']};
}}
QCheckBox::indicator:hover {{
    border-color: {t['border_active']};
}}
QCheckBox::indicator:checked {{
    background: {t['btn_primary']};
    border-color: {t['btn_primary']};
    {tick}
}}
QCheckBox:disabled, QRadioButton:disabled {{
    color: {t['dim']};
}}
QCheckBox::indicator:disabled {{
    border-color: {t['border']};
    background: {t['surface2']};
}}
QRadioButton::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {t['border_hover']};
    border-radius: 8px;
    background: {t['paper']};
}}
QRadioButton::indicator:hover {{
    border-color: {t['border_active']};
}}
QRadioButton::indicator:checked {{
    width: 8px;
    height: 8px;
    background: {t['paper']};
    border: 4px solid {t['btn_primary']};
    border-radius: 8px;
}}
QRadioButton::indicator:checked:disabled {{
    width: 8px;
    height: 8px;
    background: {t['paper']};
    border: 4px solid {t['dim']};
    border-radius: 8px;
}}
QRadioButton::indicator:disabled {{
    border-color: {t['border']};
    background: {t['surface2']};
}}

/* ---------- buttons ---------- */
QPushButton {{
    background: {t['surface2']};
    color: {t['text']};
    border: 1px solid transparent;
    border-radius: 15px;
    padding: 5px 14px;
}}
QPushButton:hover {{
    background: {t['recessed']};
}}
QPushButton:pressed {{
    background: {t['surface3']};
}}
QPushButton:disabled {{
    color: {t['disabled']};
    background: {t['surface2']};
    border-color: transparent;
}}
QPushButton#PrimaryBtn {{
    background: {t['btn_primary']};
    color: {t['btn_primary_text']};
    border: 1px solid transparent;
    border-radius: {PRIMARY_H // 2}px;
    padding: 0 18px;
    font-weight: 700;
}}
QPushButton#PrimaryBtn:hover {{
    background: {t['btn_primary_hover']};
}}
QPushButton#PrimaryBtn:pressed {{
    background: {t['text']};
}}
QPushButton#PrimaryBtn:disabled {{
    background: {t['surface3']};
    color: {t['dim']};
    border-color: transparent;
}}
QPushButton#SecondaryBtn {{
    background: {t['surface2']};
    color: {t['text']};
    border: 1px solid transparent;
    border-radius: 15px;
    padding: 0 14px;
}}
QPushButton#SecondaryBtn:hover {{
    background: {t['recessed']};
}}
QPushButton#SecondaryBtn:pressed {{
    background: {t['surface3']};
}}
QPushButton#SecondaryBtn:disabled {{
    color: {t['disabled']};
    background: transparent;
}}
QPushButton#PaperPillBtn {{
    background: {t['paper']};
    color: {t['text']};
    border: 1px solid {t['border_card']};
    border-radius: 9px;
    padding: 0 12px;
    font-size: {FS_MICRO}px;
}}
QPushButton#PaperPillBtn:hover {{
    background: {t['surface']};
    border-color: {t['border_hover']};
}}
QPushButton#PaperPillBtn:disabled {{
    color: {t['disabled']};
    background: {t['surface2']};
    border-color: {t['border']};
}}
QPushButton#OutlineBtn {{
    background: {t['paper']};
    color: {t['text']};
    border: 1px solid {t['border_card']};
    border-radius: 9px;
    padding: 0 12px;
    font-size: {FS_MICRO}px;
}}
QPushButton#OutlineBtn:hover {{
    background: {t['surface2']};
    border-color: {t['border_hover']};
}}
QPushButton#OutlineBtn:checked {{
    background: {t['well']};
    color: {t['text']};
    border-color: {t['border_active']};
    font-weight: 600;
}}
QPushButton#OutlineBtn:disabled {{
    color: {t['disabled']};
    background: {t['surface2']};
    border-color: {t['border']};
}}
QPushButton#SoftBtn {{
    background: {t['well']};
    color: {t['text']};
    border: 1px solid {t['border']};
    border-radius: 8px;
    padding: 0 10px;
    font-size: {FS_MICRO}px;
}}
QPushButton#SoftBtn:hover {{
    background: {t['surface3']};
    border-color: {t['border_hover']};
}}
QPushButton#SoftBtn:disabled {{
    color: {t['disabled']};
    background: {t['surface2']};
    border-color: {t['border']};
}}
QPushButton#DangerBtn {{
    background: {t['danger_bg']};
    color: {t['danger']};
    border: 1px solid {t['danger_border']};
    border-radius: 9px;
    padding: 0 12px;
    font-weight: 700;
}}
QPushButton#DangerBtn:hover {{
    background: {_rgba(t['danger'], 0.14)};
}}
QPushButton#DangerBtn:disabled {{
    color: {t['disabled']};
    background: transparent;
    border-color: {t['border']};
}}
QPushButton#GhostBtn, QPushButton#IconBtn {{
    background: transparent;
    color: {t['muted']};
    border: 1px solid transparent;
    border-radius: 14px;
    padding: 4px 9px;
}}
QPushButton#GhostBtn:hover, QPushButton#IconBtn:hover {{
    color: {t['text']};
    background: {t['surface2']};
}}
QPushButton#GhostBtn:checked {{
    color: {t['text']};
    background: {t['recessed']};
}}
QPushButton#GhostBtn[compact="true"] {{
    padding: 2px 10px;
}}
QPushButton#SealTextBtn {{
    background: transparent;
    color: {t['accent']};
    border: 1px solid transparent;
    border-radius: 12px;
    padding: 2px 8px;
    font-size: {FS_AUX}px;
}}
QPushButton#SealTextBtn:hover {{
    background: {t['accent_tint']};
}}
QPushButton#GhostBtn:disabled, QPushButton#IconBtn:disabled {{
    color: {t['disabled']};
    background: transparent;
}}
QPushButton:focus {{
    border: 1px solid {t['border_active']};
}}
QPushButton#PrimaryBtn:focus {{
    border: 1px solid {t['border_active']};
}}
QPushButton#SecondaryBtn:focus, QPushButton#DangerBtn:focus,
QPushButton#GhostBtn:focus, QPushButton#IconBtn:focus,
QPushButton#ToggleChip:focus,
QPushButton#PaperPillBtn:focus {{
    border: 1px solid {t['border_active']};
}}
QRadioButton:focus {{
    color: {t['text']};
}}

/* ---------- toggle chips ---------- */
QPushButton#ToggleChip {{
    background: {t['surface2']};
    color: {t['muted']};
    border: 1px solid transparent;
    border-radius: {CONTROL_H // 2}px;
    padding: 0 12px;
    font-size: {FS_AUX}px;
}}
QPushButton#ToggleChip:hover {{
    color: {t['text']};
    background: {t['recessed']};
}}
QPushButton#ToggleChip:checked {{
    background: {t['btn_primary']};
    color: {t['btn_primary_text']};
}}
QPushButton#ToggleChip:disabled {{
    color: {t['disabled']};
    background: transparent;
}}

/* ---------- round play button (solid ink, paper glyph) ---------- */
QPushButton#PlayBtn {{
    background: {t['btn_primary']};
    border: none;
    border-radius: {PLAYER_BTN // 2}px;
    min-width: {PLAYER_BTN}px;
    max-width: {PLAYER_BTN}px;
    min-height: {PLAYER_BTN}px;
    max-height: {PLAYER_BTN}px;
    padding: 0;
}}
QPushButton#PlayBtn:hover {{
    background: {t['btn_primary_hover']};
}}
QPushButton#PlayBtn:disabled {{
    background: {t['surface3']};
}}

/* ---------- history list (bordered modular row cards inside well) ---------- */
QFrame#HistoryItem {{
    background: {t['paper']};
    border: 1px solid {t['border_card']};
    border-radius: 11px;
}}
QFrame#HistoryItem:hover {{
    background: {t['surface']};
    border: 1px solid {t['border_hover']};
}}
QFrame#HistoryItem[playing="true"] {{
    background: {t['paper']};
    border: 1px solid {t['accent_border']};
}}
QFrame#HistoryPlayBar {{
    background: {t['accent']};
    border: none;
    border-radius: 1px;
}}
QPushButton#HisPlay, QPushButton#HisSave, QPushButton#HisDel, QPushButton#HisExpand {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 12px;
    padding: 0;
}}
QPushButton#HisPlay:hover, QPushButton#HisSave:hover, QPushButton#HisExpand:hover {{
    background: {t['surface2']};
}}
QFrame#HistoryItem:hover QPushButton#HisPlay:hover,
QFrame#HistoryItem:hover QPushButton#HisSave:hover,
QFrame#HistoryItem:hover QPushButton#HisExpand:hover {{
    background: {t['surface3']};
}}
QPushButton#HisDel:hover {{
    background: {_rgba(t['danger'], 0.10)};
}}
QPushButton#HisPlay:disabled, QPushButton#HisSave:disabled, QPushButton#HisDel:disabled {{
    background: transparent;
}}
QLabel#HistoryMeta {{
    color: {t['dim']};
    font-family: {_NUM_STACK};
    font-size: {FS_MICRO}px;
}}
QFrame#HistoryItem[playing="true"] QLabel#HistoryMeta {{
    color: {t['accent']};
}}
QLabel#HistoryText {{
    color: {t['text']};
    font-size: {FS_AUX}px;
}}
QLabel#HistoryText[missing="true"] {{
    color: {t['muted']};
}}
QLabel#HistoryMissing, QLabel#WarnPill {{
    background: {t['warning_bg']};
    color: {t['warning']};
    border: 1px solid {t['warning_border']};
    border-radius: 9px;
    padding: 1px 7px;
    font-size: 11px;
}}
QFrame#BadgeNeutral, QLabel#BadgeNeutral {{
    background: {t['well']};
    color: {t['muted']};
    border: 1px solid {t['border']};
    border-radius: 9px;
    padding: 1px 9px;
    font-size: {FS_MICRO}px;
}}
QLabel#BadgeOk {{
    background: {t['ok_bg']};
    color: {t['ok']};
    border: 1px solid {t['ok_border']};
    border-radius: 9px;
    padding: 1px 9px;
    font-size: {FS_MICRO}px;
}}
QLabel#BadgeVerm {{
    background: {t['accent_tint']};
    color: {t['accent']};
    border: 1px solid {t['accent_border']};
    border-radius: 9px;
    padding: 1px 8px;
    font-size: 11px;
}}
QLabel#KeyCap {{
    background: {t['paper']};
    color: {t['muted']};
    border: 1px solid {t['border_card']};
    border-radius: 6px;
    padding: 2px 7px;
    font-size: 11px;
}}
QLabel#HistoryFull {{
    color: {t['muted']};
    font-size: {FS_AUX}px;
}}
QScrollArea#HistoryDetail {{
    background: {t['well']};
    border: 1px solid {t['border']};
    border-radius: 8px;
}}
QWidget#HistoryDetailBody {{
    background: transparent;
}}
QLabel#HistoryGroup {{
    color: {t['dim']};
    font-size: {FS_MICRO}px;
}}
QLabel#EmptyHint {{
    color: {t['text']};
    font-size: {FS_BODY}px;
    font-weight: 700;
}}
QLabel#EmptyHintSub {{
    color: {t['muted']};
    font-size: {FS_AUX}px;
}}

/* ---------- log view ---------- */
QPlainTextEdit#LogView, QTextEdit#LogView {{
    background: {t['well']};
    color: {t['muted']};
    border: 1px solid {t['border']};
    border-radius: 8px;
    font-family: {_MONO_STACK};
    font-size: {FS_MICRO}px;
    padding: 8px 10px;
}}

/* ---------- progress ---------- */
QProgressBar {{
    background: #E2DCD0;
    border: none;
    border-radius: 2px;
    max-height: 4px;
    min-height: 4px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    background: {t['accent']};
    border-radius: 2px;
}}

/* ---------- slider ---------- */
QSlider {{
    background: transparent;
    min-height: 20px;
}}
QSlider::groove:horizontal {{
    height: 3px;
    background: #E2DCD0;
    border-radius: 1px;
}}
QSlider::sub-page:horizontal {{
    background: {t['text']};
    border-radius: 1px;
}}
QSlider::add-page:horizontal {{
    background: #E2DCD0;
}}
QSlider::handle:horizontal {{
    width: 10px;
    height: 10px;
    margin: -5px 0;
    border-radius: 6px;
    background: {t['paper']};
    border: 2px solid {t['text']};
}}
QSlider::handle:horizontal:hover {{
    width: 12px;
    height: 12px;
    margin: -6px 0;
    border-radius: 7px;
    background: {t['surface']};
}}
QSlider::handle:horizontal:disabled {{
    border-color: {t['border_hover']};
}}
QSlider::sub-page:horizontal:disabled {{
    background: {t['border_hover']};
}}

/* ---------- scroll ---------- */
QScrollArea {{
    background: transparent;
    border: none;
}}
QSplitter::handle {{
    background: transparent;
}}
QScrollArea > QWidget > QWidget {{
    background: transparent;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {t['border_hover']};
    border-radius: 2px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{
    background: {t['border_active']};
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 8px;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {t['border_hover']};
    border-radius: 2px;
    min-width: 24px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {t['border_active']};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    height: 0;
    width: 0;
}}
QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent;
}}

/* ---------- tooltip ---------- */
QToolTip {{
    background: {t['toast_bg']};
    color: {t['btn_primary_text']};
    border: none;
    border-radius: {R_SM}px;
    padding: 5px 10px;
    font-size: {FS_MICRO}px;
}}

/* ---------- menu ---------- */
QMenu {{
    background: {t['paper']};
    color: {t['text']};
    border: 1px solid {t['border']};
    border-radius: {R_MD}px;
    padding: {SP_XS}px;
}}
QMenu::item {{
    padding: 7px 16px;
    border-radius: {R_SM}px;
}}
QMenu::item:selected {{
    background: {t['selection_menu']};
}}
QMenu::separator {{
    height: 1px;
    background: {t['hairline']};
    margin: 4px 8px;
}}
QMessageBox {{
    background: {t['paper']};
}}
"""


def header_qss() -> str:
    """Typography stylesheet (titles, labels, numerals)."""
    t = THEME
    return f"""
QLabel#AppTitle {{
    color: {t['text']};
    font-family: {_BRAND_STACK};
    font-size: {FS_BRAND}px;
    font-weight: 700;
}}
QLabel#AppSubtitle {{
    color: {t['dim']};
    font-family: {_NUM_STACK};
    font-size: {FS_MICRO}px;
    padding-top: 2px;
}}
QLabel#SectionTitle {{
    color: {t['text']};
    font-size: {FS_SECTION}px;
    font-weight: 700;
}}
QLabel#CardTitle {{
    color: {t['text']};
    font-size: {FS_SECTION}px;
    font-weight: 700;
}}
QLabel#FieldLabel {{
    color: {t['muted']};
    font-size: {FS_MICRO}px;
    font-weight: 400;
}}
QLabel#RowTitle {{
    color: {t['text']};
    font-size: {FS_BODY}px;
}}
QLabel#MutedBody {{
    color: {t['muted']};
    font-size: {FS_AUX}px;
}}
QLabel#WeakMeta {{
    color: {t['dim']};
    font-size: {FS_MICRO}px;
}}
QLabel#MonoWeak {{
    color: {t['dim']};
    font-family: {_MONO_STACK};
    font-size: {FS_MICRO}px;
}}
QLabel#SealTag, QLabel#DetectTag {{
    background: {t['accent_tint']};
    color: {t['accent']};
    border: 1px solid {t['accent_border']};
    border-radius: 9px;
    padding: 1px 8px;
    font-size: 11px;
}}
QLabel#StatusHint {{
    color: {t['dim']};
    font-size: {FS_MICRO}px;
}}
QLabel#Num, QLabel#PaperCount {{
    color: {t['dim']};
    font-family: {_NUM_STACK};
    font-size: {FS_MICRO}px;
}}
QLabel#NowPlaying {{
    color: {t['text']};
    font-size: {FS_BODY}px;
}}
QFrame#PlayerBar[idle="true"] QLabel#NowPlaying {{
    color: {t['dim']};
}}
QLabel#SummaryText {{
    color: {t['muted']};
    font-size: {FS_AUX}px;
}}
QLabel#PlayerTime {{
    color: {t['dim']};
    font-family: {_MONO_STACK};
    font-size: {FS_MICRO}px;
}}
QLabel#EngineLabel {{
    color: {t['muted']};
    font-size: {FS_AUX}px;
}}
QLabel#EngineUrlLabel {{
    color: {t['dim']};
    font-family: {_MONO_STACK};
    font-size: {FS_MICRO}px;
}}
QLabel#PaperTitle {{
    color: {t['muted']};
    font-family: {_FONT_STACK};
    font-size: {FS_AUX}px;
}}
QLabel#SetupTitle {{
    color: {t['text']};
    font-family: {_FONT_STACK};
    font-size: 23px;
    font-weight: 700;
}}
QLabel#SetupSubtitle {{
    color: {t['muted']};
    font-size: {FS_AUX}px;
}}
QLabel#SetupBullet {{
    color: {t['muted']};
    font-size: {FS_AUX}px;
}}
QLabel#SetupDot {{
    color: {t['dim']};
    font-size: {FS_AUX}px;
}}
QLabel#SourceChannel {{
    color: {t['dim']};
    font-size: {FS_MICRO}px;
}}
QLabel#SourceMetric {{
    color: {t['dim']};
    font-family: {_MONO_STACK};
    font-size: {FS_MICRO}px;
}}
QLabel#SourceMetric[status="ok"] {{
    color: {t['ok']};
}}
QLabel#SourceMetric[status="bad"] {{
    color: {t['danger']};
}}
QLabel#SourceMetric[status="busy"] {{
    color: {t['warning']};
}}
QLabel#DiskHint {{
    color: {t['dim']};
    font-size: {FS_MICRO}px;
}}
QLabel#StageText {{
    color: {t['dim']};
    font-size: {FS_MICRO}px;
}}
QLabel#LogPercent {{
    color: {t['dim']};
    font-family: {_MONO_STACK};
    font-size: {FS_MICRO}px;
}}
QLabel#DialogTitle {{
    color: {t['text']};
    font-family: {_FONT_STACK};
    font-size: {FS_BRAND}px;
    font-weight: 700;
}}
QLabel#DialogHint {{
    color: {t['muted']};
    font-size: {FS_AUX}px;
}}
QLabel#DialogError {{
    color: {t['danger']};
    font-size: {FS_AUX}px;
}}
"""


def status_style(kind: str) -> str:
    """Inline stylesheet for the engine/job status label.

    ``kind``: ``busy`` | ``warn`` | ``ok`` | ``error`` (unknown -> muted).
    """
    t = THEME
    color = {
        "busy": t["muted"],
        "warn": t["warning"],
        "ok": t["ok"],
        "error": t["danger"],
    }.get(kind, t["muted"])
    return (
        f"QLabel {{ color: {color}; background: transparent; "
        f"font-size: {FS_AUX}px; }}"
    )


def dot_style(kind: str) -> str:
    """Inline stylesheet for the 8px engine status dot."""
    t = THEME
    color = {
        "busy": t["warning"],
        "warn": t["warning"],
        "ok": t["ok"],
        "error": t["danger"],
    }.get(kind, t["dim"])
    return f"QFrame {{ background: {color}; border: none; border-radius: 4px; }}"


def log_color(level: str) -> str:
    """Message-body color for a structured-log level.

    Only ``warn`` / ``error`` stay chromatic; everything else is muted so the
    log reads quietly until something actually needs attention.
    """
    t = THEME
    return {
        "info": t["muted"],
        "ok": t["muted"],
        "warn": t["warning"],
        "error": t["danger"],
        "engine": t["muted"],
        "debug": t["muted"],
    }.get(level, t["muted"])


def log_tag_color(level: str) -> str:
    """Tag color for a structured-log level (dim unless it is a problem)."""
    t = THEME
    return {
        "info": t["dim"],
        "ok": t["dim"],
        "warn": t["warning"],
        "error": t["danger"],
        "engine": t["dim"],
        "debug": t["dim"],
    }.get(level, t["dim"])
