"""Single-window application shell (spec item 22).

``MamboStudioWindow`` hosts a ``QStackedWidget`` with the embedded engine
setup view (index 0) and the voiceover studio workspace (index 1).  All
blocking work (engine probing / starting / synthesis) runs on daemon
threads and marshals back through Qt Signals; periodic ``/control`` health
probes honor the ``EngineSupervisor`` heartbeat interlock.
"""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta


def _safe_emit(signal, *args) -> None:
    """Emit from a worker thread without crashing after the window is gone."""
    try:
        signal.emit(*args)
    except RuntimeError:
        pass

from PySide6.QtCore import (
    QBuffer,
    QByteArray,
    QEasingCurve,
    QEvent,
    QObject,
    QPoint,
    QPropertyAnimation,
    QSignalBlocker,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QBoxLayout,
    QComboBox,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from mambotts import APP_VERSION, PROJECT_ROOT
from mambotts.domain.models import (
    DEFAULT_API_URL,
    SPEED_MAX,
    SPEED_MIN,
    AppConfig,
    HistoryRecord,
    SynthesisJobResult,
    has_pronounceable_char,
)
from mambotts.domain.naming import (
    _known_desktop,
    ensure_wav_suffix,
    filename_from_text,
    is_reserved_windows_name,
    resolve_output_dir,
    sanitize_stem,
    unique_path,
)
from mambotts.domain.storage_repo import (
    ConfigRepository,
    HistoryRepository,
    remove_quiet,
    replace_with_retry,
)
from mambotts.domain.voice_repo import REQUIRED_MODEL_FILES, VoiceRepository
from mambotts.infra.engine_mgr import (
    EngineSupervisor,
    append_engine_log,
    engine_log_path,
    is_engine_installed,
    is_local_api_url,
    validate_http_api_url,
)
from mambotts.infra.gsv_client import GSVHttpClient
from mambotts.infra.installer import terminate_active_install_processes
from mambotts.pipeline.scheduler import SynthesisPipeline

from .player_history import HistoryItemWidget, StructuredLogBuffer
from .script_editor import ScriptEditorWidget
from .setup_view import EmbeddedSetupView
from .icons import icon, stateful_icon
from .theme import (
    CONTROL_H,
    DIALOG_W,
    ICON_SIZE,
    INPUT_H,
    PLAYER_BTN,
    PLAYER_H,
    PRIMARY_H,
    R_LG,
    R_SHEET,
    SHADOW_MARGIN_WINDOW,
    SP_LG,
    SP_MD,
    SP_SM,
    SP_XS,
    SP_XXL,
    THEME,
    TOPBAR_H,
    apply_selection_palette,
    base_qss,
    dot_style,
    header_qss,
    status_style,
)
from .widgets import (
    ElidedLabel,
    EmptyStateWidget,
    ModuleCard,
    ModuleHeaderBar,
    SeekSlider,
    SlideSwitch,
    WarmSpeedSlider,
    attach_shadow,
    set_shadow_level,
)

# QtMultimedia is bundled with PySide6 but kept optional so a broken media
# backend can never take down the studio; synthesis works regardless.
try:  # pragma: no cover - environment dependent
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

    _HAS_QTMULTIMEDIA = True
except Exception:  # noqa: BLE001
    QMediaPlayer = None  # type: ignore[assignment]
    QAudioOutput = None  # type: ignore[assignment]
    _HAS_QTMULTIMEDIA = False

_BUSY_TOOLTIP = "合成进行中，暂不可修改"
_STARTUP_PROBE_DELAY_MS = 100
_HEARTBEAT_STARTUP_MS = 2000
_HEARTBEAT_ONLINE_MS = 10000
_SAVE_DEBOUNCE_MS = 800
_PLAYBACK_RAM_LIMIT_BYTES = 128 * 1024 * 1024
_FORCE_RESTART_WAIT_SEC = 10
_ENGINE_START_TIMEOUT_SEC = 120.0
_STALE_TEMP_PLAY_AGE_SEC = 2 * 3600
_WIN_INVALID_PATH_CHARS = set('<>"|?*')


def _cleanup_stale_temp_play_files() -> None:
    """Remove leftover mambotts_play_*.wav temp files from dead/stale runs."""
    tmp_dir = tempfile.gettempdir()
    pid_prefix = f"mambotts_play_{os.getpid()}_"
    now = time.time()
    try:
        entries = os.listdir(tmp_dir)
    except OSError:
        return
    for name in entries:
        if not (name.startswith("mambotts_play_") and name.endswith(".wav")):
            continue
        full = os.path.join(tmp_dir, name)
        if name.startswith(pid_prefix):
            remove_quiet(full)
            continue
        try:
            if now - os.path.getmtime(full) > _STALE_TEMP_PLAY_AGE_SEC:
                remove_quiet(full)
        except OSError:
            pass


def _fmt_time(ms: int) -> str:
    total_sec = max(0, int(ms)) // 1000
    return f"{total_sec // 60:02d}:{total_sec % 60:02d}"


_APP_ICON_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "assets", "app_icon.png"
)


def _make_app_icon() -> QIcon:
    """Build multi-size window/tray icon from the bundled avatar PNG,
    falling back to the drawn monogram if the asset file is missing.
    """
    if os.path.isfile(_APP_ICON_PATH):
        base = QPixmap(_APP_ICON_PATH)
        if not base.isNull():
            app_icon = QIcon()
            for sz in (16, 20, 24, 32, 40, 48, 64, 128, 256):
                scaled = (
                    base
                    if (base.width() == sz and base.height() == sz)
                    else base.scaled(sz, sz, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )
                app_icon.addPixmap(scaled)
            return app_icon

    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#1F1D1A"))
    painter.drawRoundedRect(4, 4, 56, 56, 14, 14)
    painter.setPen(QColor("#FDFCF9"))
    font = QFont()
    font.setFamilies(["Segoe UI", "Microsoft YaHei UI", "PingFang SC", "sans-serif"])
    font.setPixelSize(34)
    font.setWeight(QFont.DemiBold)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignCenter, "M")
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#B5472B"))
    painter.drawEllipse(43, 10, 7, 7)
    painter.end()
    return QIcon(pixmap)


# ---------------------------------------------------------------------------
# Synthesis worker
# ---------------------------------------------------------------------------


class _WorkerSignals(QObject):
    """Qt signal bundle so a plain ``threading.Thread`` can marshal results."""

    progress = Signal(int, int, str)
    done = Signal(object)


