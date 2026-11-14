"""First-run GPT-SoVITS v5 environment setup view."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import os
import shutil
import threading
import time

from PySide6.QtCore import QEvent, QThread, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from mambotts import APP_VERSION, PROJECT_ROOT
from mambotts.domain.models import GPT_MODEL, REF_WAV, SOVITS_MODEL
from mambotts.domain.voice_repo import check_models_ready
from mambotts.infra.installer import (
    DISK_SPACE_FACTOR,
    GPU_PACKAGES,
    STAGES,
    classify_local_cache,
    describe_sources,
    detect_gpu_type,
    estimate_disk_requirement,
    install_engine,
    probe_source_metrics,
    terminate_active_install_processes,
)

from .icons import icon
from .theme import (
    CONTROL_H,
    PRIMARY_H,
    SETUP_COL_W,
    SHADOW_MARGIN_2,
    SP_LG,
    SP_MD,
    SP_SM,
    SP_XL,
    SP_XS,
    THEME,
    dot_style,
)
from .widgets import ModuleCard, ThemedDialog

logger = logging.getLogger("mambotts.ui.setup")

_GiB = 1024 ** 3


class SetupWorker(QThread):
    """Background installer worker running ``install_engine``."""

    progress = Signal(float, str)
    log = Signal(str)
    finished = Signal(bool, str)

    def __init__(
        self,
        gpu_key: str,
        base_dir: str,
        *,
        preferred_url: str | None = None,
        pre_ranked_urls: list[str] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._gpu_key = gpu_key
        self._base_dir = base_dir
        self._preferred_url = preferred_url
        self._pre_ranked_urls = list(pre_ranked_urls) if pre_ranked_urls else None
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()
        terminate_active_install_processes()

    def run(self) -> None:
        stage_holder = [STAGES[0][0] if STAGES else "准备安装"]
        pct_holder = [0.0]

        def _on_stage(stage: str) -> None:
            stage_holder[0] = str(stage or stage_holder[0])
            self.progress.emit(pct_holder[0], stage_holder[0])

        def _on_progress(pct: float) -> None:
            pct_holder[0] = float(pct)
            self.progress.emit(pct_holder[0], stage_holder[0])

        try:
            ok, msg = install_engine(
                base_dir=self._base_dir,
                gpu_type=self._gpu_key,
                progress_cb=_on_progress,
                state_cb=_on_stage,
                log_cb=lambda line: self.log.emit(str(line)),
                cancel_cb=self._cancel_event.is_set,
                preferred_url=self._preferred_url,
                pre_ranked_urls=self._pre_ranked_urls,
            )
            self.finished.emit(bool(ok), str(msg))
        except Exception as exc:  # noqa: BLE001 - report installer failure
            logger.exception("setup worker failed")
            self.finished.emit(False, f"安装异常：{exc}")


class SpeedTestWorker(QThread):
    """Bounded concurrent speed probe across all visible download sources."""

    source_result = Signal(int, str, object, object)  # gen, url, mb_s, latency_ms
    finished_gen = Signal(int)

    def __init__(
        self,
        gen: int,
        items: list[tuple[str, int]],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._gen = gen
        self._items = list(items)
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        try:
            if not self._items or self._cancel_event.is_set():
                return
            pool = ThreadPoolExecutor(max_workers=min(4, max(1, len(self._items))))
            cancelled = False
            try:
                future_map = {
                    pool.submit(
                        probe_source_metrics,
                        url,
                        expected_bytes=exp_bytes,
                        cancel_cb=self._cancel_event.is_set,
                    ): url
                    for url, exp_bytes in self._items
                }
                for fut in as_completed(future_map):
                    if self._cancel_event.is_set():
                        cancelled = True
                        for pending in future_map:
                            pending.cancel()
                        break
                    url = future_map[fut]
                    try:
                        speed_bps, latency = fut.result()
                    except Exception:
                        speed_bps, latency = None, None
                    mb_s = (
                        float(speed_bps) / (1024 * 1024)
                        if isinstance(speed_bps, (int, float)) and speed_bps > 0
                        else None
                    )
                    self.source_result.emit(self._gen, url, mb_s, latency)
            finally:
                cancelled = cancelled or self._cancel_event.is_set()
                pool.shutdown(wait=not cancelled, cancel_futures=True)
        finally:
            self.finished_gen.emit(self._gen)


class _GuideDialog(ThemedDialog):
    """Setup guide modal covering models/ and offline .7z resume."""

    def __init__(self, base_dir: str, parent: QWidget | None = None):
        super().__init__(
            parent,
            title="环境与模型准备说明",
            subtitle="手动离线安装与文件对照",
            dot_color=THEME["accent"],
            width=500,
        )
        self._base_dir = base_dir
        body = self.body

        scroll = QScrollArea()
        scroll.setObjectName("GuideScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setMaximumHeight(360)

        inner = QWidget()
        inner.setObjectName("TransparentSlot")
        inner_col = QVBoxLayout(inner)
        inner_col.setContentsMargins(0, 0, 4, 0)
        inner_col.setSpacing(SP_MD)

        intro_well = QFrame()
        intro_well.setObjectName("WellBox")
        iw_layout = QVBoxLayout(intro_well)
        iw_layout.setContentsMargins(SP_MD, SP_MD, SP_MD, SP_MD)
        iw_layout.setSpacing(SP_SM)
        for line in (
            "1. 整合包（GPT-SoVITS v5 · 20261005）会下载至项目根目录 engine_temp/，自动校验 SHA-256 后解压至 GPT-SoVITS/。",
            "2. 如浏览器或迅雷下载更快，可点击线路旁「复制链接」，将 .7z 放入 engine_temp/ 再点「开始下载并安装」即可自动跳过下载。",
        ):
            lbl = QLabel(line)
            lbl.setObjectName("SetupBullet")
            lbl.setWordWrap(True)
            iw_layout.addWidget(lbl)
        inner_col.addWidget(intro_well)

        files_well = QFrame()
        files_well.setObjectName("WellBox")
        fw_layout = QVBoxLayout(files_well)
        fw_layout.setContentsMargins(SP_MD, SP_MD, SP_MD, SP_MD)
        fw_layout.setSpacing(SP_SM)
        fw_title = QLabel("3. 需放入 models/ 文件夹的角色文件")
        fw_title.setObjectName("SectionTitle")
        fw_layout.addWidget(fw_title)

        file_specs = (
            (GPT_MODEL, "GPT 语义权重", "必需", "WarnPill"),
            (SOVITS_MODEL, "SoVITS 声学权重", "必需", "WarnPill"),
            (REF_WAV, "默认参考音频", "必需", "WarnPill"),
            ("refer.txt", "默认参考台词（缺省将回落内置台词，音色可能漂移）", "推荐", "BadgeNeutral"),
        )
        for fname, desc, tag, role in file_specs:
            item_card = QFrame()
            item_card.setObjectName("SubCard")
            row = QHBoxLayout(item_card)
            row.setContentsMargins(SP_MD, SP_SM, SP_MD, SP_SM)
            row.setSpacing(SP_SM)
            name_lbl = QLabel(fname)
            name_lbl.setObjectName("RowTitle")
            row.addWidget(name_lbl)
            desc_lbl = QLabel(desc)
            desc_lbl.setObjectName("WeakMeta")
            desc_lbl.setWordWrap(True)
            row.addWidget(desc_lbl, 1)
            tag_lbl = QLabel(tag)
            tag_lbl.setObjectName(role)
            tag_lbl.setFixedHeight(22)
            tag_lbl.setAlignment(Qt.AlignCenter)
            row.addWidget(tag_lbl)
            fw_layout.addWidget(item_card)
        inner_col.addWidget(files_well)
        scroll.setWidget(inner)
        body.addWidget(scroll)

        self._err_label = QLabel()
        self._err_label.setObjectName("SetupBullet")
        self._err_label.setWordWrap(True)
        self._err_label.setVisible(False)
        body.addWidget(self._err_label)

        btn_open_cache = QPushButton("打开 engine_temp/")
        btn_open_cache.setObjectName("OutlineBtn")
        btn_open_cache.setFixedHeight(CONTROL_H)
        btn_open_cache.setCursor(Qt.PointingHandCursor)
        btn_open_cache.clicked.connect(
            lambda: self._try_open_folder(os.path.join(self._base_dir, "engine_temp"))
        )
        btn_open_models = QPushButton("打开 models/")
        btn_open_models.setObjectName("OutlineBtn")
        btn_open_models.setFixedHeight(CONTROL_H)
        btn_open_models.setCursor(Qt.PointingHandCursor)
        btn_open_models.clicked.connect(
            lambda: self._try_open_folder(os.path.join(self._base_dir, "models"))
        )
        btn_close = QPushButton("知道了")
        btn_close.setObjectName("PrimaryBtn")
        btn_close.setFixedHeight(PRIMARY_H)
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.setAutoDefault(True)
        btn_close.setDefault(True)
        btn_close.clicked.connect(self.accept)

        # Insert left-aligned utility folder buttons ahead of footer's stretch item.
        self.footer.insertWidget(0, btn_open_models)
        self.footer.insertWidget(0, btn_open_cache)
        self.footer.addWidget(btn_close)
        self.first_focus_widget = btn_close
        btn_close.setFocus()

    def _try_open_folder(self, path: str) -> None:
        ok, err = self._open_folder(path)
        if not ok:
            self._err_label.setText(err)
            self._err_label.setStyleSheet(f"color: {THEME['warning']}; font-size: 12px;")
            self._err_label.setVisible(True)

    @staticmethod
    def _open_folder(path: str) -> tuple[bool, str]:
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            return False, f"无法打开或创建目录 {path}：{exc}"
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        return True, ""


class _SourceRowWidgets:
    def __init__(
        self,
        source: dict[str, str],
        radio: QRadioButton,
        metric_label: QLabel,
        copy_btn: QPushButton,
        row_frame: QFrame,
        active_label: QLabel,
    ):
        self.source = source
        self.radio = radio
        self.metric_label = metric_label
        self.copy_btn = copy_btn
        self.row_frame = row_frame
        self.active_label = active_label
        self.speed_mb_s: float | None = None
        self.latency_ms: int | None = None


class _ClickableGroupRow(QFrame):
    """Single source card row that activates its radio button when clicked."""

    def __init__(
        self,
        radio: QRadioButton,
        on_pick,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("SourceRow")
        self.setMinimumHeight(42)
        self._radio = radio
        self._on_pick = on_pick
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.LeftButton and self._radio.isEnabled():
            self._radio.setChecked(True)
            self._on_pick()
        super().mousePressEvent(event)


class _GpuOptionCard(QFrame):
    """Top-icon Warm Paper selection card synced to a backing QRadioButton."""

    def __init__(
        self,
        key: str,
        title: str,
        desc: str,
        radio: QRadioButton,
        detected: bool,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.key = key
        self.radio = radio
        self._icon_name = "gpu-chip" if key == "nvidia50" else "gpu-card"
        self._detected = detected
        self.setCursor(Qt.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(SP_SM)

        top = QHBoxLayout()
        top.setSpacing(SP_SM)
        self.icon_label = QLabel()
        top.addWidget(self.icon_label)
        top.addStretch(1)
        self.seal_tag = QLabel("本机推荐" if detected else "当前选择")
        self.seal_tag.setObjectName("SealTag")
        self.seal_tag.setAlignment(Qt.AlignCenter)
        self.seal_tag.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        top.addWidget(self.seal_tag)
        layout.addLayout(top)

        lbl_title = QLabel(title)
        lbl_title.setObjectName("CardTitle")
        layout.addWidget(lbl_title)

        lbl_desc = QLabel(desc)
        lbl_desc.setObjectName("MutedBody")
        lbl_desc.setWordWrap(True)
        layout.addWidget(lbl_desc)
        layout.addStretch(1)

        # Keep compatibility QRadioButton in widget tree without visual clutter.
        radio.setVisible(False)
        layout.addWidget(radio)
        self.sync_visual(radio.isChecked())

    def sync_visual(self, selected: bool) -> None:
        self.setObjectName("SetupCardSelected" if selected else "SetupCardIdle")
        icon_color = THEME["text"] if selected else THEME["muted"]
        self.icon_label.setPixmap(icon(self._icon_name, icon_color, 22, 1.5).pixmap(22, 22))
        self.seal_tag.setVisible(self._detected or selected)
        self.seal_tag.setText("本机推荐" if self._detected else "当前选择")
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.LeftButton and self.radio.isEnabled():
            self.radio.setChecked(True)
        super().mousePressEvent(event)


class EmbeddedSetupView(QWidget):
    """Scrollable onboarding sheet for engine v5 download, speed test & guide."""

    setup_completed = Signal()

    def __init__(
        self,
        base_dir: str = PROJECT_ROOT,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._base_dir = base_dir
        self._gpu_detected: str | None = detect_gpu_type()
        self._detected_gpu: str | None = self._gpu_detected
        self._selected_gpu: str = (
            self._gpu_detected
            if self._gpu_detected in GPU_PACKAGES
            else "general"
        )
        self._worker: SetupWorker | None = None
        self._speed_worker: SpeedTestWorker | None = None
        self._retired_speed_workers: list[SpeedTestWorker] = []
        self._speedtest_gen: int = 0
        self._manual_source_selected: bool = False
        self._cancel_requested: bool = False
        self._ranked_urls: list[str] = []
        self._last_install_message: str = ""
        self._dl_start_mono: float = 0.0
        self._dl_start_pct: float = 0.0
        self._source_rows: list[_SourceRowWidgets] = []

        self._radios: dict[str, QRadioButton] = {}
        self._gpu_cards: dict[str, _GpuOptionCard] = {}
        self._model_badges: dict[str, tuple[QLabel, QLabel]] = {}
        self._gpu_group = QButtonGroup(self)
        self._source_group = QButtonGroup(self)

        self._build_ui()
        self._refresh_local_state()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setObjectName("SetupScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        scroll_content = QWidget()
        scroll_content.setObjectName("SetupScrollContent")
        outer_h = QHBoxLayout(scroll_content)
        outer_h.setContentsMargins(SP_LG, 0, SP_LG, 0)
        outer_h.setSpacing(0)
        outer_h.addStretch(1)

        self._col_widget = QWidget()
        self._col_widget.setObjectName("SetupScrollContent")
        self._col_widget.setMinimumWidth(520 + SHADOW_MARGIN_2 * 2)
        self._col_widget.setMaximumWidth(SETUP_COL_W + SHADOW_MARGIN_2 * 2)
        self._col_widget.setSizePolicy(
            QSizePolicy.Preferred, QSizePolicy.Minimum,
        )
        col = QVBoxLayout(self._col_widget)
        col.setContentsMargins(SHADOW_MARGIN_2, SP_XL, SHADOW_MARGIN_2, SP_XL)
        col.setSpacing(0)

        # Calm editorial header with subtle vermilion seal mark
        brand_row = QHBoxLayout()
        brand_row.setSpacing(SP_SM)
        chop = QFrame()
        chop.setObjectName("BrandDot")
        brand_row.addWidget(chop, 0, Qt.AlignVCenter)
        brand_lbl = QLabel("MamboTTS · 首次环境配置")
        brand_lbl.setObjectName("MutedBody")
        brand_row.addWidget(brand_lbl, 0, Qt.AlignVCenter)
        ver_lbl = QLabel(APP_VERSION)
        ver_lbl.setObjectName("BadgeNeutral")
        ver_lbl.setFixedHeight(22)
        ver_lbl.setAlignment(Qt.AlignCenter)
        brand_row.addWidget(ver_lbl, 0, Qt.AlignVCenter)
        brand_row.addStretch(1)
        col.addLayout(brand_row)
        col.addSpacing(SP_SM)

        title = QLabel("准备本地语音合成引擎")
        title.setObjectName("SetupTitle")
        col.addWidget(title)
        col.addSpacing(SP_XS)

        sub = QLabel(
            "下载官方 GPT-SoVITS v5（20261005）推理整合包至本地并校验曼波音色文件；"
            "若 engine_temp/ 已有断点包将自动校验续传。"
        )
        sub.setObjectName("SetupSubtitle")
        sub.setWordWrap(True)
        col.addWidget(sub)
        col.addSpacing(SP_XL)

        # Section 1: Side-by-side GPU selection cards
        col.addWidget(self._build_gpu_section())
        col.addSpacing(SP_LG)

        # Section 2: Download sources inside ModuleCard + WellBox
        col.addWidget(self._build_sources_section())
        col.addSpacing(SP_LG)

        # Section 3: Character voice models inside ModuleCard + WellBox
        col.addWidget(self._build_models_section())
        col.addSpacing(SP_MD)

        # Collapsible installation log (ModuleCard hidden until expanded)
        log_header = QHBoxLayout()
        log_header.setSpacing(SP_SM)
        self.btn_details = QPushButton("展开安装日志")
        self.btn_details.setObjectName("OutlineBtn")
        self.btn_details.setFixedHeight(28)
        self.btn_details.setCursor(Qt.PointingHandCursor)
        self.btn_details.setCheckable(True)
        self.btn_details.toggled.connect(self._on_toggle_details)
        log_header.addWidget(self.btn_details)
        log_header.addStretch(1)
        col.addLayout(log_header)

        log_panel = ModuleCard(
            "安装与校验日志",
            dot_color=THEME["ok"],
            body_margins=(SP_MD, SP_MD, SP_MD, SP_MD),
            body_spacing=0,
        )
        self._log_pct_pill = QLabel("0%")
        self._log_pct_pill.setObjectName("BadgeVerm")
        self._log_pct_pill.setFixedHeight(22)
        self._log_pct_pill.setAlignment(Qt.AlignCenter)
        self._log_pct_pill.setVisible(False)
        log_panel.add_header_widget(self._log_pct_pill)

        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("LogView")
        self.log_view.setReadOnly(True)
        self.log_view.setFixedHeight(200)
        self.log_view.setVisible(False)
        log_panel.body_layout.addWidget(self.log_view)
        self._log_panel = log_panel
        self._log_panel.setVisible(False)
        col.addSpacing(SP_XS)
        col.addWidget(log_panel)

        outer_h.addWidget(self._col_widget)
        outer_h.addStretch(1)
        self._scroll.setWidget(scroll_content)
        root.addWidget(self._scroll, 1)

        # Pinned bottom footer (stays above the fold at 1000x680)
        root.addWidget(self._build_footer())

    def _build_gpu_section(self) -> ModuleCard:
        card = ModuleCard(
            "1 · 选择显卡整合包",
            "支持断点续传",
            body_margins=(SP_LG, SP_MD, SP_LG, SP_LG),
            body_spacing=SP_SM,
        )

        self.disk_label = QLabel()
        self.disk_label.setObjectName("BadgeNeutral")
        self.disk_label.setFixedHeight(24)
        self.disk_label.setAlignment(Qt.AlignCenter)
        card.add_header_widget(self.disk_label)

        cards_row = QHBoxLayout()
        cards_row.setContentsMargins(0, 0, 0, 0)
        cards_row.setSpacing(SP_MD)

        for key, pkg in GPU_PACKAGES.items():
            radio = QRadioButton(pkg["label"])
            radio.setChecked(key == self._selected_gpu)
            radio.toggled.connect(
                lambda checked, gpu_key=key: self._on_gpu_toggled(gpu_key, checked)
            )
            self._radios[key] = radio
            self._gpu_group.addButton(radio)

            size_bytes = int(pkg.get("size_bytes", 0) or 0)
            size_gib = float(size_bytes) / _GiB
            need_gib = (float(size_bytes) * DISK_SPACE_FACTOR) / _GiB
            arch = (
                "适配 Blackwell 架构 (CUDA 12.8)"
                if key == "nvidia50"
                else "适配 GTX 10 至 RTX 40 系常规显卡"
            )
            desc = f"{arch}\n安装包 {size_gib:.1f} GiB · 预留空间约 {need_gib:.0f} GiB"
            opt_card = _GpuOptionCard(
                key=key,
                title=pkg["label"],
                desc=desc,
                radio=radio,
                detected=(self._gpu_detected == key),
            )
            self._gpu_cards[key] = opt_card
            cards_row.addWidget(opt_card, 1)

        card.body_layout.addLayout(cards_row)

        if self._gpu_detected is None:
            no_gpu = QLabel(
                "未检测到 NVIDIA 显卡或驱动：已默认选用「其他显卡 (NVIDIA RTX 30/40 系等)」，如安装有 RTX 50 系列可手动切换。"
            )
            no_gpu.setObjectName("SetupBullet")
            no_gpu.setWordWrap(True)
            no_gpu.setStyleSheet(f"color: {THEME['warning']};")
            card.body_layout.addWidget(no_gpu)

        return card

    def _build_sources_section(self) -> ModuleCard:
        card = ModuleCard(
            "2 · 选择下载线路",
            "自动排除限速网盘，如直连缓慢可测速或复制链接手动下载",
            body_margins=(SP_LG, SP_MD, SP_LG, SP_LG),
            body_spacing=SP_SM,
        )

        self.btn_open_cache = QPushButton("打开缓存目录")
        self.btn_open_cache.setObjectName("OutlineBtn")
        self.btn_open_cache.setFixedHeight(28)
        self.btn_open_cache.setCursor(Qt.PointingHandCursor)
        self.btn_open_cache.clicked.connect(self._open_cache_dir)
        card.add_header_widget(self.btn_open_cache)

        self.btn_speedtest = QPushButton("一键测速")
        self.btn_speedtest.setObjectName("OutlineBtn")
        self.btn_speedtest.setFixedHeight(28)
        self.btn_speedtest.setCursor(Qt.PointingHandCursor)
        self.btn_speedtest.clicked.connect(self._start_speedtest)
        card.add_header_widget(self.btn_speedtest)

        well = QFrame()
        well.setObjectName("WellBox")
        well_layout = QVBoxLayout(well)
        well_layout.setContentsMargins(SP_SM, SP_SM, SP_SM, SP_SM)
        well_layout.setSpacing(0)

        self._sources_container = QWidget()
        self._sources_container.setObjectName("SetupOptionRow")
        self._sources_layout = QVBoxLayout(self._sources_container)
        self._sources_layout.setContentsMargins(0, 0, 0, 0)
        self._sources_layout.setSpacing(6)
        well_layout.addWidget(self._sources_container)
        card.body_layout.addWidget(well)

        self.sources_status_label = QLabel()
        self.sources_status_label.setObjectName("SetupBullet")
        self.sources_status_label.setWordWrap(True)
        self.sources_status_label.setVisible(False)
        card.body_layout.addWidget(self.sources_status_label)
        return card

    def _build_models_section(self) -> ModuleCard:
        card = ModuleCard(
            "3 · 角色音色模型",
            "放入 models/ 即可开始配音（可边安装边准备）",
            body_margins=(SP_LG, SP_MD, SP_LG, SP_LG),
            body_spacing=SP_SM,
        )

        self.btn_refresh_status = QPushButton("刷新检查")
        self.btn_refresh_status.setObjectName("OutlineBtn")
        self.btn_refresh_status.setFixedHeight(28)
        self.btn_refresh_status.setCursor(Qt.PointingHandCursor)
        self.btn_refresh_status.clicked.connect(self._refresh_disk_and_models)
        card.add_header_widget(self.btn_refresh_status)

        btn_models = QPushButton("打开 models/")
        btn_models.setObjectName("OutlineBtn")
        btn_models.setFixedHeight(28)
        btn_models.setCursor(Qt.PointingHandCursor)
        btn_models.clicked.connect(
            lambda: self._open_folder_safe(os.path.join(self._base_dir, "models"))
        )
        card.add_header_widget(btn_models)

        self.btn_readme = QPushButton("准备说明")
        self.btn_readme.setObjectName("OutlineBtn")
        self.btn_readme.setFixedHeight(28)
        self.btn_readme.setCursor(Qt.PointingHandCursor)
        self.btn_readme.clicked.connect(self._open_guide)
        card.add_header_widget(self.btn_readme)

        well = QFrame()
        well.setObjectName("WellBox")
        well_layout = QVBoxLayout(well)
        well_layout.setContentsMargins(SP_SM, SP_SM, SP_SM, SP_SM)
        well_layout.setSpacing(6)

        model_specs = (
            (GPT_MODEL, "GPT 语义权重文件"),
            (SOVITS_MODEL, "SoVITS 声学权重文件"),
            (REF_WAV, "曼波参考音色音频（放入 models/ 即可开始配音）"),
        )
        for fname, desc in model_specs:
            row = QFrame()
            row.setObjectName("SubCard")
            row.setMinimumHeight(48)
            rl = QHBoxLayout(row)
            rl.setContentsMargins(SP_MD, SP_SM, SP_MD, SP_SM)
            rl.setSpacing(SP_SM)
            tv = QVBoxLayout()
            tv.setSpacing(2)
            lbl_name = QLabel(fname)
            lbl_name.setObjectName("RowTitle")
            lbl_desc = QLabel(desc)
            lbl_desc.setObjectName("WeakMeta")
            tv.addWidget(lbl_name)
            tv.addWidget(lbl_desc)
            rl.addLayout(tv, 1)

            ok_lbl = QLabel("已就绪")
            ok_lbl.setObjectName("BadgeOk")
            ok_lbl.setFixedHeight(24)
            ok_lbl.setAlignment(Qt.AlignCenter)
            missing_lbl = QLabel("待放入")
            missing_lbl.setObjectName("WarnPill")
            missing_lbl.setFixedHeight(24)
            missing_lbl.setAlignment(Qt.AlignCenter)
            rl.addWidget(ok_lbl)
            rl.addWidget(missing_lbl)
            self._model_badges[fname] = (ok_lbl, missing_lbl)
            well_layout.addWidget(row)

        card.body_layout.addWidget(well)

        self.models_status_label = QLabel()
        self.models_status_label.setObjectName("DiskHint")
        self.models_status_label.setWordWrap(True)
        card.body_layout.addWidget(self.models_status_label)
        return card

    def _build_footer(self) -> QFrame:
        footer = QFrame()
        footer.setObjectName("SetupFooter")
        footer.setMinimumHeight(68)
        outer = QHBoxLayout(footer)
        outer.setContentsMargins(SP_LG, 14, SP_LG, 14)
        outer.setSpacing(0)
        outer.addStretch(1)

        inner = QWidget()
        inner.setObjectName("SetupOptionRow")
        inner.setMinimumWidth(520)
        inner.setMaximumWidth(SETUP_COL_W + SHADOW_MARGIN_2 * 2)
        inner_h = QHBoxLayout(inner)
        inner_h.setContentsMargins(SHADOW_MARGIN_2, 0, SHADOW_MARGIN_2, 0)
        inner_h.setSpacing(SP_LG)
        inner_h.setAlignment(Qt.AlignVCenter)

        left_col = QVBoxLayout()
        left_col.setContentsMargins(0, 0, 0, 0)
        left_col.setSpacing(0)
        left_col.setAlignment(Qt.AlignVCenter)

        self._idle_footer_wrap = QWidget()
        self._idle_footer_wrap.setObjectName("TransparentSlot")
        idle_row = QHBoxLayout(self._idle_footer_wrap)
        idle_row.setContentsMargins(0, 0, 0, 0)
        idle_row.setSpacing(SP_SM)
        idle_dot = QFrame()
        idle_dot.setFixedSize(8, 8)
        idle_dot.setStyleSheet(dot_style("ok"))
        idle_row.addWidget(idle_dot, 0, Qt.AlignVCenter)
        self._idle_footer_hint = QLabel(
            "准备就绪 · 支持断点续传与 SHA-256 校验"
        )
        self._idle_footer_hint.setObjectName("WeakMeta")
        self._idle_footer_hint.setWordWrap(True)
        idle_row.addWidget(self._idle_footer_hint, 1)
        left_col.addWidget(self._idle_footer_wrap)

        self.progress_block = QWidget()
        self.progress_block.setObjectName("SetupOptionRow")
        p_layout = QVBoxLayout(self.progress_block)
        p_layout.setContentsMargins(0, 0, 0, 0)
        p_layout.setSpacing(6)

        meta = QHBoxLayout()
        meta.setContentsMargins(0, 0, 0, 0)
        meta.setSpacing(SP_SM)
        self.stage_label = QLabel(f"等待开始（共 {len(STAGES)} 阶段）")
        self.stage_label.setObjectName("StageText")
        self.stage_label.setWordWrap(True)
        self.percent_label = QLabel("0%")
        self.percent_label.setObjectName("LogPercent")
        meta.addWidget(self.stage_label, 1)
        meta.addWidget(self.percent_label)
        p_layout.addLayout(meta)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(4)
        p_layout.addWidget(self.progress_bar)
        self.progress_block.setVisible(False)
        left_col.addWidget(self.progress_block)
        inner_h.addLayout(left_col, 1)

        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setObjectName("DangerBtn")
        self.btn_cancel.setFixedHeight(PRIMARY_H)
        self.btn_cancel.setCursor(Qt.PointingHandCursor)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setVisible(False)
        self.btn_cancel.clicked.connect(self._on_cancel)
        inner_h.addWidget(self.btn_cancel, 0, Qt.AlignVCenter)

        self.btn_start = QPushButton("开始下载并安装")
        self.btn_start.setObjectName("PrimaryBtn")
        self.btn_start.setFixedHeight(PRIMARY_H)
        self.btn_start.setCursor(Qt.PointingHandCursor)
        self.btn_start.clicked.connect(self._on_start)
        inner_h.addWidget(self.btn_start, 0, Qt.AlignVCenter)

        outer.addWidget(inner, 1)
        outer.addStretch(1)
        return footer

    # ------------------------------------------------------------------
    # Interactive logic
    # ------------------------------------------------------------------

    def _refresh_local_state(self) -> None:
        self._refresh_sources_for_gpu()
        self._refresh_disk_and_models()

    def _refresh_disk_and_models(self) -> None:
        self._refresh_disk_hint()
        self._refresh_models_status()

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.type() == QEvent.ActivationChange and self.isVisible():
            self._refresh_disk_and_models()
        super().changeEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        self._refresh_disk_and_models()

    def _sync_gpu_cards(self) -> None:
        for key, card in self._gpu_cards.items():
            card.sync_visual(key == self._selected_gpu)

    def _on_gpu_toggled(self, key: str, checked: bool) -> None:
        if not checked:
            return
        self._selected_gpu = key
        self._sync_gpu_cards()
        self._cancel_speedtest()
        self._manual_source_selected = False
        self._ranked_urls = []
        if hasattr(self, "sources_status_label"):
            self.sources_status_label.setVisible(False)
        self._refresh_sources_for_gpu()
        self._refresh_disk_hint()

    def _sync_source_rows(self) -> None:
        total = len(self._source_rows)
        for idx, row in enumerate(self._source_rows):
            checked = row.radio.isChecked()
            is_last = idx == total - 1
            pos = "only" if total == 1 else ("top" if idx == 0 else ("bottom" if is_last else "mid"))
            row.row_frame.setProperty("selected", "true" if checked else "false")
            row.row_frame.setProperty("last", "true" if is_last else "false")
            row.row_frame.setProperty("pos", pos)
            row.active_label.setVisible(checked)
            row.row_frame.style().unpolish(row.row_frame)
            row.row_frame.style().polish(row.row_frame)
            row.row_frame.update()

    def _refresh_sources_for_gpu(self) -> None:
        for btn in list(self._source_group.buttons()):
            self._source_group.removeButton(btn)
        while self._sources_layout.count():
            item = self._sources_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._source_rows.clear()

        pkg = GPU_PACKAGES[self._selected_gpu]
        sources = describe_sources(pkg, base_dir=self._base_dir)
        for idx, src in enumerate(sources):
            raw_name = src["name"]
            disp_name = raw_name if len(raw_name) <= 44 else (raw_name[:41] + "...")
            radio = QRadioButton(disp_name)
            radio.setToolTip(f"{raw_name}\n{src['url']}")
            radio.setCursor(Qt.PointingHandCursor)
            radio.setChecked(idx == 0)
            radio.clicked.connect(self._on_source_clicked)
            radio.toggled.connect(lambda _c: self._sync_source_rows())
            self._source_group.addButton(radio)

            row_frame = _ClickableGroupRow(radio, self._on_source_clicked)
            row_h = QHBoxLayout(row_frame)
            row_h.setContentsMargins(SP_MD, SP_SM, SP_MD, SP_SM)
            row_h.setSpacing(SP_SM)
            row_h.addWidget(radio)

            ch_lbl = QLabel(src["channel"])
            ch_lbl.setObjectName("SourceChannel")
            row_h.addWidget(ch_lbl)
            row_h.addStretch(1)

            metric_lbl = QLabel("待测速")
            metric_lbl.setObjectName("SourceMetric")
            metric_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            row_h.addWidget(metric_lbl)

            active_lbl = QLabel("使用中")
            active_lbl.setObjectName("BadgeVerm")
            active_lbl.setFixedHeight(22)
            active_lbl.setAlignment(Qt.AlignCenter)
            row_h.addWidget(active_lbl)

            copy_btn = QPushButton("复制链接")
            copy_btn.setObjectName("SoftBtn")
            copy_btn.setFixedHeight(26)
            copy_btn.setCursor(Qt.PointingHandCursor)
            url = src["url"]
            copy_btn.clicked.connect(
                lambda _=False, u=url, b=copy_btn: self._copy_source_url(u, b)
            )
            row_h.addWidget(copy_btn)

            self._sources_layout.addWidget(row_frame)
            self._source_rows.append(
                _SourceRowWidgets(src, radio, metric_lbl, copy_btn, row_frame, active_lbl)
            )
        self._sync_source_rows()
        self.btn_speedtest.setText("一键测速")
        self.btn_speedtest.setEnabled(True)

    def _on_source_clicked(self) -> None:
        self._manual_source_selected = True

    def _copy_source_url(self, url: str, button: QPushButton) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(url)
        button.setText("已复制")
        QTimer.singleShot(
            1500,
            lambda b=button: b.setText("复制链接") if self._is_valid_qobject(b) else None,
        )

    @staticmethod
    def _is_valid_qobject(obj: object) -> bool:
        if obj is None:
            return False
        try:
            import shiboken6

            if not shiboken6.isValid(obj):
                return False
        except ImportError:
            pass
        try:
            _ = obj.objectName()  # type: ignore[attr-defined]
            return True
        except RuntimeError:
            return False

    def _open_folder_safe(self, path: str) -> None:
        ok, err = _GuideDialog._open_folder(path)
        if not ok:
            self.sources_status_label.setText(err)
            self.sources_status_label.setStyleSheet(f"color: {THEME['warning']}; font-size: 12px;")
            self.sources_status_label.setVisible(True)

    def _open_cache_dir(self) -> None:
        self._open_folder_safe(os.path.join(self._base_dir, "engine_temp"))

    def _open_guide(self) -> None:
        dlg = _GuideDialog(self._base_dir, parent=self)
        dlg.exec()
        self._refresh_disk_and_models()

    def _refresh_disk_hint(self) -> None:
        pkg = GPU_PACKAGES[self._selected_gpu]
        cache_state, _ = classify_local_cache(pkg, base_dir=self._base_dir)
        required, matched_cache = estimate_disk_requirement(pkg, base_dir=self._base_dir)
        req_gib = required / _GiB
        try:
            free = shutil.disk_usage(self._base_dir).free
        except OSError:
            free = 0
        free_gib = free / _GiB

        if cache_state == "complete" and matched_cache > 0:
            prefix = f"检测到本地完整缓存 {matched_cache / _GiB:.1f} GiB · "
        elif cache_state == "partial" and matched_cache > 0:
            prefix = f"检测到断点续传缓存 {matched_cache / _GiB:.1f} GiB · "
        elif cache_state == "unverified" and matched_cache > 0:
            prefix = f"检测到待校验/未完成包 {matched_cache / _GiB:.1f} GiB · "
        else:
            prefix = ""

        free_suffix = (
            "（无法获取可用磁盘空间）"
            if free <= 0
            else f"（当前可用 {free_gib:.1f} GiB）"
        )
        if free > 0 and free < required:
            self.disk_label.setText(
                f"{prefix}空间紧张：需 {req_gib:.1f} GiB · 可用 {free_gib:.1f} GiB"
            )
            self.disk_label.setObjectName("WarnPill")
        else:
            self.disk_label.setText(
                f"{prefix}预计需 {req_gib:.1f} GiB{free_suffix}"
            )
            self.disk_label.setObjectName("BadgeNeutral")
        self.disk_label.style().unpolish(self.disk_label)
        self.disk_label.style().polish(self.disk_label)
        self.disk_label.update()

    def _refresh_models_status(self) -> None:
        ready, missing = check_models_ready(self._base_dir)
        missing_set = set(missing)
        for fname, (ok_lbl, missing_lbl) in self._model_badges.items():
            is_ok = fname not in missing_set
            ok_lbl.setVisible(is_ok)
            missing_lbl.setVisible(not is_ok)

        if ready:
            self.models_status_label.setText(
                f"模型目录已就绪（{GPT_MODEL} / {SOVITS_MODEL} / {REF_WAV} 均已到位）"
            )
            self.models_status_label.setStyleSheet(
                f"color: {THEME['ok']}; font-size: 12px;"
            )
        else:
            self.models_status_label.setText(
                f"尚未就绪（缺少：{'、'.join(missing)}）—— 安装完成后放入 models/ 即可开始配音"
            )
            self.models_status_label.setStyleSheet("")

    def _set_metric_status(self, lbl: QLabel, text: str, status: str) -> None:
        lbl.setText(text)
        lbl.setProperty("status", status)
        lbl.style().unpolish(lbl)
        lbl.style().polish(lbl)

    def _retire_speed_worker(self, w: SpeedTestWorker) -> None:
        if w in self._retired_speed_workers:
            self._retired_speed_workers.remove(w)
        if self._speed_worker is w:
            self._speed_worker = None
        if self._is_valid_qobject(w):
            w.deleteLater()

    def _cancel_speedtest(self) -> None:
        self._speedtest_gen += 1
        if self._speed_worker is not None:
            w = self._speed_worker
            self._speed_worker = None
            w.cancel()
            if self._is_valid_qobject(w) and w.isRunning():
                if w not in self._retired_speed_workers:
                    self._retired_speed_workers.append(w)
            elif self._is_valid_qobject(w):
                w.deleteLater()
        if hasattr(self, "btn_speedtest"):
            self.btn_speedtest.setEnabled(True)
            self.btn_speedtest.setText("重新测速" if self._ranked_urls else "一键测速")
        for row in self._source_rows:
            if row.metric_label.property("status") == "busy":
                self._set_metric_status(row.metric_label, "待测速", "idle")

    def _start_speedtest(self) -> None:
        if not self._source_rows:
            return
        self._cancel_speedtest()
        gen = self._speedtest_gen
        self._manual_source_selected = False
        self._ranked_urls = []
        self.sources_status_label.setVisible(False)
        self.btn_speedtest.setEnabled(False)
        self.btn_speedtest.setText("测速中…")

        pkg = GPU_PACKAGES[self._selected_gpu]
        expected_bytes = int(pkg.get("size_bytes", 0) or 0)
        items: list[tuple[str, int]] = []
        for row in self._source_rows:
            row.speed_mb_s = None
            row.latency_ms = None
            self._set_metric_status(row.metric_label, "测速中…", "busy")
            items.append((row.source["url"], expected_bytes))

        worker = SpeedTestWorker(gen, items, parent=None)
        worker.source_result.connect(self._on_speedtest_result)
        worker.finished_gen.connect(self._on_speedtest_finished)
        worker.finished.connect(lambda w=worker: self._retire_speed_worker(w))
        self._speed_worker = worker
        worker.start()

    def _on_speedtest_result(
        self,
        gen: int,
        url: str,
        speed: object,
        latency: object,
    ) -> None:
        if gen != self._speedtest_gen:
            return
        for row in self._source_rows:
            if row.source["url"] != url:
                continue
            if isinstance(speed, (int, float)) and speed > 0:
                lat_ms = int(latency) if isinstance(latency, (int, float)) else 0
                row.speed_mb_s = float(speed)
                row.latency_ms = lat_ms
                self._set_metric_status(
                    row.metric_label, f"{speed:.1f} MB/s · {lat_ms} ms", "ok"
                )
            else:
                row.speed_mb_s = None
                row.latency_ms = None
                self._set_metric_status(row.metric_label, "不可达", "bad")

    def _on_speedtest_finished(self, gen: int) -> None:
        if gen != self._speedtest_gen:
            return
        self.btn_speedtest.setEnabled(True)
        self.btn_speedtest.setText("重新测速")

        reachable = [
            row for row in self._source_rows
            if row.speed_mb_s is not None and row.speed_mb_s > 0
        ]
        reachable.sort(
            key=lambda r: (
                -(r.speed_mb_s or 0.0),
                r.latency_ms if r.latency_ms is not None else 999999,
            )
        )
        self._ranked_urls = [r.source["url"] for r in reachable] + [
            r.source["url"] for r in self._source_rows if r not in reachable
        ]
        if reachable:
            self.sources_status_label.setVisible(False)
            if not self._manual_source_selected:
                reachable[0].radio.setChecked(True)
        else:
            self.sources_status_label.setText(
                "所有线路测速均不可达：可点击“打开缓存目录”手动下载放入 .7z，或在应用根目录 mirrors.txt 添加自定义镜像。"
            )
            self.sources_status_label.setStyleSheet(
                f"color: {THEME['warning']}; font-size: 12px;"
            )
            self.sources_status_label.setVisible(True)

    # ------------------------------------------------------------------
    # Install start / progress / cancel
    # ------------------------------------------------------------------

    def _selected_source_url(self) -> str | None:
        for row in self._source_rows:
            if row.radio.isChecked():
                return row.source["url"]
        return self._source_rows[0].source["url"] if self._source_rows else None

    def _on_toggle_details(self, checked: bool) -> None:
        self._log_panel.setVisible(bool(checked))
        self.log_view.setVisible(bool(checked))
        self.btn_details.setText("收起安装日志" if checked else "展开安装日志")

    def _set_installing(self, running: bool) -> None:
        for radio in self._radios.values():
            radio.setEnabled(not running)
        for row in self._source_rows:
            row.radio.setEnabled(not running)
        self.btn_speedtest.setEnabled(not running)
        self.btn_start.setEnabled(not running)
        self.btn_cancel.setVisible(running)
        self.btn_cancel.setEnabled(running)
        self.btn_cancel.setText("取消")
        show_progress = (
            running or self.progress_bar.value() > 0 or bool(self._last_install_message)
        )
        self.progress_block.setVisible(show_progress)
        self._idle_footer_wrap.setVisible(not show_progress)
        if getattr(self, "_log_pct_pill", None) is not None:
            self._log_pct_pill.setVisible(show_progress)

    def _ask_continue_without_nvidia(self) -> bool:
        dlg = ThemedDialog(self, title="未检测到 NVIDIA 显卡", width=420)
        msg = QLabel(
            "未检测到 NVIDIA 显卡或驱动，GPT-SoVITS 本地引擎通常需要 NVIDIA GPU（CUDA）运行。\n\n"
            "已为您默认选择兼容性更好的「其他显卡 (NVIDIA RTX 30/40 系等)」。如果本机实际安装有 RTX 50 系列显卡，也可以先返回手动切换。"
        )
        msg.setObjectName("MutedBody")
        msg.setWordWrap(True)
        dlg.body.addWidget(msg)

        btn_cancel = QPushButton("取消")
        btn_cancel.setObjectName("SecondaryBtn")
        btn_cancel.setFixedHeight(CONTROL_H)
        btn_cancel.setCursor(Qt.PointingHandCursor)
        btn_cancel.clicked.connect(dlg.reject)

        btn_ok = QPushButton("继续使用通用包")
        btn_ok.setObjectName("PrimaryBtn")
        btn_ok.setFixedHeight(PRIMARY_H)
        btn_ok.setCursor(Qt.PointingHandCursor)
        btn_ok.setDefault(True)
        btn_ok.clicked.connect(dlg.accept)

        dlg.footer.addWidget(btn_cancel)
        dlg.footer.addWidget(btn_ok)
        dlg.first_focus_widget = btn_ok
        return dlg.exec() == QDialog.Accepted

    def _on_start(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        if self._gpu_detected is None and not self._ask_continue_without_nvidia():
            return
        self._cancel_speedtest()
        self._cancel_requested = False
        self._last_install_message = ""
        self._dl_start_mono = 0.0
        self._dl_start_pct = 0.0
        self.progress_bar.setValue(0)
        self.percent_label.setText("0%")
        self.stage_label.setText("准备安装…")
        self.log_view.clear()
        self._set_installing(True)

        preferred = self._selected_source_url() if self._manual_source_selected else None
        worker = SetupWorker(
            gpu_key=self._selected_gpu,
            base_dir=self._base_dir,
            preferred_url=preferred,
            pre_ranked_urls=self._ranked_urls or None,
            parent=None,
        )
        worker.progress.connect(self._on_progress)
        worker.log.connect(self._append_log)
        worker.finished.connect(self._on_install_finished)
        self._worker = worker
        worker.start()

    def _on_cancel(self) -> None:
        self._cancel_requested = True
        w = self._worker
        if w is not None and self._is_valid_qobject(w) and w.isRunning():
            threading.Thread(target=w.cancel, daemon=True, name="setup-cancel").start()
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setText("正在取消…")
        self.stage_label.setText("正在停止下载/解压并清理现场…")

    def _on_progress(self, pct: float, text: str) -> None:
        if self._cancel_requested:
            return
        clamped = max(0.0, min(1.0, float(pct)))
        self.progress_bar.setValue(int(round(clamped * 1000)))
        pct_str = f"{int(round(clamped * 100))}%"
        self.percent_label.setText(pct_str)
        if getattr(self, "_log_pct_pill", None) is not None:
            self._log_pct_pill.setText(pct_str)
            self._log_pct_pill.setVisible(True)
        stage_txt = text or ""
        dl_weight = STAGES[0][1] if STAGES else 0.72
        if 0.0 < clamped < dl_weight:
            now = time.monotonic()
            if self._dl_start_mono <= 0.0 and clamped > 0.04:
                self._dl_start_mono = now
                self._dl_start_pct = clamped
            elif self._dl_start_mono > 0.0:
                elapsed = now - self._dl_start_mono
                delta_pct = clamped - self._dl_start_pct
                if elapsed >= 3.0 and delta_pct >= 0.005:
                    remain_sec = int(elapsed * (dl_weight - clamped) / delta_pct)
                    if remain_sec >= 60:
                        eta = f"约 {remain_sec // 60} 分 {remain_sec % 60:02d} 秒"
                    else:
                        eta = f"约 {max(1, remain_sec)} 秒"
                    stage_txt = f"{stage_txt}（{eta}）" if stage_txt else f"预计剩余 {eta}"
        if stage_txt:
            self.stage_label.setText(stage_txt)

    def _append_log(self, line: str) -> None:
        self.log_view.appendPlainText(line)

    def _on_install_finished(self, ok: bool, message: str) -> None:
        worker = self._worker
        self._worker = None
        if worker is not None and self._is_valid_qobject(worker):
            worker.deleteLater()
        self._cancel_requested = False
        self._last_install_message = message or ""
        self._set_installing(False)
        self.stage_label.setText(self._last_install_message)
        if message:
            self._append_log(message)
        self._refresh_disk_and_models()
        if ok:
            self.progress_bar.setValue(1000)
            self.percent_label.setText("100%")
            if getattr(self, "_log_pct_pill", None) is not None:
                self._log_pct_pill.setText("100%")
            self.setup_completed.emit()
        else:
            if not self.btn_details.isChecked():
                self.btn_details.setChecked(True)

    def cancel_and_cleanup(self) -> None:
        """Stop background speedtest and installer/7zr workers before close."""
        self._cancel_speedtest()
        terminate_active_install_processes()
        for w in list(self._retired_speed_workers):
            if self._is_valid_qobject(w):
                w.cancel()
                if w.isRunning():
                    w.wait(1000)
            self._retire_speed_worker(w)
        if self._worker is not None and self._is_valid_qobject(self._worker):
            if self._worker.isRunning():
                self._worker.cancel()
                self._worker.wait(5000)
        terminate_active_install_processes()