class GenerateWorker(threading.Thread):
    """Daemon thread that runs ``SynthesisPipeline.run_job``.

    All parameters are snapshotted before the thread starts (API URL, output
    path, speed, toggles, cancel event).  Results
    travel back to the GUI thread via queued ``Signal`` emissions.
    """

    def __init__(self, pipeline: SynthesisPipeline, params: dict):
        super().__init__(daemon=True, name="mambo-generate-worker")
        self.signals = _WorkerSignals()
        self.cancel_event = threading.Event()
        self._pipeline = pipeline
        self._params = dict(params)
        self._params["cancel_event"] = self.cancel_event
        self._params["progress_cb"] = (
            lambda done, total, stage: _safe_emit(
                self.signals.progress, int(done), int(total), str(stage)
            )
        )

    def run(self) -> None:  # executed on the worker thread
        try:
            result = self._pipeline.run_job(**self._params)
        except Exception as exc:  # noqa: BLE001 - GUI must always get a result
            result = SynthesisJobResult(success=False, message=f"合成线程异常：{exc}")
        _safe_emit(self.signals.done, result)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class MamboStudioWindow(QMainWindow):
    """Single-window MamboTTS studio (setup view + studio workspace)."""

    _probe_done = Signal(bool, int, str, bool)
    _start_done = Signal(bool, str, int)
    _force_restart_done = Signal(int)
    _heartbeat_done = Signal(bool, int, str)
    _engine_line = Signal(str)
    _play_copy_done = Signal(int, str, str)
    _history_save_done = Signal(bool, str, str)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        base_dir: str = PROJECT_ROOT,
        auto_start_engine: bool = True,
    ):
        super().__init__(parent)
        self._base_dir = base_dir
        self._config_path = os.path.join(base_dir, "config.json")
        self._history_path = os.path.join(base_dir, "history.json")
        self._auto_start_engine = bool(auto_start_engine)

        config_warnings: list[str] = []
        self.cfg: AppConfig = ConfigRepository.load(
            self._config_path, warn_cb=config_warnings.append
        )
        normalized_api = validate_http_api_url(self.cfg.api_url)
        if normalized_api:
            self.cfg.api_url = normalized_api
        else:
            if self.cfg.api_url:
                config_warnings.append(
                    f"配置中的引擎地址无效（{self.cfg.api_url}），已恢复默认。"
                )
            self.cfg.api_url = DEFAULT_API_URL

        raw_speed = float(self.cfg.speed) if isinstance(self.cfg.speed, (int, float)) else 1.0
        if not math.isfinite(raw_speed):
            raw_speed = 1.0
        self._exact_speed = min(SPEED_MAX, max(SPEED_MIN, raw_speed))
        self._initial_slider_val = int(round(self._exact_speed * 100))

        self.setObjectName("MainWindow")
        self.setWindowTitle(f"MamboTTS 工作室 {APP_VERSION}")
        self._app_icon = _make_app_icon()
        self.setWindowIcon(self._app_icon)
        self.setMinimumSize(1000, 680)
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.resize(
                min(1240, max(1000, int(available.width() * 0.9))),
                min(800, max(680, int(available.height() * 0.9))),
            )
        else:
            self.resize(1240, 800)

        # ---- state ----
        _cleanup_stale_temp_play_files()
        self.log_buffer = StructuredLogBuffer(max_entries=2000)
        self._history_records: list[HistoryRecord] = []
        self._history_widgets: list[HistoryItemWidget] = []
        self._synth_worker: GenerateWorker | None = None
        self._last_text = ""
        self._last_params: dict | None = None
        self._last_save_path = ""
        self._engine_online = False
        self._engine_starting = False
        self._starting_in_flight = False
        self._force_restarting = False
        self._probe_in_flight = False
        self._probe_generation = 0
        self._start_generation = 0
        self._starting_url = ""
        self._engine_start_deadline = 0.0
        self._cancel_requested = False
        self._closing = False
        self._saved_tooltips: dict[QWidget, str] = {}
        self._status_kind = "busy"
        self._real_quit = False
        self._tray: QSystemTrayIcon | None = None
        self._tray_menu: QMenu | None = None
        self.setup_view: EmbeddedSetupView | None = None
        self._player = None
        self._audio_output = None
        self._player_bytes: QByteArray | None = None
        self._player_buffer: QBuffer | None = None
        self._temp_play_file = ""
        self._play_copy_gen = 0
        self._player_duration = 0
        self._seek_dragging = False
        self._current_audio_path = ""
        self._has_audio = False
        self._fade_started = False
        self._dot_anim: QPropertyAnimation | None = None
        self._progress_anim: QPropertyAnimation | None = None
        self._cancel_wait_sec = 0
        self._cancel_can_force_restart = False
        self._probe_preserve_gen = 0
        self._synth_started_mono = 0.0
        self._synth_stage_text = ""

        # ---- engine stack ----
        self.supervisor = EngineSupervisor(
            base_dir=base_dir, engine_line_cb=self._engine_line.emit
        )
        self.voice_repo = VoiceRepository(base_dir)
        self.gsv_client = GSVHttpClient(
            api_url=self.cfg.api_url, project_dir=base_dir
        )
        self.pipeline = SynthesisPipeline(
            self.voice_repo, self.gsv_client, supervisor=self.supervisor
        )

        # ---- UI ----
        self._build_ui()

        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(base_qss() + header_qss())
            apply_selection_palette(app)

        # ---- signals ----
        self._probe_done.connect(self._on_startup_probe)
        self._start_done.connect(self._on_engine_start_done)
        self._force_restart_done.connect(self._on_force_restart_done)
        self._heartbeat_done.connect(self._on_heartbeat_result)
        self._engine_line.connect(self._on_engine_line)
        self._play_copy_done.connect(self._on_play_copy_done)
        self._history_save_done.connect(self._on_history_save_done)

        # ---- history ----
        self._history_records = HistoryRepository.load(
            self._history_path, base_output_dir=self._history_base_dir()
        )
        self._render_history()

        # ---- timers ----
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.setInterval(_HEARTBEAT_STARTUP_MS)
        self._heartbeat_timer.timeout.connect(self._heartbeat_tick)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(_SAVE_DEBOUNCE_MS)
        self._save_timer.timeout.connect(self._save_config)

        self._cancel_wait_timer = QTimer(self)
        self._cancel_wait_timer.setInterval(1000)
        self._cancel_wait_timer.timeout.connect(self._on_cancel_wait_tick)

        self._synth_elapsed_timer = QTimer(self)
        self._synth_elapsed_timer.setInterval(1000)
        self._synth_elapsed_timer.timeout.connect(self._on_synth_elapsed_tick)

        # ---- initial view & probe ----
        self._set_status("正在检测引擎…", "busy")
        if is_engine_installed(base_dir=self._base_dir):
            self.stack.setCurrentWidget(self.studio_page)
            self._heartbeat_timer.start()
            QTimer.singleShot(_STARTUP_PROBE_DELAY_MS, self._startup_probe)
        else:
            self.show_setup_view()

        for warning in config_warnings:
            self._log("warn", warning)
        self._log("info", f"MamboTTS {APP_VERSION} 已启动。")

        if self.cfg.minimize_to_tray:
            self._setup_tray()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self._build_studio_page()
        self.stack.addWidget(self.studio_page)  # index adjusted when setup view inserts

    def _build_studio_page(self) -> None:
        self.studio_page = QWidget()
        self.studio_page.setObjectName("StudioPage")
        root = QVBoxLayout(self.studio_page)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_top_bar())
        self.model_banner = self._build_model_banner()
        root.addWidget(self.model_banner)

        body = QHBoxLayout()
        body.setContentsMargins(SP_XXL, 8, SP_XXL, 8)
        body.setSpacing(SP_LG)
        root.addLayout(body, 1)

        # ---- left: compose column (~60%) ----
        left_widget = QWidget()
        left_widget.setObjectName("TransparentSlot")
        left_widget.setMinimumWidth(460)
        left = QVBoxLayout(left_widget)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(8)
        self.script_paper = self._build_script_paper()
        left.addWidget(self.script_paper, 1)
        attach_shadow(self.script_paper, 1, R_LG)
        self.ctrl_card = self._build_ctrl_card()
        left.addWidget(self.ctrl_card, 0)
        attach_shadow(self.ctrl_card, 1, R_LG)
        body.addWidget(left_widget, 3)

        # ---- right: history + collapsible/docked log (~40%) ----
        right_widget = QWidget()
        right_widget.setObjectName("TransparentSlot")
        right_widget.setMinimumWidth(340)
        right = QVBoxLayout(right_widget)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(SP_MD)
        self.right_splitter = QSplitter(Qt.Vertical)
        self.right_splitter.setChildrenCollapsible(False)
        self.right_splitter.setHandleWidth(8)
        self.history_card = self._build_history_panel()
        self.right_splitter.addWidget(self.history_card)
        self.log_panel = self._build_log_panel()
        self.right_splitter.addWidget(self.log_panel)
        self.right_splitter.setStretchFactor(0, 1)
        self.right_splitter.setStretchFactor(1, 0)
        self.right_splitter.setSizes([520, 200])
        right.addWidget(self.right_splitter, 1)
        self.log_collapsed_strip = self._build_log_collapsed_strip()
        right.addWidget(self.log_collapsed_strip, 0)
        # Log drawer visibility follows the persisted preference (default off).
        log_open = bool(self.cfg.log_visible)
        self.btn_toggle_log.setChecked(log_open)
        self.log_panel.setVisible(log_open)
        self.log_collapsed_strip.setVisible(not log_open)
        if log_open:
            QTimer.singleShot(0, lambda: self.right_splitter.setSizes([520, 200]))
        body.addWidget(right_widget, 2)

        # ---- bottom: full-width floating player bar (always visible; inert until audio) ----
        player_host = QWidget()
        player_host.setObjectName("TransparentSlot")
        player_host_layout = QVBoxLayout(player_host)
        player_host_layout.setContentsMargins(SP_XXL, 0, SP_XXL, 10)
        player_host_layout.setSpacing(0)
        self.player_bar = self._build_player_bar()
        player_host_layout.addWidget(self.player_bar)
        attach_shadow(self.player_bar, 1, R_LG)
        root.addWidget(player_host)

        self._settings_popover = self._build_settings_popover()
        self._apply_icons()
        self._check_model_files()

        # shortcuts
        for seq in ("Ctrl+Return", "Ctrl+Enter"):
            shortcut = QShortcut(QKeySequence(seq), self)
            shortcut.activated.connect(self._on_generate_shortcut)

    def _build_model_banner(self) -> QWidget:
        host = QWidget()
        outer = QHBoxLayout(host)
        outer.setContentsMargins(SP_XXL, SP_SM, SP_XXL, 0)
        outer.setSpacing(0)

        bar = QFrame()
        bar.setObjectName("WarningBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(14, SP_SM, 12, SP_SM)
        layout.setSpacing(SP_SM)
        self.model_banner_icon = QLabel()
        layout.addWidget(self.model_banner_icon, 0, Qt.AlignVCenter)
        self.model_banner_text = QLabel()
        self.model_banner_text.setObjectName("WarningText")
        self.model_banner_text.setWordWrap(True)
        layout.addWidget(self.model_banner_text, 1)
        self.btn_recheck_models = QPushButton("重新检查")
        self.btn_recheck_models.setObjectName("GhostBtn")
        self.btn_recheck_models.setProperty("compact", "true")
        self.btn_recheck_models.setFixedHeight(26)
        self.btn_recheck_models.setCursor(Qt.PointingHandCursor)
        self.btn_recheck_models.clicked.connect(self._on_recheck_models)
        layout.addWidget(self.btn_recheck_models)
        self.btn_open_models = QPushButton("打开 models/")
        self.btn_open_models.setObjectName("GhostBtn")
        self.btn_open_models.setProperty("compact", "true")
        self.btn_open_models.setFixedHeight(26)
        self.btn_open_models.setCursor(Qt.PointingHandCursor)
        self.btn_open_models.clicked.connect(self._open_models_dir)
        layout.addWidget(self.btn_open_models)
        outer.addWidget(bar)
        host.hide()
        return host

    def _model_files(self) -> list[str]:
        return list(REQUIRED_MODEL_FILES)

    def _check_model_files(self) -> None:
        missing = [
            name
            for name in self._model_files()
            if not os.path.isfile(os.path.join(self.voice_repo.models_dir, name))
        ]
        if not missing:
            self.model_banner.hide()
            return
        self.model_banner_text.setText(
            "缺少曼波音色模型：" + "、".join(missing) + "，请放入 models 目录后再合成。"
        )
        self.model_banner_text.setToolTip(self.voice_repo.models_dir)
        self.model_banner.show()

    def _on_recheck_models(self) -> None:
        self._check_model_files()
        if (
            not self.model_banner.isVisible()
            and not self._engine_online
            and not self._engine_starting
        ):
            self._startup_probe()

    def _open_models_dir(self) -> None:
        folder = self.voice_repo.models_dir
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            self._set_status("无法打开模型目录", "error")
            self._log("error", f"创建模型目录失败（{folder}）：{exc}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _build_top_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("TopBar")
        bar.setFixedHeight(TOPBAR_H)
        row = QHBoxLayout(bar)
        row.setContentsMargins(SP_XXL, 4, SP_XXL, 0)
        row.setSpacing(SP_SM)

        brand_dot = QFrame()
        brand_dot.setObjectName("BrandDot")
        row.addWidget(brand_dot, 0, Qt.AlignVCenter)

        title = QLabel("MamboTTS 工作台")
        title.setObjectName("AppTitle")
        row.addWidget(title, 0, Qt.AlignVCenter)

        version = QLabel(APP_VERSION)
        version.setObjectName("BadgeNeutral")
        version.setFixedHeight(22)
        version.setAlignment(Qt.AlignCenter)
        row.addWidget(version, 0, Qt.AlignVCenter)
        row.addStretch(1)

        self.engine_pill = QFrame()
        self.engine_pill.setObjectName("EnginePill")
        self.engine_pill.setProperty("state", "busy")
        self.engine_pill.setFixedHeight(30)
        pill_row = QHBoxLayout(self.engine_pill)
        pill_row.setContentsMargins(12, 0, 12, 0)
        pill_row.setSpacing(6)

        self.engine_dot = QFrame()
        self.engine_dot.setFixedSize(8, 8)
        pill_row.addWidget(self.engine_dot, 0, Qt.AlignVCenter)
        self._dot_effect = QGraphicsOpacityEffect(self.engine_dot)
        self._dot_effect.setOpacity(1.0)
        self.engine_dot.setGraphicsEffect(self._dot_effect)

        self.engine_status_label = QLabel("检测中")
        self.engine_status_label.setObjectName("EngineLabel")
        pill_row.addWidget(self.engine_status_label, 0, Qt.AlignVCenter)

        sep = QLabel("·")
        sep.setObjectName("WeakMeta")
        pill_row.addWidget(sep, 0, Qt.AlignVCenter)

        # The configured endpoint lives next to the engine state in the top
        # bar (never in the action row); scheme stripped to keep it short.
        self.engine_url_label = QLabel(
            self.cfg.api_url.split("://", 1)[-1].rstrip("/")
        )
        self.engine_url_label.setObjectName("EngineUrlLabel")
        pill_row.addWidget(self.engine_url_label, 0, Qt.AlignVCenter)

        self.btn_engine_retry = QPushButton("重试")
        self.btn_engine_retry.setObjectName("SoftBtn")
        self.btn_engine_retry.setFixedHeight(22)
        self.btn_engine_retry.setCursor(Qt.PointingHandCursor)
        self.btn_engine_retry.setToolTip("重新连接 / 启动引擎")
        self.btn_engine_retry.clicked.connect(self._on_engine_retry)
        self.btn_engine_retry.hide()
        pill_row.addWidget(self.btn_engine_retry, 0, Qt.AlignVCenter)
        row.addWidget(self.engine_pill, 0, Qt.AlignVCenter)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(SP_SM)

        self.btn_top_models = QPushButton(" 模型目录")
        self.btn_top_models.setObjectName("OutlineBtn")
        self.btn_top_models.setFixedHeight(30)
        self.btn_top_models.setCursor(Qt.PointingHandCursor)
        self.btn_top_models.setToolTip("打开 models 音色模型目录")
        self.btn_top_models.clicked.connect(self._open_models_dir)
        actions.addWidget(self.btn_top_models)

        self.btn_toggle_log = QPushButton(" 日志")
        self.btn_toggle_log.setObjectName("OutlineBtn")
        self.btn_toggle_log.setCheckable(True)
        self.btn_toggle_log.setFixedHeight(30)
        self.btn_toggle_log.setCursor(Qt.PointingHandCursor)
        self.btn_toggle_log.setToolTip("显示 / 隐藏运行日志")
        self.btn_toggle_log.toggled.connect(self._on_log_toggled)
        actions.addWidget(self.btn_toggle_log)

        self.btn_settings = QPushButton(" 设置")
        self.btn_settings.setObjectName("OutlineBtn")
        self.btn_settings.setFixedHeight(30)
        self.btn_settings.setCursor(Qt.PointingHandCursor)
        self.btn_settings.setToolTip("引擎地址与托盘设置")
        self.btn_settings.clicked.connect(self._show_settings)
        actions.addWidget(self.btn_settings)
        row.addLayout(actions)
        return bar

    def _build_script_paper(self) -> QFrame:
        paper = QFrame()
        paper.setObjectName("ModuleCard")
        paper.setMinimumHeight(160)
        layout = QVBoxLayout(paper)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        self.script_editor = ScriptEditorWidget()
        self.script_editor.text_input.installEventFilter(self)
        layout.addWidget(self.script_editor, 1)
        return paper

    def _build_ctrl_card(self) -> ModuleCard:
        """Two-tier control module: export path/naming on top, speed/SRT/actions below."""
        card = ModuleCard(
            "导出路径与合成参数",
            body_margins=(SP_LG, 10, SP_LG, SP_MD),
            body_spacing=0,
        )
        card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)

        self._ctrl_split = QBoxLayout(QBoxLayout.TopToBottom)
        self._ctrl_split.setContentsMargins(0, 0, 0, 0)
        self._ctrl_split.setSpacing(SP_SM)
        card.body_layout.addLayout(self._ctrl_split)

        # ---- top WellBox: output folder + file naming ----
        out_well = QFrame()
        out_well.setObjectName("WellBox")
        out_well.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self.out_well = out_well
        out_col = QVBoxLayout(out_well)
        out_col.setContentsMargins(SP_MD, 10, SP_MD, 10)
        out_col.setSpacing(6)

        dir_row = QHBoxLayout()
        dir_row.setContentsMargins(0, 0, 0, 0)
        dir_row.setSpacing(6)
        dir_label = QLabel("保存目录")
        dir_label.setObjectName("MutedBody")
        dir_row.addWidget(dir_label)

        init_output_dir = (
            os.path.normpath(self.cfg.output_dir.strip())
            if (self.cfg.output_dir or "").strip()
            else ""
        )
        self.path_input = QLineEdit(init_output_dir)
        self.path_input.setObjectName("CleanInput")
        self.path_input.setFixedHeight(INPUT_H)
        self.path_input.setMinimumWidth(0)
        self.path_input.setPlaceholderText("桌面（默认）")
        self.path_input.textChanged.connect(self._update_output_summary)
        self.path_input.editingFinished.connect(self._schedule_save)
        self.path_input.editingFinished.connect(self._update_output_summary)
        dir_row.addWidget(self.path_input, 1)

        self.btn_browse = QPushButton("浏览")
        self.btn_browse.setObjectName("OutlineBtn")
        self.btn_browse.setFixedHeight(INPUT_H)
        self.btn_browse.setCursor(Qt.PointingHandCursor)
        self.btn_browse.setToolTip("选择音频输出文件夹")
        self.btn_browse.clicked.connect(self._browse_output_dir)
        dir_row.addWidget(self.btn_browse)

        self.btn_open_folder = QPushButton()
        self.btn_open_folder.setObjectName("OutlineBtn")
        self.btn_open_folder.setFixedSize(INPUT_H, INPUT_H)
        self.btn_open_folder.setCursor(Qt.PointingHandCursor)
        self.btn_open_folder.setToolTip("打开输出文件夹")
        self.btn_open_folder.clicked.connect(self._open_output_folder)
        dir_row.addWidget(self.btn_open_folder)
        out_col.addLayout(dir_row)

        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)
        name_row.setSpacing(6)
        name_label = QLabel("文件命名")
        name_label.setObjectName("MutedBody")
        name_row.addWidget(name_label)

        self.name_input = QLineEdit(self.cfg.name)
        self.name_input.setObjectName("CleanInput")
        self.name_input.setFixedHeight(INPUT_H)
        self.name_input.setPlaceholderText("留空按首句自动命名")
        self.name_input.textChanged.connect(self._update_output_summary)
        self.name_input.editingFinished.connect(self._schedule_save)
        self.name_input.editingFinished.connect(self._update_output_summary)
        name_row.addWidget(self.name_input, 1)
        out_col.addLayout(name_row)

        self._strip_status = QLabel()
        self._strip_status.setObjectName("WeakMeta")
        self._strip_status.setVisible(False)
        out_col.addWidget(self._strip_status)

        # Hidden compatibility summary label for automated state checks.
        self.output_summary = ElidedLabel("")
        self.output_summary.setObjectName("SummaryText")
        self.output_summary.setVisible(False)
        out_col.addWidget(self.output_summary)
        self._ctrl_split.addWidget(out_well)

        # ---- bottom WellBox: speed, SRT, reserved progress, and actions ----
        synth_well = QFrame()
        synth_well.setObjectName("WellBox")
        synth_well.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self.synth_well = synth_well
        synth_col = QVBoxLayout(synth_well)
        synth_col.setContentsMargins(SP_MD, 10, SP_MD, 10)
        synth_col.setSpacing(6)

        param_row = QHBoxLayout()
        param_row.setContentsMargins(0, 0, 0, 0)
        param_row.setSpacing(SP_SM)

        self.speed_title = QLabel("语速")
        self.speed_title.setObjectName("MutedBody")
        param_row.addWidget(self.speed_title)

        self.speed_slider = WarmSpeedSlider()
        self.speed_slider.setRange(int(round(SPEED_MIN * 100)), int(round(SPEED_MAX * 100)))
        self.speed_slider.setValue(self._initial_slider_val)
        self.speed_slider.setSingleStep(5)  # 方向键 = ±0.05x
        self.speed_slider.setPageStep(25)  # PgUp/PgDn = ±0.25x
        self.speed_slider.setMinimumWidth(68)
        self.speed_slider.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.speed_slider.setToolTip("拖动按 0.05x 步进吸附 · 双击回到 1.00x")
        self.speed_slider.installEventFilter(self)
        self.speed_slider.valueChanged.connect(self._on_speed_changed)
        param_row.addWidget(self.speed_slider, 1)

        self.speed_value_label = QLabel(f"{self._exact_speed:.2f}x")
        self.speed_value_label.setObjectName("BadgeNeutral")
        self.speed_value_label.setFixedSize(48, 22)
        self.speed_value_label.setAlignment(Qt.AlignCenter)
        param_row.addWidget(self.speed_value_label)

        self._srt_wrap = QWidget()
        self._srt_wrap.setStyleSheet("background: transparent;")
        srt_l = QHBoxLayout(self._srt_wrap)
        srt_l.setContentsMargins(SP_XS, 0, 0, 0)
        srt_l.setSpacing(6)
        self._srt_label = QLabel("导出 SRT")
        self._srt_label.setObjectName("MutedBody")
        self.chk_export_srt = SlideSwitch()
        self.chk_export_srt.setChecked(self.cfg.export_srt)
        self.chk_export_srt.setToolTip("同时导出与音频时间轴对齐的 SRT 字幕")
        self.chk_export_srt.toggled.connect(lambda _checked: self._schedule_save())
        srt_l.addWidget(self._srt_label)
        srt_l.addWidget(self.chk_export_srt)
        param_row.addWidget(self._srt_wrap)
        synth_col.addLayout(param_row)

        # Reserve the 4px progress slot at all times so starting a job can
        # never push the control card height down.
        self.progress_slot = QWidget()
        self.progress_slot.setFixedHeight(4)
        self.progress_slot.setStyleSheet("background: transparent;")
        slot_layout = QVBoxLayout(self.progress_slot)
        slot_layout.setContentsMargins(0, 0, 0, 0)
        slot_layout.setSpacing(0)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self._progress_effect = QGraphicsOpacityEffect(self.progress_bar)
        self._progress_effect.setOpacity(0.0)
        self.progress_bar.setGraphicsEffect(self._progress_effect)
        self.progress_bar.hide()
        slot_layout.addWidget(self.progress_bar)
        synth_col.addWidget(self.progress_slot)

        self.action_row = QWidget()
        self.action_row.setObjectName("ActionRow")
        action_outer = QVBoxLayout(self.action_row)
        action_outer.setContentsMargins(0, 0, 0, 0)
        action_outer.setSpacing(0)

        self.action_wide = QWidget()
        self.action_wide.setObjectName("ActionWide")
        self.action_wide_layout = QHBoxLayout(self.action_wide)
        self.action_wide_layout.setContentsMargins(0, 0, 0, 0)
        self.action_wide_layout.setSpacing(SP_SM)
        action_outer.addWidget(self.action_wide)

        self.action_compact = QWidget()
        self.action_compact.setObjectName("ActionCompact")
        compact_outer = QVBoxLayout(self.action_compact)
        compact_outer.setContentsMargins(0, 0, 0, 0)
        compact_outer.setSpacing(SP_XS)
        self.compact_row0 = QHBoxLayout()
        self.compact_row0.setContentsMargins(0, 0, 0, 0)
        self.compact_row0.setSpacing(SP_SM)
        self.compact_row1 = QHBoxLayout()
        self.compact_row1.setContentsMargins(0, 0, 0, 0)
        self.compact_row1.setSpacing(SP_SM)
        compact_outer.addLayout(self.compact_row0)
        compact_outer.addLayout(self.compact_row1)
        self.action_compact.hide()
        action_outer.addWidget(self.action_compact)

        self.status_label = ElidedLabel("")
        self.status_label.setMinimumWidth(0)
        self.status_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

        self.btn_cancel_synth = QPushButton("取消合成")
        self.btn_cancel_synth.setObjectName("DangerBtn")
        self.btn_cancel_synth.setFixedHeight(CONTROL_H)
        self.btn_cancel_synth.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.btn_cancel_synth.setCursor(Qt.PointingHandCursor)
        self.btn_cancel_synth.clicked.connect(self.cancel_synthesis)
        self.btn_cancel_synth.hide()

        self.btn_force_restart = QPushButton("强制重启引擎")
        self.btn_force_restart.setObjectName("DangerBtn")
        self.btn_force_restart.setFixedHeight(CONTROL_H)
        self.btn_force_restart.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.btn_force_restart.setToolTip("强制终止并重启卡住的本地引擎子进程")
        self.btn_force_restart.clicked.connect(self._force_restart_engine)
        self.btn_force_restart.hide()

        self.key_hint_action = QLabel("Ctrl+Enter")
        self.key_hint_action.setObjectName("KeyCap")
        self.key_hint_action.setFixedHeight(24)
        self.key_hint_action.setAlignment(Qt.AlignCenter)

        self.btn_generate = QPushButton("合成")
        self.btn_generate.setObjectName("PrimaryBtn")
        self.btn_generate.setFixedHeight(PRIMARY_H)
        self.btn_generate.setMinimumWidth(88)
        self.btn_generate.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.btn_generate.setCursor(Qt.PointingHandCursor)
        self.btn_generate.setDefault(True)
        self.btn_generate.setToolTip("开始合成（Ctrl+Enter）")
        self.btn_generate.clicked.connect(self._start_synthesis)

        self._action_widgets = (
            self.status_label,
            self.btn_cancel_synth,
            self.btn_force_restart,
            self.key_hint_action,
            self.btn_generate,
        )
        synth_col.addWidget(self.action_row)
        self._ctrl_split.addWidget(synth_well)

        self._relayout_action_row()
        QTimer.singleShot(0, self._relayout_action_row)
        self.script_editor.set_speed(self._exact_speed)
        self._update_output_summary()
        return card

    @staticmethod
    def _clear_layout(layout) -> None:
        while layout.count():
            layout.takeAt(0)

    def _relayout_action_row(self) -> None:
        """One row on wide layouts; split status over action buttons on narrow ones."""
        if not hasattr(self, "action_wide_layout"):
            return
        compact = self.width() < 1120
        status_widget = self._action_widgets[0]
        buttons = self._action_widgets[1:]
        self._clear_layout(self.action_wide_layout)
        self._clear_layout(self.compact_row0)
        self._clear_layout(self.compact_row1)

        if compact:
            self.compact_row0.addWidget(status_widget, 1)
            self.compact_row1.addStretch(1)
            for widget in buttons:
                self.compact_row1.addWidget(widget)
            self.action_wide.hide()
            self.action_compact.show()
        else:
            self.action_wide_layout.addWidget(status_widget, 1)
            for widget in buttons:
                self.action_wide_layout.addWidget(widget)
            self.action_compact.hide()
            self.action_wide.show()

    def _build_settings_popover(self) -> QFrame:
        """Rarely-touched config (engine URL / tray switch) as a floating panel."""
        host = QFrame(self, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        host.setObjectName("PopoverHost")
        host.setAttribute(Qt.WA_TranslucentBackground)
        host.setFixedWidth(DIALOG_W + SHADOW_MARGIN_WINDOW * 2)
        outer = QVBoxLayout(host)
        outer.setContentsMargins(
            SHADOW_MARGIN_WINDOW,
            SHADOW_MARGIN_WINDOW,
            SHADOW_MARGIN_WINDOW,
            SHADOW_MARGIN_WINDOW,
        )

        panel = QFrame()
        panel.setObjectName("Popover")
        outer.addWidget(panel)
        self._popover_shadow_layer = attach_shadow(panel, "focus", R_SHEET)
        root = QVBoxLayout(panel)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(0)

        head = ModuleHeaderBar("系统设置", "引擎地址与托盘", height=44, parent=panel)
        self.btn_close_settings = QPushButton()
        self.btn_close_settings.setObjectName("GhostBtn")
        self.btn_close_settings.setFixedSize(26, 26)
        self.btn_close_settings.setCursor(Qt.PointingHandCursor)
        self.btn_close_settings.setToolTip("关闭设置")
        self.btn_close_settings.clicked.connect(host.hide)
        head.add_right_widget(self.btn_close_settings)
        root.addWidget(head)

        body = QWidget()
        body.setObjectName("TransparentSlot")
        form = QVBoxLayout(body)
        form.setContentsMargins(SP_LG, SP_MD, SP_LG, SP_LG)
        form.setSpacing(SP_MD)
        root.addWidget(body)

        api_well = QFrame()
        api_well.setObjectName("WellBox")
        al = QVBoxLayout(api_well)
        al.setContentsMargins(SP_MD, SP_MD, SP_MD, SP_MD)
        al.setSpacing(6)
        api_title = QLabel("引擎 API 地址")
        api_title.setObjectName("RowTitle")
        al.addWidget(api_title)
        self.api_input = QLineEdit(self.cfg.api_url)
        self.api_input.setObjectName("CleanInput")
        self.api_input.setPlaceholderText(DEFAULT_API_URL)
        self.api_input.setFixedHeight(INPUT_H)
        self.api_input.editingFinished.connect(self._on_api_url_edited)
        al.addWidget(self.api_input)
        form.addWidget(api_well)

        tray_well = QFrame()
        tray_well.setObjectName("WellBox")
        tl = QHBoxLayout(tray_well)
        tl.setContentsMargins(SP_MD, SP_MD, SP_MD, SP_MD)
        tl.setSpacing(SP_MD)
        tv = QVBoxLayout()
        tv.setSpacing(2)
        tray_label = QLabel("最小化到托盘")
        tray_label.setObjectName("RowTitle")
        tray_desc = QLabel("关闭窗口时后台常驻，引擎保持在线")
        tray_desc.setObjectName("WeakMeta")
        tv.addWidget(tray_label)
        tv.addWidget(tray_desc)
        tl.addLayout(tv, 1)
        self.chk_tray = SlideSwitch()
        self.chk_tray.setToolTip("关闭窗口时最小化到系统托盘，引擎保持运行")
        self.chk_tray.setChecked(bool(self.cfg.minimize_to_tray))
        self.chk_tray.toggled.connect(self._on_tray_toggled)
        tl.addWidget(self.chk_tray)
        form.addWidget(tray_well)
        return host

    def _show_settings(self) -> None:
        pop = self._settings_popover
        pop.adjustSize()
        btn = self.btn_settings
        anchor = btn.mapToGlobal(QPoint(btn.width(), btn.height() + SP_SM))
        x = anchor.x() - (pop.width() - SHADOW_MARGIN_WINDOW)
        y = anchor.y() - SHADOW_MARGIN_WINDOW
        screen = QApplication.screenAt(anchor) or self.screen()
        if screen is not None:
            avail = screen.availableGeometry()
            x = max(avail.left(), min(x, avail.right() - pop.width() + 1))
            y = max(avail.top(), min(y, avail.bottom() - pop.height() + 1))
        pop.move(x, y - SP_SM)
        pop.setWindowOpacity(0.0)
        pop.show()

        pos_anim = QPropertyAnimation(pop, b"pos", self)
        pos_anim.setDuration(120)
        pos_anim.setEasingCurve(QEasingCurve.OutCubic)
        pos_anim.setStartValue(QPoint(x, y - SP_SM))
        pos_anim.setEndValue(QPoint(x, y))
        fade_anim = QPropertyAnimation(pop, b"windowOpacity", self)
        fade_anim.setDuration(120)
        fade_anim.setEasingCurve(QEasingCurve.OutCubic)
        fade_anim.setStartValue(0.0)
        fade_anim.setEndValue(1.0)
        self._popover_anims = (pos_anim, fade_anim)
        pos_anim.start()
        fade_anim.start()

    def _build_player_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("PlayerBar")
        bar.setFixedHeight(PLAYER_H)
        bar.setProperty("idle", "true")
        row = QHBoxLayout(bar)
        row.setContentsMargins(SP_LG, 0, SP_LG, 0)
        row.setSpacing(14)

        self.btn_play_pause = QPushButton()
        self.btn_play_pause.setObjectName("PlayBtn")
        self.btn_play_pause.setFixedSize(PLAYER_BTN, PLAYER_BTN)
        self.btn_play_pause.clicked.connect(self._toggle_play)
        row.addWidget(self.btn_play_pause)

        self.now_playing_label = ElidedLabel("暂无音频", mode=Qt.ElideMiddle)
        self.now_playing_label.setObjectName("NowPlaying")
        self.now_playing_label.setMinimumWidth(140)
        self.now_playing_label.setMaximumWidth(280)
        self.now_playing_label.setSizePolicy(
            QSizePolicy.Preferred, QSizePolicy.Preferred
        )
        row.addWidget(self.now_playing_label)

        self.seek_slider = SeekSlider()
        self.seek_slider.setRange(0, 1000)
        self.seek_slider.sliderPressed.connect(lambda: setattr(self, "_seek_dragging", True))
        self.seek_slider.sliderReleased.connect(self._on_seek_released)
        self.seek_slider.sliderMoved.connect(self._on_seek_moved)
        row.addWidget(self.seek_slider, 1)

        time_pill = QFrame()
        time_pill.setObjectName("BadgeNeutral")
        time_pill.setFixedHeight(26)
        tp_layout = QHBoxLayout(time_pill)
        tp_layout.setContentsMargins(10, 0, 10, 0)
        tp_layout.setSpacing(0)
        self.time_label = QLabel()
        self.time_label.setObjectName("PlayerTime")
        self.time_label.setTextFormat(Qt.RichText)
        self.time_label.setMinimumWidth(84)
        self.time_label.setAlignment(Qt.AlignCenter)
        tp_layout.addWidget(self.time_label)
        row.addWidget(time_pill)
        self._set_time_label(0, 0)

        if _HAS_QTMULTIMEDIA:
            self._player = QMediaPlayer(self)
            self._audio_output = QAudioOutput(self)
            self._player.setAudioOutput(self._audio_output)
            self._player.positionChanged.connect(self._on_player_position)
            self._player.durationChanged.connect(self._on_player_duration)
            self._player.playbackStateChanged.connect(lambda _state: self._update_play_icon())
            self._player.mediaStatusChanged.connect(self._on_media_status)
            self._player.errorOccurred.connect(self._on_player_error)
        else:
            self.btn_play_pause.setEnabled(False)
            self.btn_play_pause.setToolTip("QtMultimedia 不可用，音频预览已禁用")
            self.seek_slider.setEnabled(False)
        # Idle state: visible but inert until a synthesis provides audio.
        self.btn_play_pause.setEnabled(False)
        if _HAS_QTMULTIMEDIA:
            self.btn_play_pause.setToolTip("合成完成后可在此试听")
        self.seek_slider.setEnabled(False)
        self._update_play_icon()
        return bar

    def _build_history_panel(self) -> ModuleCard:
        card = ModuleCard(
            "历史档案",
            body_margins=(SP_LG, SP_MD, SP_LG, SP_LG),
            body_spacing=0,
        )

        self.history_count_label = QLabel()
        self.history_count_label.setObjectName("BadgeNeutral")
        self.history_count_label.setFixedHeight(22)
        self.history_count_label.setMinimumWidth(46)
        self.history_count_label.setAlignment(Qt.AlignCenter)
        card.add_header_widget(self.history_count_label)

        well = QFrame()
        well.setObjectName("WellBox")
        well_layout = QVBoxLayout(well)
        well_layout.setContentsMargins(SP_SM, SP_SM, SP_SM, SP_SM)
        well_layout.setSpacing(0)

        container = QWidget()
        container.setObjectName("HistoryScrollContainer")
        container.setStyleSheet("QWidget#HistoryScrollContainer { background: transparent; }")
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 2, 0)
        container_layout.setSpacing(0)
        self._history_cards_layout = QVBoxLayout()
        self._history_cards_layout.setContentsMargins(0, 0, 0, 0)
        self._history_cards_layout.setSpacing(SP_SM)
        container_layout.addLayout(self._history_cards_layout)
        container_layout.addStretch(1)

        self._history_scroll = QScrollArea()
        self._history_scroll.setFrameShape(QFrame.NoFrame)
        self._history_scroll.setWidgetResizable(True)
        self._history_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._history_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
        )
        self._history_scroll.setWidget(container)
        well_layout.addWidget(self._history_scroll, 1)

        self.history_empty_hint = EmptyStateWidget()
        well_layout.addWidget(self.history_empty_hint, 1)
        card.body_layout.addWidget(well, 1)
        return card

    def _on_log_toggled(self, visible: bool) -> None:
        if not hasattr(self, "log_panel"):
            return
        is_open = bool(visible)
        self.log_panel.setVisible(is_open)
        if getattr(self, "log_collapsed_strip", None) is not None:
            self.log_collapsed_strip.setVisible(not is_open)
        if is_open and hasattr(self, "right_splitter"):
            total = max(320, self.right_splitter.height())
            QTimer.singleShot(
                0,
                lambda: self.right_splitter.setSizes([max(240, total - 200), 200]),
            )
        self._schedule_save()

    def _build_log_panel(self) -> ModuleCard:
        panel = ModuleCard(
            "引擎运行日志",
            dot_color=THEME["ok"],
            body_margins=(SP_MD, SP_MD, SP_MD, SP_MD),
            body_spacing=0,
        )
        self.btn_open_engine_log = QPushButton("日志文件")
        self.btn_open_engine_log.setObjectName("SoftBtn")
        self.btn_open_engine_log.setFixedHeight(24)
        self.btn_open_engine_log.setCursor(Qt.PointingHandCursor)
        self.btn_open_engine_log.setToolTip("打开持久化 engine.log 日志文件")
        self.btn_open_engine_log.clicked.connect(self._open_engine_log)
        panel.add_header_widget(self.btn_open_engine_log)

        self.btn_close_log = QPushButton("收起")
        self.btn_close_log.setObjectName("SoftBtn")
        self.btn_close_log.setFixedHeight(24)
        self.btn_close_log.setCursor(Qt.PointingHandCursor)
        self.btn_close_log.setToolTip("收起日志")
        self.btn_close_log.clicked.connect(lambda: self.btn_toggle_log.setChecked(False))
        panel.add_header_widget(self.btn_close_log)

        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("LogView")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setMinimumHeight(150)
        self.log_view.setMaximumHeight(320)
        self.log_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        panel.body_layout.addWidget(self.log_view, 1)
        return panel

    def _open_engine_log(self) -> None:
        log_path = engine_log_path(self._base_dir)
        if not os.path.exists(log_path):
            append_engine_log("=== 日志文件已创建 ===", base_dir=self._base_dir)
        target = log_path if os.path.exists(log_path) else (os.path.dirname(os.path.abspath(log_path)) or self._base_dir)
        QDesktopServices.openUrl(QUrl.fromLocalFile(target))

    def _build_log_collapsed_strip(self) -> QFrame:
        strip = QFrame()
        strip.setObjectName("ModuleCard")
        strip.setFixedHeight(44)
        row = QHBoxLayout(strip)
        row.setContentsMargins(SP_LG, 0, SP_LG, 0)
        row.setSpacing(SP_SM)

        self.log_strip_dot = QFrame()
        self.log_strip_dot.setFixedSize(8, 8)
        self.log_strip_dot.setStyleSheet(dot_style("ok"))
        row.addWidget(self.log_strip_dot, 0, Qt.AlignVCenter)

        title = QLabel("引擎运行日志")
        title.setObjectName("SectionTitle")
        row.addWidget(title, 0, Qt.AlignVCenter)

        self.log_preview_label = ElidedLabel("· 最新：等待引擎响应")
        self.log_preview_label.setObjectName("WeakMeta")
        self.log_preview_label.setMinimumWidth(0)
        self.log_preview_label.setSizePolicy(
            QSizePolicy.Ignored, QSizePolicy.Preferred
        )
        row.addWidget(self.log_preview_label, 1)

        self.btn_expand_log = QPushButton("展开")
        self.btn_expand_log.setObjectName("SoftBtn")
        self.btn_expand_log.setFixedHeight(24)
        self.btn_expand_log.setCursor(Qt.PointingHandCursor)
        self.btn_expand_log.setToolTip("展开引擎运行日志")
        self.btn_expand_log.clicked.connect(lambda: self.btn_toggle_log.setChecked(True))
        row.addWidget(self.btn_expand_log, 0, Qt.AlignVCenter)
        return strip

    def _apply_icons(self) -> None:
        """Apply editorial icons using the warm-paper palette."""
        t = THEME
        muted, text, dim = t["muted"], t["text"], t["dim"]
        if getattr(self, "btn_top_models", None) is not None:
            self.btn_top_models.setIcon(
                stateful_icon("folder", muted, text, size=ICON_SIZE)
            )
        self.btn_settings.setIcon(
            stateful_icon("settings", muted, text, size=ICON_SIZE)
        )
        if getattr(self, "btn_close_settings", None) is not None:
            self.btn_close_settings.setIcon(
                stateful_icon("close", muted, text, size=14)
            )
        self.btn_toggle_log.setIcon(stateful_icon("log", muted, text, size=ICON_SIZE))
        self.btn_open_folder.setIcon(
            stateful_icon("folder", muted, text, dim, size=ICON_SIZE)
        )
        if getattr(self, "model_banner_icon", None) is not None:
            self.model_banner_icon.setPixmap(
                icon("alert", t["warning"], 15).pixmap(15, 15)
            )
        self._update_play_icon()

    def _update_play_icon(self) -> None:
        playing = (
            self._player is not None
            and self._player.playbackState() == QMediaPlayer.PlayingState
        )
        color = (
            THEME["btn_primary_text"]
            if self.btn_play_pause.isEnabled()
            else THEME["dim"]
        )
        self.btn_play_pause.setIcon(
            icon("pause" if playing else "play", color, 14, width=2.0)
        )

    # ------------------------------------------------------------------
    # Output summary strip
    # ------------------------------------------------------------------

    def _history_base_dir(self) -> str:
        """Resolved output folder for HistoryRepository relative-path healing."""
        return resolve_output_dir("", self.cfg.output_dir)

    @staticmethod
    def _nearest_existing_dir(path: str) -> str:
        """Walk up from ``path`` to the closest existing folder (else Desktop)."""
        cur = os.path.abspath(os.path.expanduser(path)) if path else ""
        while cur:
            if os.path.isdir(cur):
                return cur
            parent = os.path.dirname(cur)
            if not parent or parent == cur:
                break
            cur = parent
        return resolve_output_dir("", "")

    @staticmethod
    def _validate_output_dir_text(raw_text: str) -> tuple[str | None, str]:
        """Validate ``path_input`` without creating directories on disk."""
        cleaned = (raw_text or "").strip().strip('"')
        if not cleaned:
            target = resolve_output_dir("", "")
            if os.path.exists(target) and not os.path.isdir(target):
                return None, "默认桌面路径被同名文件占用"
            return target, ""

        _drive, rest = os.path.splitdrive(cleaned)
        if any(ch in _WIN_INVALID_PATH_CHARS or ord(ch) < 32 for ch in rest) or ":" in rest:
            return None, "路径包含非法字符"

        parts = [part for part in rest.replace("\\", "/").split("/") if part]
        for seg in parts:
            if seg in (".", ".."):
                continue
            if seg.endswith(" ") or (seg.endswith(".") and seg not in (".", "..")):
                return None, "目录名不能以空格或句点结尾"
            if is_reserved_windows_name(seg):
                return None, f"目录名含系统保留字 {seg}"

        try:
            target = os.path.abspath(os.path.expanduser(cleaned))
        except OSError:
            return None, "路径格式无效"

        drv, _ = os.path.splitdrive(target)
        if drv:
            if drv.startswith("\\\\"):
                if not (
                    os.path.exists(target)
                    or os.path.exists(drv + os.sep)
                    or os.path.exists(drv)
                ):
                    return None, f"网络共享路径不可访问（{drv}）"
            elif not os.path.exists(drv + os.sep):
                return None, f"磁盘或驱动器不存在（{drv}）"

        cur = target
        while cur:
            if os.path.exists(cur):
                if os.path.isdir(cur):
                    return target, ""
                return None, (
                    "目标路径是已存在的文件"
                    if os.path.normcase(cur) == os.path.normcase(target)
                    else "上级路径是已存在的文件"
                )
            parent = os.path.dirname(cur)
            if not parent or parent == cur:
                break
            cur = parent
        return target, ""

    def _resolve_or_prepare_output_dir(self, *, create: bool) -> str:
        """Resolve visible output path without side-effects unless ``create=True``."""
        target, err = self._validate_output_dir_text(self.path_input.text())
        if err or target is None:
            if create:
                raise OSError(err or "输出目录无效")
            return ""
        if create:
            os.makedirs(target, exist_ok=True)
            probe = os.path.join(target, f".mambo_write_probe_{uuid.uuid4().hex}")
            try:
                with open(probe, "wb") as handle:
                    handle.write(b"")
            finally:
                remove_quiet(probe)
        return target

    def _current_output_dir(self) -> str:
        """Non-creating output directory resolution for UI preview & save."""
        return self._resolve_or_prepare_output_dir(create=False)

    def _output_path_display(self, raw: str | None = None) -> str:
        raw = self._current_output_dir() if raw is None else raw
        if not raw:
            return "路径无效"
        norm = os.path.normpath(os.path.abspath(raw))
        desktop = os.path.normpath(os.path.abspath(_known_desktop()))
        if os.path.normcase(norm) == os.path.normcase(desktop):
            return "桌面"
        drive, rest = os.path.splitdrive(norm)
        parts = [part for part in rest.replace("\\", "/").split("/") if part]
        if not parts and drive:
            return f"{drive}\\"
        if len(parts) <= 2:
            return norm
        return f"{drive}\\…\\{parts[-1]}"

    def _update_output_summary(self) -> None:
        raw_input = (self.path_input.text() or "").strip()
        target, err = self._validate_output_dir_text(raw_input)
        custom = self.name_input.text().strip()
        naming = f"文件名 {custom}" if custom else "按首句命名"
        if err or target is None:
            detail = err or "路径无效"
            self.output_summary.setFullText(f"输出目录无效（{detail}）　·　{naming}")
            self.output_summary.setToolTip(f"{raw_input}\n错误：{detail}\n{naming}")
            self.path_input.setToolTip(f"{raw_input}\n错误：{detail}")
            if getattr(self, "_strip_status", None) is not None:
                self._strip_status.setText(detail)
                self._strip_status.setStyleSheet(f"color: {THEME['danger']}; font-size: 12px;")
                self._strip_status.setVisible(True)
            return
        suffix = "" if os.path.isdir(target) else "（合成时自动新建）"
        disp = self._output_path_display(target)
        self.output_summary.setFullText(
            f"保存至 {disp}{suffix}　·　{naming}"
        )
        self.output_summary.setToolTip(f"{target}{suffix}\n{naming}")
        self.path_input.setToolTip(f"{target}{suffix}")
        if getattr(self, "_strip_status", None) is not None:
            if suffix:
                self._strip_status.setText("合成时自动新建")
                self._strip_status.setStyleSheet(f"color: {THEME['muted']}; font-size: 12px;")
                self._strip_status.setVisible(True)
            else:
                self._strip_status.setVisible(False)

    # ------------------------------------------------------------------
    # Progress fade & player helpers
    # ------------------------------------------------------------------

    def _set_progress_visible(self, visible: bool) -> None:
        previous = self._progress_anim
        if previous is not None:
            previous.stop()
        if visible:
            self.progress_bar.show()
        anim = QPropertyAnimation(self._progress_effect, b"opacity", self)
        anim.setDuration(150)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setStartValue(self._progress_effect.opacity())
        anim.setEndValue(1.0 if visible else 0.0)
        if not visible:
            anim.finished.connect(self.progress_bar.hide)
        self._progress_anim = anim
        anim.start()

    def _set_time_label(self, position_ms: int, duration_ms: int) -> None:
        self.time_label.setText(
            f'<span style="color:{THEME["text"]};">{_fmt_time(position_ms)}</span>'
            f'<span style="color:{THEME["dim"]};"> / {_fmt_time(duration_ms)}</span>'
        )

    def _on_media_status(self, status) -> None:
        if self._player is not None and status == QMediaPlayer.EndOfMedia:
            self._clear_playing_marker()
            self._update_play_icon()

    def _mark_playing(self, path: str) -> None:
        wanted = os.path.normcase(os.path.abspath(path)) if path else ""
        for card in self._history_widgets:
            card_path = card.record.path or ""
            matches = bool(wanted) and (
                os.path.normcase(os.path.abspath(card_path)) == wanted
            )
            card.set_playing(matches)

    def _clear_playing_marker(self) -> None:
        for card in self._history_widgets:
            card.set_playing(False)

    def _apply_player_idle(self, idle: bool) -> None:
        self.player_bar.setProperty("idle", "true" if idle else "false")
        self.player_bar.style().unpolish(self.player_bar)
        self.player_bar.style().polish(self.player_bar)
        self.player_bar.update()

    # ------------------------------------------------------------------
    # Status dot breathing (busy only)
    # ------------------------------------------------------------------

    def _start_dot_breathing(self) -> None:
        if self._dot_anim is None:
            anim = QPropertyAnimation(self._dot_effect, b"opacity", self)
            anim.setDuration(1600)
            anim.setLoopCount(-1)
            anim.setStartValue(0.4)
            anim.setKeyValueAt(0.5, 1.0)
            anim.setEndValue(0.4)
            anim.setEasingCurve(QEasingCurve.InOutSine)
            self._dot_anim = anim
        if self._dot_anim.state() != QPropertyAnimation.Running:
            self._dot_anim.start()

    def _stop_dot_breathing(self) -> None:
        if self._dot_anim is not None:
            self._dot_anim.stop()
        self._dot_effect.setOpacity(1.0)

    # ------------------------------------------------------------------
    # Stack: setup view <-> studio
    # ------------------------------------------------------------------

    def _ensure_setup_view(self) -> EmbeddedSetupView:
        if self.setup_view is None:
            self.setup_view = EmbeddedSetupView(base_dir=self._base_dir)
            self.setup_view.setup_completed.connect(self._on_setup_completed)
            self.stack.insertWidget(0, self.setup_view)  # spec: index 0 = setup
        return self.setup_view

    def show_setup_view(self) -> None:
        self._ensure_setup_view()
        timer = getattr(self, "_heartbeat_timer", None)
        if timer is not None:
            timer.stop()
        self.stack.setCurrentIndex(0)

    def show_studio(self) -> None:
        self.stack.setCurrentWidget(self.studio_page)
        timer = getattr(self, "_heartbeat_timer", None)
        if timer is not None and not timer.isActive():
            timer.start()

    def _set_engine_dot(self, dot_kind: str) -> None:
        """Update only the top-bar health dot, pill state, and breathing animation."""
        dot = getattr(self, "engine_dot", None)
        if dot is not None:
            dot.setStyleSheet(dot_style(dot_kind))
        pill = getattr(self, "engine_pill", None)
        if pill is not None:
            pill.setProperty("state", dot_kind)
            pill.style().unpolish(pill)
            pill.style().polish(pill)
            pill.update()
        if getattr(self, "_dot_effect", None) is not None:
            if dot_kind == "busy":
                self._start_dot_breathing()
            else:
                self._stop_dot_breathing()

    def _on_setup_completed(self) -> None:
        self._log("ok", "引擎安装完成，正在进入工作室…")
        self.show_studio()
        self._check_model_files()
        QApplication.alert(self, 0)
        if self._tray is not None:
            self._tray.showMessage(
                "MamboTTS", "引擎安装完成，可以开始合成。", QSystemTrayIcon.Information, 3000
            )
        QTimer.singleShot(_STARTUP_PROBE_DELAY_MS, self._startup_probe)

    # ------------------------------------------------------------------
    # Engine probing / lifecycle (all async)
    # ------------------------------------------------------------------

    def _current_api_url(self) -> str:
        return self.api_input.text().strip() or DEFAULT_API_URL

    def _set_engine_label(self, text: str) -> None:
        label = getattr(self, "engine_status_label", None)
        if label is not None:
            label.setText(text)
            self._refresh_engine_endpoint()

    def _refresh_engine_endpoint(self) -> None:
        """Keep the top-bar endpoint label + hover hints in sync with the URL."""
        if not hasattr(self, "api_input"):
            return
        url = self._current_api_url()
        tip = f"引擎地址：{url}"
        dot = getattr(self, "engine_dot", None)
        if dot is not None:
            dot.setToolTip(tip)
        state_label = getattr(self, "engine_status_label", None)
        if state_label is not None:
            state_label.setToolTip(tip)
        url_label = getattr(self, "engine_url_label", None)
        if url_label is not None:
            url_label.setText(url.split("://", 1)[-1].rstrip("/"))
            url_label.setToolTip(tip)

    def _startup_probe(self, auto_start: bool = True, *, preserve_status: bool = False) -> None:
        """One-shot async probe deciding whether to spawn the engine."""
        if self._closing or self.stack.currentWidget() is not self.studio_page:
            return
        self._probe_generation += 1
        self._probe_in_flight = False
        gen = self._probe_generation
        self._probe_preserve_gen = gen if preserve_status else 0
        url = self._current_api_url()
        self._set_engine_dot("busy")
        if not preserve_status:
            self._set_status("正在检测引擎…", "busy")
        self._set_engine_label("检测中")

        def work() -> None:
            try:
                online = GSVHttpClient(api_url=url).is_api_running(timeout=2.0, api_url=url)
            except Exception:  # noqa: BLE001 - probe must never raise
                online = False
            _safe_emit(self._probe_done, online, gen, url, bool(auto_start))

        threading.Thread(target=work, daemon=True, name="engine-startup-probe").start()

    def _on_startup_probe(
        self,
        online: bool,
        gen: int | None = None,
        url: str = "",
        auto_start: bool = True,
    ) -> None:
        if self._closing:
            return
        if gen is not None and gen != self._probe_generation:
            return
        preserve_status = gen is not None and gen == self._probe_preserve_gen
        self._probe_preserve_gen = 0
        if self._force_restarting:
            return
        cur = self._current_api_url()
        if url and url != cur:
            return
        if online:
            self._set_engine_online(preserve_status=preserve_status)
            self._log("ok", f"检测到引擎在线：{cur}")
            return
        if auto_start and self._auto_start_engine and is_local_api_url(cur):
            # Jump straight into the starting state; showing 引擎离线 here
            # would mislead first-time users while the engine warms up.
            self._start_engine_subprocess()
        else:
            self._set_engine_offline(preserve_status=preserve_status)
            self._log("warn", f"引擎离线（{cur}）。可手动重试，或运行引擎后重连。")

    def _start_engine_subprocess(self) -> None:
        url = self._current_api_url()
        if not is_local_api_url(url):
            self._set_engine_dot("error")
            self._set_status("非本机 API 地址，拒绝自动启动引擎", "error")
            self._set_engine_label("地址无效")
            self._log("error", f"非本机地址不启动引擎子进程：{url}")
            self.btn_engine_retry.setVisible(True)
            return
        self._start_generation += 1
        start_gen = self._start_generation
        self._starting_url = url
        self._starting_in_flight = True
        self._engine_starting = True
        self._engine_start_deadline = time.monotonic() + _ENGINE_START_TIMEOUT_SEC
        self.btn_engine_retry.setVisible(False)
        self._set_engine_dot("busy")
        self._set_status("正在启动本地引擎…", "busy")
        self._set_engine_label("引擎启动中")
        self._log("info", f"正在启动本地引擎子进程（{url}）…")

        def work() -> None:
            ok = False
            message = ""
            try:
                ok, message = self.supervisor.start(url, bind_job_object=True)
            except Exception as exc:  # noqa: BLE001 - guarantee signal emit
                ok = False
                message = f"启动引擎线程异常：{exc}"
            finally:
                _safe_emit(self._start_done, bool(ok), str(message), start_gen)

        threading.Thread(target=work, daemon=True, name="engine-starter").start()

    def _on_engine_start_done(
        self, ok: bool, message: str, gen: int | None = None
    ) -> None:
        if self._closing:
            return
        if gen is not None and gen != self._start_generation:
            return
        self._starting_in_flight = False
        if self._force_restarting:
            return
        if self._starting_url and self._starting_url != self._current_api_url():
            return
        self._log("ok" if ok else "error", message)
        if ok:
            if self._engine_online:
                self._engine_starting = False
                return
            # The starting latch stays ON until the first successful
            # heartbeat releases it (_set_engine_online): model loading takes
            # 10-30s and failed probes in that window must never flash
            # 引擎离线 + the retry button.
            self._engine_starting = True
            self._set_engine_dot("busy")
            self._set_status("引擎启动中，等待模型加载完成（约 10-30 秒）…", "busy")
            self._set_engine_label("引擎启动中")
            self._heartbeat_timer.setInterval(_HEARTBEAT_STARTUP_MS)
        else:
            self._engine_starting = False
            self._set_engine_dot("error")
            self._set_status("引擎启动失败", "error")
            self._set_engine_label("启动失败")
            self.btn_engine_retry.setVisible(True)

    def _heartbeat_tick(self) -> None:
        """Periodic /control probe; skipped off studio page, while busy or probing."""
        if self.stack.currentWidget() is not self.studio_page:
            return
        if self._force_restarting:
            return
        if self._synth_worker is not None and self._synth_worker.is_alive():
            return
        if not self.supervisor.should_poll_health():
            return
        if self._probe_in_flight:
            return
        self._probe_in_flight = True
        gen = self._probe_generation
        url = self._current_api_url()

        def work() -> None:
            try:
                online = GSVHttpClient(api_url=url).is_api_running(timeout=1.5, api_url=url)
            except Exception:  # noqa: BLE001
                online = False
            _safe_emit(self._heartbeat_done, online, gen, url)

        threading.Thread(target=work, daemon=True, name="engine-heartbeat").start()

    def _on_heartbeat_result(
        self,
        online: bool,
        gen: int | None = None,
        url: str = "",
    ) -> None:
        if self._closing:
            self._probe_in_flight = False
            return
        if gen is not None and gen != self._probe_generation:
            return
        self._probe_in_flight = False
        if self._force_restarting:
            return
        if url and url != self._current_api_url():
            return
        if online:
            self._set_engine_online()
        else:
            self._set_engine_offline()

    def _set_engine_online(self, *, preserve_status: bool = False) -> None:
        first_online = not self._engine_online
        self._engine_online = True
        self._engine_starting = False
        self._starting_in_flight = False
        self._force_restarting = False
        self._engine_start_deadline = 0.0
        self.supervisor.mark_online(True)
        if first_online and not self.supervisor.is_running():
            self.gsv_client.invalidate_active_models()
        self._set_engine_dot("ok")
        # Online is already announced by the top-bar dot + label; the action
        # row stays silent until there is a real job message. Preserve any
        # active synthesis error/completion status on background checks.
        if first_online and not preserve_status and self._status_kind != "error":
            self._set_status("", "ok")
        self._set_engine_label("引擎在线")
        self.btn_engine_retry.setVisible(False)
        self._heartbeat_timer.setInterval(_HEARTBEAT_ONLINE_MS)

    def _set_engine_offline(self, *, preserve_status: bool = False) -> None:
        self._engine_online = False
        self.supervisor.mark_online(False)
        url = self._current_api_url()
        if self._engine_starting and (
            self._starting_in_flight or self.supervisor.is_running_for(url)
        ):
            timed_out = (
                self._engine_start_deadline > 0.0
                and time.monotonic() >= self._engine_start_deadline
            )
            if timed_out:
                self._engine_starting = False
                self._engine_start_deadline = 0.0
                self._set_engine_dot("error")
                self._set_status("引擎启动超时（120 秒未就绪），请点击「重试启动」", "error")
                self._set_engine_label("启动超时")
                self._log(
                    "error",
                    f"引擎启动超过 {_ENGINE_START_TIMEOUT_SEC:.0f} 秒仍未响应，可点击重试强制重启。",
                )
                self.btn_engine_retry.setVisible(True)
            else:
                # Managed engine still warming up (or Popen in progress): keep the
                # starting state instead of the misleading 引擎离线.
                self._set_engine_dot("busy")
                self._set_status("引擎启动中，等待就绪…", "busy")
                self._set_engine_label("引擎启动中")
                self.btn_engine_retry.setVisible(False)
        else:
            self._engine_starting = False
            self._engine_start_deadline = 0.0
            self._set_engine_dot("warn")
            if not preserve_status and self._status_kind != "error":
                self._set_status("引擎离线", "warn")
            self._set_engine_label("引擎离线")
            self.btn_engine_retry.setVisible(True)

    def _on_engine_retry(self) -> None:
        self.btn_engine_retry.setVisible(False)
        url = self._current_api_url()
        if is_local_api_url(url):
            if self.supervisor.is_running_for(url):
                self._force_restart_engine()
            else:
                self._start_engine_subprocess()
        else:
            self._startup_probe(auto_start=False)

    def _on_engine_line(self, line: str) -> None:
        line = (line or "").strip()
        if line:
            self._log("engine", line)

    # ------------------------------------------------------------------
    # Synthesis
    # ------------------------------------------------------------------

    def _on_generate_shortcut(self) -> None:
        if self.stack.currentWidget() is not self.studio_page:
            return
        if self.btn_generate.isEnabled():
            self._start_synthesis()

    def _first_script_line(self, text: str) -> str:
        for line in (text or "").splitlines():
            if has_pronounceable_char(line):
                return line
        return text or ""

    def _effective_speed(self) -> float:
        """Preserve exact non-0.05 speed from config until user moves the slider."""
        if self.speed_slider.value() == self._initial_slider_val:
            return self._exact_speed
        return self.speed_slider.value() / 100.0

    def _build_job_params(self, text: str) -> dict:
        out_dir = self._resolve_or_prepare_output_dir(create=True)
        custom = self.name_input.text().strip()
        if custom:
            stem = custom[:-4] if custom.lower().endswith(".wav") else custom
            basename = ensure_wav_suffix(sanitize_stem(stem) or "mambo")
        else:
            basename = filename_from_text(self._first_script_line(text))
        save_path = unique_path(os.path.join(out_dir, basename))
        return {
            "text": text,
            "save_path": save_path,
            "speed": self._effective_speed(),
            "api_url": self._current_api_url(),
            "export_srt": self.chk_export_srt.isChecked(),
            "trim_silence": self.cfg.trim_silence,
            "normalize_audio": self.cfg.normalize_audio,
            "use_cache": True,
        }

    def _launch_worker(self, params: dict, status_text: str) -> None:
        self._cancel_requested = False
        worker = GenerateWorker(self.pipeline, params)
        worker.signals.progress.connect(self._on_generate_progress)
        worker.signals.done.connect(self._on_generate_done)
        self._synth_worker = worker
        self._synth_started_mono = time.monotonic()
        self._synth_stage_text = status_text
        self._set_busy(True)
        self.progress_bar.setValue(0)
        self._set_progress_visible(True)
        self._set_status(status_text, "busy")
        self._synth_elapsed_timer.start()
        self._log("info", f"开始合成 → {params.get('save_path', '')}")
        worker.start()

    def _on_synth_elapsed_tick(self) -> None:
        worker = self._synth_worker
        if (
            self._closing
            or worker is None
            or not worker.is_alive()
            or self._cancel_requested
        ):
            self._synth_elapsed_timer.stop()
            return
        elapsed = int(max(0.0, time.monotonic() - self._synth_started_mono))
        if elapsed < 3:
            return
        stage = self._synth_stage_text or "合成中…"
        if elapsed >= 8 and self.progress_bar.value() == 0:
            self._set_status(f"{stage}（单路引擎处理/排队中 · 已耗时 {elapsed}s）", "busy")
        else:
            self._set_status(f"{stage}（已耗时 {elapsed}s）", "busy")

    def _start_synthesis(self) -> None:
        if self._synth_worker is not None and self._synth_worker.is_alive():
            return
        text = self.script_editor.text_input.toPlainText()
        if not text.strip():
            self._set_status("请先输入配音脚本", "warn")
            self._log("warn", "脚本为空，未开始合成。")
            return
        if not has_pronounceable_char(text):
            self._set_status("脚本中没有可朗读的文字或数字", "warn")
            self._log("warn", "脚本仅包含标点或空白标签，未开始合成。")
            return
        if not self._engine_online:
            self._set_status("引擎离线，无法合成", "error")
            self._log("error", "引擎离线，请等待引擎上线后再开始合成。")
            return
        try:
            params = self._build_job_params(text)
        except OSError as exc:
            self._set_status("输出目录无效或不可写", "error")
            self._log("error", f"输出目录无效或不可写：{exc}")
            return
        self._last_text = text
        self._last_params = dict(params)
        self._launch_worker(params, "合成中…")

    def _on_generate_progress(self, done: int, total: int, stage: str) -> None:
        if total > 0:
            self.progress_bar.setValue(int(done * 100 / total))
        if not self._cancel_requested:
            self._synth_stage_text = str(stage)
            self._set_status(f"{stage}", "busy")

    def _on_generate_done(self, result: object) -> None:
        if self._closing:
            return
        worker = self._synth_worker
        was_canceled = self._cancel_requested or bool(
            worker is not None and worker.cancel_event.is_set()
        )
        self._synth_elapsed_timer.stop()
        self._cancel_wait_timer.stop()
        self._cancel_wait_sec = 0
        self._cancel_can_force_restart = False
        self._cancel_requested = False
        self.btn_force_restart.hide()
        self._synth_worker = None
        self._set_busy(False)
        result = result if isinstance(result, SynthesisJobResult) else None
        if result is None:
            if was_canceled or self._force_restarting:
                self._log("info", "合成已取消。")
                if not (self._force_restarting or self._engine_starting):
                    self._set_status("已取消合成", "warn")
                return
            self._set_status("合成失败", "error")
            self._log("error", "合成线程返回了无效结果。")
            return
        if result.success:
            # A verified HTTP synthesis is the only online-state writer besides
            # the heartbeat; the pipeline already called mark_online, we sync UI.
            self.supervisor.mark_online(True)
            self._engine_online = True
            self._set_engine_dot("ok")
            self._set_status(f"合成完成：{result.duration_sec:.1f} 秒", "ok")
            srt_note = "（含 SRT 字幕）" if result.srt_path else ""
            self._log("ok", f"{result.message}{srt_note} → {result.wav_path}")
            self._last_save_path = result.wav_path
            self._remember_history(result)
            self._load_audio(result.wav_path)
            if self.isMinimized() or not self.isVisible():
                if self._tray is not None:
                    self._tray.showMessage(
                        "MamboTTS 合成完成",
                        f"{os.path.basename(result.wav_path)}（{result.duration_sec:.1f} 秒）",
                        QSystemTrayIcon.Information,
                        3000,
                    )
                else:
                    QApplication.alert(self, 0)
        elif was_canceled or "取消" in (result.message or ""):
            self._log("info", result.message or "合成已取消。")
            if not (self._force_restarting or self._engine_starting):
                self._set_status("已取消合成", "warn")
        else:
            detail_lines = [ln.strip() for ln in (result.message or "").splitlines() if ln.strip()]
            short_reason = detail_lines[0] if detail_lines else "请查看引擎运行日志"
            if len(short_reason) > 48:
                short_reason = short_reason[:47] + "…"
            self._set_status(f"合成失败：{short_reason}", "error")
            self._log("error", result.message)
            if any(k in (result.message or "") for k in ("断开", "超时", "连接")):
                QTimer.singleShot(
                    300,
                    lambda: self._startup_probe(auto_start=False, preserve_status=True)
                    if not self._closing
                    else None,
                )

    def cancel_synthesis(self) -> None:
        """Cooperative cancel: set the event; UI restores when the worker exits."""
        worker = self._synth_worker
        if worker is None or not worker.is_alive():
            return
        self._cancel_requested = True
        self._synth_elapsed_timer.stop()
        worker.cancel_event.set()
        self._cancel_wait_sec = 0
        self._cancel_can_force_restart = bool(
            is_local_api_url(self._current_api_url()) and self.supervisor.is_running()
        )
        self.btn_cancel_synth.setText("正在取消…")
        self.btn_cancel_synth.setEnabled(False)
        if self._cancel_can_force_restart:
            self._set_status(
                f"正在等待引擎停止当前推理…（{_FORCE_RESTART_WAIT_SEC} 秒后可强制重启）",
                "busy",
            )
        else:
            self._set_status(
                "正在取消，等待外部/独立引擎结束当前请求…",
                "busy",
            )
        self._cancel_wait_timer.start()
        self._log("info", "已请求取消，等待当前请求结束…")

    def _on_cancel_wait_tick(self) -> None:
        worker = self._synth_worker
        if worker is None or not worker.is_alive():
            self._cancel_wait_timer.stop()
            self.btn_force_restart.hide()
            return
        self._cancel_wait_sec += 1
        if self._cancel_can_force_restart and self.supervisor.is_running():
            if self._cancel_wait_sec < _FORCE_RESTART_WAIT_SEC:
                rem = _FORCE_RESTART_WAIT_SEC - self._cancel_wait_sec
                self._set_status(f"正在等待引擎停止当前推理…（{rem} 秒后可强制重启）", "busy")
            else:
                self.btn_force_restart.show()
                self._set_status(
                    "当前请求响应较慢，可继续等待或点击“强制重启引擎”",
                    "warn",
                )
        else:
            extra = (
                " · 若长期卡住可在终端运行 engine stop"
                if self._cancel_wait_sec >= 15 and is_local_api_url(self._current_api_url())
                else ""
            )
            self._set_status(
                f"正在等待外部/独立引擎结束当前请求（已等待 {self._cancel_wait_sec}s{extra}）…",
                "busy",
            )

    def _force_restart_engine(self) -> None:
        if self._force_restarting:
            return
        self._synth_elapsed_timer.stop()
        self._cancel_wait_timer.stop()
        self._cancel_wait_sec = 0
        self.btn_force_restart.hide()
        worker = self._synth_worker
        if worker is not None and worker.is_alive():
            self._cancel_requested = True
            worker.cancel_event.set()
        self._force_restarting = True
        self._probe_generation += 1
        self._probe_in_flight = False
        self._start_generation += 1
        restart_gen = self._start_generation
        self._engine_online = False
        self._engine_starting = False
        self._starting_in_flight = False
        self._starting_url = ""
        self.supervisor.mark_online(False)
        self._set_engine_dot("busy")
        self._set_engine_label("重启中")
        self._set_status("正在强制重启本地引擎…", "busy")
        self._log("warn", "取消等待超时，正在后台强制终止并重启引擎子进程…")

        def work() -> None:
            try:
                self.supervisor.stop()
            finally:
                _safe_emit(self._force_restart_done, restart_gen)

        threading.Thread(
            target=work, daemon=True, name="engine-force-restart"
        ).start()

    def _on_force_restart_done(self, restart_gen: int) -> None:
        if self._closing or restart_gen != self._start_generation:
            return
        self._force_restarting = False
        self.gsv_client.reset_active_models()
        if is_local_api_url(self._current_api_url()):
            self._start_engine_subprocess()
        else:
            self._set_engine_offline()

    def _set_busy(self, busy: bool) -> None:
        if busy and getattr(self, "_settings_popover", None) is not None:
            if self._settings_popover.isVisible():
                self._settings_popover.hide()
        for widget in (
            self.script_editor.text_input,
            self.name_input,
            self.path_input,
            self.btn_browse,
            self.speed_slider,
            self.btn_generate,
            self.api_input,
            self.btn_settings,
        ):
            widget.setEnabled(not busy)
            if busy:
                if widget not in self._saved_tooltips:
                    self._saved_tooltips[widget] = widget.toolTip()
                widget.setToolTip(_BUSY_TOOLTIP)
            else:
                original = self._saved_tooltips.pop(
                    widget,
                    "" if widget.toolTip() == _BUSY_TOOLTIP else widget.toolTip(),
                )
                widget.setToolTip(original)
        self.script_editor.set_locked(busy)
        self.chk_export_srt.setEnabled(not busy)
        self._srt_label.setEnabled(not busy)
        self.key_hint_action.setVisible(not busy)
        self.btn_cancel_synth.setVisible(busy)
        self.btn_cancel_synth.setText("取消合成")
        self.btn_cancel_synth.setEnabled(True)
        if not busy:
            self.btn_force_restart.hide()
        self.btn_generate.setText("合成中…" if busy else "合成")
        if not busy:
            self._set_progress_visible(False)

    # ------------------------------------------------------------------
    # Player
    # ------------------------------------------------------------------

    def _schedule_temp_removal(self, path: str, attempt: int = 0) -> None:
        """Delete staged temp playback WAV; retry briefly on Windows file lock."""
        if not path or not os.path.exists(path):
            return
        try:
            os.remove(path)
        except OSError:
            if attempt < 8:
                QTimer.singleShot(
                    250,
                    lambda p=path, n=attempt + 1: self._schedule_temp_removal(p, n),
                )

    def _release_player_source(self) -> None:
        self._play_copy_gen += 1
        if self._player is not None:
            self._player.stop()
            self._player.setSource(QUrl())
            if hasattr(self._player, "setSourceDevice"):
                self._player.setSourceDevice(None)
        if self._player_buffer is not None:
            old_buf = self._player_buffer
            self._player_buffer = None
            old_buf.close()
            old_buf.setData(b"")
            old_buf.deleteLater()
        self._player_bytes = None
        if self._temp_play_file:
            old_tmp = self._temp_play_file
            self._temp_play_file = ""
            self._schedule_temp_removal(old_tmp)

    def _reset_player_idle(self) -> None:
        self._release_player_source()
        self._current_audio_path = ""
        self._has_audio = False
        self._player_duration = 0
        self.seek_slider.setValue(0)
        self.seek_slider.setEnabled(False)
        self.btn_play_pause.setEnabled(False)
        self.now_playing_label.setFullText("暂无音频")
        self._set_time_label(0, 0)
        self._apply_player_idle(True)
        self._clear_playing_marker()
        self._update_play_icon()

    def _on_player_error(self, _error, msg: str) -> None:
        detail = (msg or "未知错误").strip()
        self._set_status(f"音频播放失败：{detail}", "warn")
        self._log("warn", f"播放失败：{detail}")
        self._reset_player_idle()

    def _load_audio(self, path: str) -> None:
        if not path or not os.path.exists(path):
            return
        self._release_player_source()
        self._current_audio_path = path
        self._has_audio = True
        self.player_bar.setVisible(True)
        self._apply_player_idle(False)
        self.btn_play_pause.setEnabled(self._player is not None)
        self.now_playing_label.setFullText(os.path.basename(path))
        self._mark_playing(path)
        if self._player is None:
            self._update_play_icon()
            return
        self._player_duration = 0
        self.seek_slider.setValue(0)
        self._set_time_label(0, 0)
        try:
            file_size = os.path.getsize(path)
            if file_size <= _PLAYBACK_RAM_LIMIT_BYTES and hasattr(self._player, "setSourceDevice"):
                with open(path, "rb") as handle:
                    data = handle.read()
                self._player_bytes = QByteArray(data)
                buf = QBuffer(self)
                buf.setData(self._player_bytes)
                buf.open(QBuffer.ReadOnly)
                self._player_buffer = buf
                self._player.setSourceDevice(buf, QUrl.fromLocalFile(path))
            else:
                copy_gen = self._play_copy_gen
                tmp_path = os.path.join(
                    tempfile.gettempdir(),
                    f"mambotts_play_{os.getpid()}_{uuid.uuid4().hex}.wav",
                )
                self.btn_play_pause.setEnabled(False)
                self.now_playing_label.setFullText(f"正在加载 {os.path.basename(path)}…")

                def _copy_for_play() -> None:
                    try:
                        shutil.copyfile(path, tmp_path)
                        _safe_emit(self._play_copy_done, copy_gen, tmp_path, "")
                    except OSError as exc:
                        remove_quiet(tmp_path)
                        _safe_emit(self._play_copy_done, copy_gen, "", str(exc))

                threading.Thread(target=_copy_for_play, daemon=True, name="play-copy").start()
                return
        except OSError as exc:
            self._log("warn", f"加载音频失败：{exc}")
            self._reset_player_idle()
            return
        self._player.play()
        # Icon only -- a literal "⏸" text renders as a blue Windows emoji
        # next to our SVG icon on real machines.
        self._update_play_icon()

    def _on_play_copy_done(self, copy_gen: int, tmp_path: str, err: str) -> None:
        if self._closing or copy_gen != self._play_copy_gen:
            if tmp_path:
                remove_quiet(tmp_path)
            return
        if err or not tmp_path or self._player is None:
            self._log("warn", f"加载音频失败：{err or '播放器不可用'}")
            self._reset_player_idle()
            return
        self._temp_play_file = tmp_path
        self.btn_play_pause.setEnabled(True)
        self.now_playing_label.setFullText(os.path.basename(self._current_audio_path or tmp_path))
        self._player.setSource(QUrl.fromLocalFile(tmp_path))
        self._player.play()
        self._update_play_icon()

    def _toggle_play(self) -> None:
        if self._player is None or not self._has_audio:
            return
        if self._player.playbackState() == QMediaPlayer.PlayingState:
            self._player.pause()
        else:
            if (
                self._player.mediaStatus() == QMediaPlayer.EndOfMedia
                or (
                    self._player_duration > 0
                    and self._player.position() >= max(0, self._player_duration - 50)
                )
            ):
                self._player.setPosition(0)
            if self._current_audio_path:
                self._mark_playing(self._current_audio_path)
            self._player.play()
        self._update_play_icon()

    def _on_player_position(self, position_ms: int) -> None:
        if self._player_duration > 0 and not self._seek_dragging:
            self.seek_slider.setValue(int(position_ms * 1000 / self._player_duration))
        self._set_time_label(position_ms, self._player_duration)

    def _on_player_duration(self, duration_ms: int) -> None:
        self._player_duration = int(duration_ms)
        self.seek_slider.setEnabled(self._player_duration > 0)
        self._set_time_label(
            self._player.position() if self._player is not None else 0,
            self._player_duration,
        )

    def _on_seek_moved(self, value: int) -> None:
        if self._player is not None and self._player_duration > 0:
            self._player.setPosition(int(value / 1000.0 * self._player_duration))
        if self._player_duration > 0:
            self._set_time_label(
                int(value / 1000.0 * self._player_duration), self._player_duration
            )

    def _on_seek_released(self) -> None:
        self._seek_dragging = False

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def _remember_history(self, result: SynthesisJobResult) -> None:
        params = self._last_params or {}
        next_id = max((rec.record_id for rec in self._history_records), default=0) + 1
        record = HistoryRecord(
            record_id=next_id,
            path=result.wav_path,
            text=self._last_text or "",
            time=datetime.now().strftime("%Y-%m-%d %H:%M"),
            duration=result.duration_sec,
            srt_path=result.srt_path,
            speed=float(params.get("speed", 1.0)),
        )
        self._history_records.insert(0, record)
        del self._history_records[HistoryRepository.MAX_RECORDS:]
        self._persist_history()
        self._render_history()

    @staticmethod
    def _history_group_label(timestamp: str) -> str:
        try:
            day = datetime.strptime(timestamp, "%Y-%m-%d %H:%M").date()
        except (TypeError, ValueError):
            return (timestamp or "")[:10]
        today = datetime.now().date()
        if day == today:
            return "今天"
        if day == today - timedelta(days=1):
            return "昨天"
        return day.strftime("%Y-%m-%d")

    def _render_history(self) -> None:
        while self._history_cards_layout.count():
            item = self._history_cards_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._history_widgets = []
        previous_group = ""
        for record in self._history_records:
            group = self._history_group_label(record.time)
            if group != previous_group:
                group_label = QLabel(group)
                group_label.setObjectName("HistoryGroup")
                group_label.setContentsMargins(
                    12, SP_MD if previous_group else 2, 0, 2
                )
                self._history_cards_layout.addWidget(group_label)
                previous_group = group
            card = HistoryItemWidget(
                record,
                play_cb=self._play_history_record,
                save_cb=self._save_history_copy,
                delete_cb=self._delete_history_record,
            )
            self._history_widgets.append(card)
            self._history_cards_layout.addWidget(card)
        self.history_count_label.setText(
            f"{len(self._history_records)}/{HistoryRepository.MAX_RECORDS}"
        )
        has_records = bool(self._history_records)
        if getattr(self, "_history_scroll", None) is not None:
            self._history_scroll.setVisible(has_records)
        self.history_empty_hint.setVisible(not has_records)
        self._mark_playing(self._current_audio_path)

    def _persist_history(self) -> None:
        try:
            HistoryRepository.save(
                self._history_path,
                self._history_records,
                base_output_dir=self._history_base_dir(),
            )
        except OSError as exc:
            self._log("warn", f"历史记录保存失败：{exc}")

    def _find_history_card(self, record: HistoryRecord) -> HistoryItemWidget | None:
        for card in self._history_widgets:
            if card.record.record_id == record.record_id:
                return card
        return None

    def _mark_record_missing(self, record: HistoryRecord) -> None:
        card = self._find_history_card(record)
        if card is not None:
            card.set_missing(True)
        else:
            record.missing = True
        self._set_status("音频文件已被移动或删除", "warn")
        self._log("warn", f"音频文件不存在，无法操作：{record.path}")

    def _play_history_record(self, record: HistoryRecord) -> None:
        if not os.path.exists(record.path):
            self._mark_record_missing(record)
            return
        self._load_audio(record.path)

    def _save_history_copy(self, record: HistoryRecord) -> None:
        if not os.path.exists(record.path):
            self._mark_record_missing(record)
            return
        suggested_dir = self._current_output_dir()
        if not os.path.isdir(suggested_dir):
            suggested_dir = resolve_output_dir("", self.cfg.output_dir)
        suggested = os.path.join(suggested_dir, os.path.basename(record.path))
        target, _ = QFileDialog.getSaveFileName(self, "另存音频", suggested, "WAV 音频 (*.wav)")
        if not target:
            return
        target = os.path.normpath(target)
        src_wav = record.path
        src_srt = record.srt_path or ""
        self._log("info", f"正在另存至：{target}…")

        def _atomic_copy_file(src: str, dst: str) -> None:
            part = f"{dst}.{os.getpid()}.{threading.get_ident()}.part"
            try:
                shutil.copyfile(src, part)
                replace_with_retry(part, dst)
            except BaseException:
                remove_quiet(part)
                raise

        def _copy_history() -> None:
            try:
                _atomic_copy_file(src_wav, target)
                if src_srt and os.path.exists(src_srt):
                    _atomic_copy_file(src_srt, os.path.splitext(target)[0] + ".srt")
                _safe_emit(self._history_save_done, True, target, "")
            except OSError as exc:
                _safe_emit(self._history_save_done, False, target, str(exc))

        threading.Thread(target=_copy_history, daemon=True, name="history-export").start()

    def _on_history_save_done(self, ok: bool, target: str, err: str) -> None:
        if self._closing:
            return
        if ok:
            self._log("ok", f"已另存：{target}")
        else:
            self._log("error", f"另存失败：{err}")

    def _delete_history_record(self, record: HistoryRecord) -> None:
        if (
            self._current_audio_path
            and record.path
            and os.path.normcase(os.path.abspath(self._current_audio_path))
            == os.path.normcase(os.path.abspath(record.path))
        ):
            self._reset_player_idle()
        self._history_records = [
            rec for rec in self._history_records if rec.record_id != record.record_id
        ]
        self._persist_history()
        self._render_history()
        self._log("info", f"已从历史移除：{os.path.basename(record.path)}")

    # ------------------------------------------------------------------
    # Config / misc
    # ------------------------------------------------------------------

    def _set_status(self, text: str, kind: str) -> None:
        self._status_kind = kind
        self.status_label.setText(text)
        self.status_label.setStyleSheet(status_style(kind))

    def _log(self, level: str, message: str) -> None:
        entry = self.log_buffer.append(level, message)
        # Incremental append.
        self.log_buffer.render_line(self.log_view, entry)
        entry_text = entry[2] if isinstance(entry, tuple) and len(entry) >= 3 else str(entry)
        preview = getattr(self, "log_preview_label", None)
        if preview is not None:
            compact = " ".join((message or "").splitlines()).strip()
            preview.setFullText(f"· 最新：{compact or entry_text}")
        if level != "engine" and entry_text:
            append_engine_log(f"[{level.upper()}] {entry_text}", base_dir=self._base_dir)

    def _schedule_save(self) -> None:
        # Signals fired during _build_ui (e.g. checkbox setChecked) arrive
        # before the debounce timer exists; early saves are pointless anyway.
        timer = getattr(self, "_save_timer", None)
        if timer is not None:
            timer.start()

    def _save_config(self) -> None:
        cfg = self.cfg
        candidate_url = (self.api_input.text() or "").strip()
        valid_url = validate_http_api_url(candidate_url)
        if valid_url is not None:
            cfg.api_url = valid_url
        cfg.speed = self._effective_speed()
        cfg.output_dir = self.path_input.text().strip()
        cfg.name = self.name_input.text().strip()
        cfg.export_srt = self.chk_export_srt.isChecked()
        cfg.log_visible = self.btn_toggle_log.isChecked()
        try:
            ConfigRepository.save(self._config_path, cfg)
        except OSError as exc:
            self._log("warn", f"配置保存失败：{exc}")

    def _on_speed_changed(self, value: int) -> None:
        snapped = int(round(value / 5.0)) * 5
        if snapped != value:
            # Detent snap: free pixel dragging always lands on a clean 0.05x
            # step; the nested setValue re-enters this slot with it.
            self.speed_slider.setValue(snapped)
            return
        self._initial_slider_val = -1
        self._exact_speed = value / 100.0
        self.speed_value_label.setText(f"{self._exact_speed:.2f}x")
        if getattr(self, "script_editor", None) is not None:
            self.script_editor.set_speed(self._exact_speed)
        self._schedule_save()

    def _on_api_url_edited(self) -> None:
        candidate = self._current_api_url()
        normalized = validate_http_api_url(candidate)
        if normalized is None:
            self._log("warn", f"引擎地址无效，已恢复：{candidate}")
            with QSignalBlocker(self.api_input):
                self.api_input.setText(self.cfg.api_url)
            self._refresh_engine_endpoint()
            return
        changed = normalized != self.cfg.api_url
        self.cfg.api_url = normalized
        if self.api_input.text().strip() != normalized:
            with QSignalBlocker(self.api_input):
                self.api_input.setText(normalized)
        self._refresh_engine_endpoint()
        self._schedule_save()
        if changed:
            self.gsv_client.api_url = normalized
            self.gsv_client.invalidate_active_models()
            self.supervisor.mark_online(False)
            self._probe_generation += 1
            self._start_generation += 1
            self._probe_in_flight = False
            self._starting_in_flight = False
            self._force_restarting = False
            self._engine_starting = False
            self._engine_online = False
            self._starting_url = ""
            self._startup_probe(auto_start=False)

    def _destroy_tray(self) -> None:
        if self._tray is not None:
            self._tray.hide()
            self._tray.deleteLater()
            self._tray = None
        if self._tray_menu is not None:
            self._tray_menu.deleteLater()
            self._tray_menu = None

    def _on_tray_toggled(self, checked: bool) -> None:
        checked = bool(checked)
        self.cfg.minimize_to_tray = checked
        if checked:
            if self._tray is None:
                self._setup_tray()
                if self._tray is None:
                    with QSignalBlocker(self.chk_tray):
                        self.chk_tray.setChecked(False)
                    self.cfg.minimize_to_tray = False
                    self._set_status("当前系统不支持系统托盘图标", "warn")
                    self._log("warn", "系统托盘不可用，无法开启最小化到托盘。")
        else:
            self._destroy_tray()
        self._schedule_save()

    def _browse_output_dir(self) -> None:
        target, _ = self._validate_output_dir_text(self.path_input.text())
        start_dir = self._nearest_existing_dir(target or resolve_output_dir("", ""))
        chosen = QFileDialog.getExistingDirectory(self, "选择输出目录", start_dir)
        if chosen:
            self.path_input.setText(os.path.normpath(chosen))
            self._schedule_save()
            self._update_output_summary()

    def _open_output_folder(self) -> None:
        try:
            out_dir = self._resolve_or_prepare_output_dir(create=True)
        except OSError as exc:
            self._set_status("输出目录无效或不可写", "error")
            self._log("error", f"无法打开输出目录：{exc}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.normpath(out_dir)))

    # ------------------------------------------------------------------
    # Tray
    # ------------------------------------------------------------------

    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._destroy_tray()
        tray = QSystemTrayIcon(getattr(self, "_app_icon", None) or _make_app_icon(), self)
        tray.setToolTip(f"MamboTTS 工作室 {APP_VERSION}")
        menu = QMenu(self)
        act_show = QAction("显示主界面", menu)
        act_show.triggered.connect(self._show_from_tray)
        act_quit = QAction("退出（停止引擎）", menu)
        act_quit.triggered.connect(self._quit_from_tray)
        menu.addAction(act_show)
        menu.addAction(act_quit)
        tray.setContextMenu(menu)
        tray.activated.connect(
            lambda reason: self._show_from_tray()
            if reason == QSystemTrayIcon.DoubleClick
            else None
        )
        tray.show()
        self._tray = tray
        self._tray_menu = menu

    def _show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit_from_tray(self) -> None:
        self._real_quit = True
        self.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    # ------------------------------------------------------------------
    # Window events
    # ------------------------------------------------------------------

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        if hasattr(self, "action_row"):
            self._relayout_action_row()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._fade_started:
            return
        self._fade_started = True
        self.setWindowOpacity(0.0)
        anim = QPropertyAnimation(self, b"windowOpacity", self)
        anim.setDuration(200)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        self._fade_anim = anim
        anim.start()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.key() == Qt.Key_Escape:
            worker = self._synth_worker
            if worker is not None and worker.is_alive():
                self.cancel_synthesis()
                return
        if event.key() == Qt.Key_Space and self.stack.currentWidget() is self.studio_page:
            focus = QApplication.focusWidget()
            if not isinstance(
                focus,
                (QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QAbstractButton),
            ):
                self._toggle_play()
                return
        super().keyPressEvent(event)

    def eventFilter(self, obj, event) -> bool:
        if obj is getattr(getattr(self, "script_editor", None), "text_input", None):
            if event.type() == QEvent.FocusIn:
                set_shadow_level(getattr(self, "script_paper", None), "focus")
            elif event.type() == QEvent.FocusOut:
                set_shadow_level(getattr(self, "script_paper", None), 1)
        if obj is getattr(self, "speed_slider", None) and event.type() == QEvent.MouseButtonDblClick:
            self.speed_slider.setValue(100)
            return True
        return super().eventFilter(obj, event)

    def closeEvent(self, event) -> None:
        if (
            self.cfg.minimize_to_tray
            and self._tray is not None
            and not self._real_quit
        ):
            event.ignore()
            self.hide()
            self._tray.showMessage(
                "MamboTTS 工作室", "已最小化到系统托盘", QSystemTrayIcon.Information, 2000
            )
            return
        # Cooperative-cancel an in-flight synthesis or setup installer so
        # background threads and 7zr/downloads clean up cleanly on process exit.
        self._closing = True
        self._play_copy_gen += 1
        worker = getattr(self, "_synth_worker", None)
        if worker is not None and worker.is_alive():
            worker.cancel_event.set()
        if self.setup_view is not None:
            self.setup_view.cancel_and_cleanup()
        else:
            terminate_active_install_processes()
        self._destroy_tray()
        self._synth_elapsed_timer.stop()
        self._cancel_wait_timer.stop()
        self._heartbeat_timer.stop()
        self._save_timer.stop()
        self._release_player_source()
        _cleanup_stale_temp_play_files()
        self._save_config()
        self.supervisor.stop()
        event.accept()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _install_chinese_ui_translations(app) -> None:
    """Load Qt's bundled zh_CN catalog so builtin context menus are Chinese."""
    from PySide6.QtCore import QLocale, QTranslator

    try:
        import PySide6

        catalog = os.path.join(
            os.path.dirname(os.path.abspath(PySide6.__file__)),
            "translations",
            "qtbase_zh_CN.qm",
        )
        if not os.path.exists(catalog):
            return
        translator = QTranslator(app)
        if translator.load(catalog):
            app.installTranslator(translator)
            # Keep a Python-side reference so the binding never GCs it.
            app._mambotts_translator = translator  # type: ignore[attr-defined]
        QLocale.setDefault(QLocale(QLocale.Chinese, QLocale.SimplifiedChineseScript))
    except Exception:  # noqa: BLE001 - translations must never block startup
        pass


def _install_crash_hook() -> None:
    """pythonw has no stderr: any startup crash would look like "nothing
    happened". Persist the traceback to crash.log and surface a dialog."""

    def _hook(etype, value, tb):
        import traceback

        if etype is KeyboardInterrupt or (
            isinstance(etype, type) and issubclass(etype, KeyboardInterrupt)
        ):
            return
        try:
            log_path = os.path.join(PROJECT_ROOT, "crash.log")
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(f"--- {datetime.now().isoformat()} ---\n")
                traceback.print_exception(etype, value, tb, file=fh)
            detail = "".join(traceback.format_exception_only(etype, value))[-800:]
        except Exception:  # noqa: BLE001
            log_path, detail = PROJECT_ROOT, str(value)
        try:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.critical(
                None,
                "MamboTTS 遇到错误",
                f"程序遇到未处理的错误并已退出。\n详情已写入：{log_path}\n\n{detail}",
            )
        except Exception:  # noqa: BLE001 - Qt may not be alive
            try:
                import ctypes

                ctypes.windll.user32.MessageBoxW(
                    0,
                    f"程序遇到错误并已退出，详情见 crash.log\n{detail}",
                    "MamboTTS",
                    0x10,
                )
            except Exception:  # noqa: BLE001
                pass

    import sys
    import threading

    sys.excepthook = _hook
    threading.excepthook = lambda args: _hook(args.exc_type, args.exc_value, args.exc_traceback)


def _acquire_single_instance_mutex(base_dir: str = PROJECT_ROOT) -> int | None:
    """Acquire a per-install Windows named mutex; return handle or None if held."""
    import sys

    if sys.platform != "win32":
        return 1
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.GetLastError.restype = ctypes.c_uint32

        norm = os.path.normcase(os.path.abspath(base_dir or PROJECT_ROOT))
        digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
        name = f"Local\\MamboTTS_Studio_{digest}"
        handle = k32.CreateMutexW(None, False, name)
        if not handle:
            return 1
        ERROR_ALREADY_EXISTS = 183
        if k32.GetLastError() == ERROR_ALREADY_EXISTS:
            k32.CloseHandle(handle)
            return None
        return int(handle)
    except Exception:
        return 1


def _release_single_instance_mutex(handle: int | None) -> None:
    import sys

    if sys.platform != "win32" or not handle or handle == 1:
        return
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.CloseHandle(ctypes.c_void_p(handle))
    except Exception:
        pass


def _set_windows_app_user_model_id() -> None:
    """Give the studio its own Windows taskbar identity rather than pythonw.exe's."""
    import sys

    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MamboTTS.Studio.v2")
    except Exception:
        pass


def run_gui(argv: list[str] | None = None) -> int:
    """Create the QApplication, show the studio window and run the loop."""
    import sys

    _set_windows_app_user_model_id()
    args = list(sys.argv[1:] if argv is None else argv)
    app = QApplication.instance()
    if app is None:
        app = QApplication(args)
    app.setApplicationName("MamboTTS")
    app.setOrganizationName("MamboTTS")
    app.setWindowIcon(_make_app_icon())
    mutex_handle = _acquire_single_instance_mutex(PROJECT_ROOT)
    if mutex_handle is None:
        QMessageBox.information(
            None,
            "MamboTTS",
            "MamboTTS 工作室已在运行中，请查看任务栏或系统托盘图标。",
        )
        return 0
    _install_crash_hook()
    _install_chinese_ui_translations(app)
    try:
        window = MamboStudioWindow()
        window.show()
        return app.exec()
    finally:
        _release_single_instance_mutex(mutex_handle)
