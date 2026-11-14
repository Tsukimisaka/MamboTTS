"""Offscreen PySide6 GUI smoke & interaction tests (spec item 26).

QT_QPA_PLATFORM=offscreen is forced BEFORE PySide6 import; the repo root is
prepended to sys.path so the tests run from any working directory.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import contextlib  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import unittest  # noqa: E402
from datetime import datetime  # noqa: E402
from unittest import mock  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from PySide6.QtCore import QMimeData, Qt  # noqa: E402
from PySide6.QtGui import QTextCursor  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractSlider,
    QApplication,
    QPlainTextEdit,
    QPushButton,
)

from mambotts.domain.models import (  # noqa: E402
    HistoryRecord,
    SynthesisJobResult,
)

from mambotts.ui.app_shell import MamboStudioWindow  # noqa: E402
from mambotts.ui.player_history import HistoryItemWidget, StructuredLogBuffer  # noqa: E402
from mambotts.ui.script_editor import ScriptEditorWidget  # noqa: E402
from mambotts.ui.setup_view import EmbeddedSetupView  # noqa: E402

_APP: QApplication | None = None


def _app() -> QApplication:
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


class _FakeWorker:
    """Duck-typed GenerateWorker stand-in for cancel-state testing."""

    def __init__(self) -> None:
        self.cancel_event = threading.Event()

    def is_alive(self) -> bool:
        return True


class UIBaseCase(unittest.TestCase):
    """Shared QApplication bootstrap + per-test cleanup."""

    def setUp(self) -> None:
        self.app = _app()

    def tearDown(self) -> None:
        self.app.processEvents()

    @staticmethod
    def _quiesce(window: MamboStudioWindow) -> None:
        """Stop timers so pending saves / probes can never fire mid-test."""
        window._heartbeat_timer.stop()
        window._save_timer.stop()
        window._synth_worker = None

    @staticmethod
    @contextlib.contextmanager
    def _isolated_studio_window(**kwargs):
        with (
            tempfile.TemporaryDirectory(prefix="mambotts_ui_iso_") as tmp,
            mock.patch("mambotts.ui.app_shell.is_engine_installed", return_value=True),
        ):
            win = MamboStudioWindow(auto_start_engine=False, base_dir=tmp, **kwargs)
            try:
                yield win, tmp
            finally:
                UIBaseCase._quiesce(win)
                win.deleteLater()


class TestWindowInit(UIBaseCase):
    def test_window_init_builds_studio(self) -> None:
        with self._isolated_studio_window() as (window, _tmp):
            window.show()
            self.assertIn("MamboTTS", window.windowTitle())
            self.assertEqual(window.log_view.maximumBlockCount(), 2000)
            self.assertFalse(window.progress_bar.isVisible())
            self.assertTrue(window.player_bar.isVisible())
            self.assertFalse(window.btn_play_pause.isEnabled())
            self.assertFalse(window.btn_cancel_synth.isVisible())
            self.assertFalse(window.btn_engine_retry.isVisible())
            self.assertEqual(window.btn_generate.text(), "合成")
            self.assertEqual(window.script_editor.key_hint.text(), "Ctrl+Enter")
            self.assertTrue(window.status_label.text())

    def test_stack_switch_setup_to_studio(self) -> None:
        with self._isolated_studio_window() as (window, _tmp):
            self._quiesce(window)
            window.show()
            # Mocked engine installed -> studio shown first.
            self.assertIs(window.stack.currentWidget(), window.studio_page)

            window.show_setup_view()
            self.assertIs(window.stack.currentWidget(), window.setup_view)
            self.assertEqual(window.stack.currentIndex(), 0)
            self.assertEqual(window.stack.indexOf(window.setup_view), 0)
            self.assertEqual(window.stack.indexOf(window.studio_page), 1)

            window.show_studio()
            self.assertIs(window.stack.currentWidget(), window.studio_page)
            self.assertEqual(window.stack.currentIndex(), 1)


class TestLogBuffer(UIBaseCase):
    def test_buffer_render_preserves_entries(self) -> None:
        buffer = StructuredLogBuffer()
        buffer.append("info", "hello")
        buffer.append("error", "boom")
        view = QPlainTextEdit()
        buffer.render_all(view)
        self.assertEqual(len(buffer), 2)
        self.assertIn("hello", view.toPlainText())
        self.assertIn("boom", view.toPlainText())


class TestScriptEditor(UIBaseCase):
    def test_no_quick_insert_tag_chips_or_clause_drawer(self) -> None:
        editor = ScriptEditorWidget()
        self.assertEqual(editor.findChildren(QPushButton, "TagChip"), [])
        self.assertFalse(hasattr(editor, "insert_tag"))
        editor.deleteLater()

    def test_selection_colors_are_high_contrast(self) -> None:
        from PySide6.QtGui import QColor, QPalette
        from mambotts.ui.theme import THEME, apply_selection_palette, base_qss

        paper = QColor(THEME["paper"])
        sel = QColor(THEME["selection"])
        self.assertGreater(abs(paper.red() - sel.red()), 40)
        self.assertGreater(abs(paper.blue() - sel.blue()), 5)
        self.assertNotEqual(THEME["selection"], THEME["surface3"])
        qss = base_qss()
        self.assertIn(f"selection-background-color: {THEME['selection']};", qss)
        self.assertIn(f"selection-background-color: {THEME['selection_menu']};", qss)

        app = _app()
        apply_selection_palette(app)
        pal = app.palette()
        self.assertEqual(pal.color(QPalette.Active, QPalette.Highlight).name().lower(), THEME["selection"].lower())
        self.assertEqual(
            pal.color(QPalette.Inactive, QPalette.Highlight).name().lower(),
            THEME["selection_inactive"].lower(),
        )
        self.assertEqual(pal.color(QPalette.Active, QPalette.HighlightedText).name().lower(), THEME["text"].lower())

    def test_editor_config(self) -> None:
        editor = ScriptEditorWidget()
        self.assertFalse(editor.text_input.acceptRichText())
        self.assertGreaterEqual(editor.text_input.minimumHeight(), 100)

    def test_paste_applies_line_height_and_undoes_as_one_step(self) -> None:
        editor = ScriptEditorWidget()
        mime = QMimeData()
        mime.setText("第一段文案\n第二段文案\n第三段文案")
        editor.text_input.insertFromMimeData(mime)
        self.assertEqual(len(editor.text_input.toPlainText().splitlines()), 3)
        block = editor.text_input.document().begin()
        blocks = 0
        while block.isValid():
            fmt = block.blockFormat()
            height_type = getattr(
                fmt.lineHeightType(), "value", fmt.lineHeightType()
            )
            self.assertEqual(height_type, 1)
            self.assertAlmostEqual(fmt.lineHeight(), 175.0, places=1)
            blocks += 1
            block = block.next()
        self.assertGreaterEqual(blocks, 3)
        editor.text_input.undo()
        self.assertEqual(editor.text_input.toPlainText(), "")

    def test_duration_estimate_follows_speed(self) -> None:
        editor = ScriptEditorWidget()
        editor.text_input.setPlainText("一二三四五六七八九十")
        editor.set_speed(1.0)
        self.assertEqual(editor.count_label.text(), "10 字")
        self.assertEqual(editor.estimate_label.text(), "约 2 秒")
        editor.set_speed(2.0)
        self.assertEqual(editor.estimate_label.text(), "约 1 秒")

    def test_count_counts_visible_non_whitespace_chars(self) -> None:
        editor = ScriptEditorWidget()
        editor.text_input.setPlainText("你好 世界\n*Ctrl+C*")
        self.assertEqual(editor.count_label.text(), "12 字")


class TestCancelStates(UIBaseCase):
    def test_cancel_button_state_transitions(self) -> None:
        with self._isolated_studio_window() as (window, _tmp):
            self._quiesce(window)
            window.show()
            self.assertFalse(window.btn_cancel_synth.isVisible())

            window._set_busy(True)
            self.assertTrue(window.btn_cancel_synth.isVisible())
            self.assertFalse(window.btn_generate.isEnabled())
            self.assertFalse(window.script_editor.text_input.isEnabled())
            self.assertFalse(window.speed_slider.isEnabled())
            self.assertFalse(window.path_input.isEnabled())
            self.assertFalse(window.name_input.isEnabled())
            self.assertFalse(window.btn_browse.isEnabled())
            self.assertEqual(window.btn_cancel_synth.text(), "取消合成")
            self.assertEqual(window.btn_generate.text(), "合成中…")

            fake = _FakeWorker()
            window._synth_worker = fake
            window.cancel_synthesis()
            self.assertTrue(fake.cancel_event.is_set())
            self.assertIn("正在取消", window.btn_cancel_synth.text())
            self.assertFalse(window.btn_cancel_synth.isEnabled())

            window._on_generate_done(SynthesisJobResult(success=False, message="合成已取消。"))
            self.assertIsNone(window._synth_worker)
            self.assertEqual(window.btn_cancel_synth.text(), "取消合成")
            self.assertFalse(window.btn_cancel_synth.isVisible())
            self.assertTrue(window.btn_generate.isEnabled())
            self.assertTrue(window.script_editor.text_input.isEnabled())
            self.assertEqual(window.status_label.text(), "已取消合成")

    def test_cancel_without_worker_is_noop(self) -> None:
        with self._isolated_studio_window() as (window, _tmp):
            self._quiesce(window)
            window.cancel_synthesis()  # no worker -> must not raise
            self.assertEqual(window.btn_cancel_synth.text(), "取消合成")


class TestHistoryWidgets(UIBaseCase):
    def test_meta_is_short_and_friendly(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d %H:%M")
        item = HistoryItemWidget(
            HistoryRecord(
                record_id=1,
                path="x.wav",
                text="文本",
                time=today,
                duration=3.2,
                speed=1.2,
            )
        )
        meta = item.meta_label.fullText()
        self.assertIn("今天", meta)
        self.assertIn("1.20x", meta)

        default_item = HistoryItemWidget(
            HistoryRecord(
                record_id=2,
                path="y.wav",
                text="文本",
                time=today,
                duration=1.0,
            )
        )
        self.assertNotIn("默认", default_item.meta_label.fullText())
        item.deleteLater()
        default_item.deleteLater()

    def test_history_item_missing_state(self) -> None:
        healthy = HistoryItemWidget(
            HistoryRecord(
                record_id=1, path="x.wav", text="文本内容", time="2026-11-04 10:00", duration=3.2
            )
        )
        self.assertTrue(healthy.play_btn.isEnabled())
        self.assertTrue(healthy.save_btn.isEnabled())
        self.assertTrue(healthy.delete_btn.isEnabled())

        missing = HistoryItemWidget(
            HistoryRecord(
                record_id=2,
                path="Z:/nope/missing.wav",
                text="丢失的记录",
                time="2026-11-04 10:00",
                duration=1.0,
                missing=True,
            )
        )
        self.assertFalse(missing.play_btn.isEnabled())
        self.assertFalse(missing.save_btn.isEnabled())
        self.assertTrue(missing.delete_btn.isEnabled())
        self.assertEqual(missing.title_label.text(), "丢失的记录")
        self.assertTrue(missing.missing_tag.isVisibleTo(missing))
        missing.deleteLater()


class TestResponsiveWindow(UIBaseCase):
    def test_action_row_wraps_at_1000(self) -> None:
        with self._isolated_studio_window() as (window, _tmp):
            self._quiesce(window)
            window.resize(1000, 700)
            window.show()
            QTest.qWait(100)
            self.assertTrue(window.action_compact.isVisible())
            self.assertFalse(window.action_wide.isVisible())

            window.resize(1240, 800)
            QTest.qWait(100)
            self.assertFalse(window.action_compact.isVisible())
            self.assertTrue(window.action_wide.isVisible())

    def test_speed_double_click_resets(self) -> None:
        with self._isolated_studio_window() as (window, _tmp):
            self._quiesce(window)
            window.show()
            window.speed_slider.setValue(150)
            QTest.mouseDClick(
                window.speed_slider,
                Qt.LeftButton,
                pos=window.speed_slider.rect().center(),
            )
            QTest.qWait(30)
            self.assertEqual(window.speed_slider.value(), 100)

    def test_missing_models_banner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            window = MamboStudioWindow(auto_start_engine=False, base_dir=tmp)
            try:
                self._quiesce(window)
                self.assertFalse(window.model_banner.isHidden())
                self.assertIn(
                    "缺少曼波音色模型", window.model_banner_text.text()
                )
            finally:
                self._quiesce(window)
                window.deleteLater()


class TestEngineStatusAndSpeed(UIBaseCase):
    """Startup-state latch, top-bar endpoint label and 0.05x slider snap."""

    def test_speed_slider_snaps_to_005_steps(self) -> None:
        with self._isolated_studio_window() as (window, _tmp):
            self._quiesce(window)
            window.speed_slider.setValue(101)
            self.assertEqual(window.speed_slider.value(), 100)
            self.assertEqual(window.speed_value_label.text(), "1.00x")
            window.speed_slider.setValue(137)
            self.assertEqual(window.speed_slider.value(), 135)
            self.assertEqual(window.speed_value_label.text(), "1.35x")
            # Keyboard steps are 0.05x as well.
            window.speed_slider.setValue(100)
            window.speed_slider.triggerAction(
                QAbstractSlider.SliderSingleStepAdd
            )
            self.assertEqual(window.speed_value_label.text(), "1.05x")

    def test_starting_latch_never_flashes_offline(self) -> None:
        with self._isolated_studio_window() as (window, _tmp):
            self._quiesce(window)
            window.show()
            window._engine_starting = True
            window.supervisor.is_running = lambda: True
            window._on_engine_start_done(True, "已启动引擎子进程。")
            self.assertTrue(window._engine_starting)
            self.assertEqual(window.engine_status_label.text(), "引擎启动中")
            self.assertFalse(window.btn_engine_retry.isVisible())
            # Failed probes while the model loads must keep the latch.
            window._on_heartbeat_result(False)
            self.assertEqual(window.engine_status_label.text(), "引擎启动中")
            self.assertFalse(window.btn_engine_retry.isVisible())
            # First successful heartbeat releases it.
            window._on_heartbeat_result(True)
            self.assertEqual(window.engine_status_label.text(), "引擎在线")
            self.assertFalse(window._engine_starting)
            self.assertEqual(window.status_label.text(), "")

    def test_dead_engine_process_reports_offline(self) -> None:
        with self._isolated_studio_window() as (window, _tmp):
            self._quiesce(window)
            window.show()
            window._engine_starting = True
            window.supervisor.is_running = lambda: False  # 子进程已退出
            window._on_engine_start_done(True, "已启动引擎子进程。")
            window._on_heartbeat_result(False)
            self.assertEqual(window.engine_status_label.text(), "引擎离线")
            self.assertTrue(window.btn_engine_retry.isVisible())

    def test_engine_url_shown_next_to_state_label(self) -> None:
        with self._isolated_studio_window() as (window, _tmp):
            self._quiesce(window)
            self.assertEqual(window.engine_url_label.text(), "127.0.0.1:9880")
            window._set_engine_online()
            self.assertEqual(window.engine_url_label.text(), "127.0.0.1:9880")
            self.assertNotIn("http", window.engine_url_label.text())
            self.assertEqual(window.status_label.text(), "")


class TestSetupView(UIBaseCase):
    @staticmethod
    def _mock_disk_usage(_path):
        import collections
        Usage = collections.namedtuple("usage", ["total", "used", "free"])
        return Usage(200 * 1024**3, 100 * 1024**3, 100 * 1024**3)

    def test_setup_view_builds_idle(self) -> None:
        with (
            tempfile.TemporaryDirectory(prefix="mambo_setup_idle_") as tmp,
            mock.patch("mambotts.ui.setup_view.shutil.disk_usage", side_effect=self._mock_disk_usage),
        ):
            view = EmbeddedSetupView(base_dir=tmp)
        try:
            self.assertTrue(view.btn_start.isEnabled())
            self.assertFalse(view.btn_cancel.isEnabled())
            self.assertFalse(view.btn_cancel.isVisible())
            self.assertGreaterEqual(len(view._source_rows), 4)
            for r in view._source_rows:
                self.assertNotIn("baidu", r.source["url"].lower())
            view.resize(1000, 680)
            view.show()
            _app().processEvents()
            # Log + progress stay collapsed until installation starts.
            self.assertFalse(view.log_view.isVisible())
            self.assertFalse(view.progress_block.isVisible())
            # Pinned footer CTA stays visible above the fold at 1000x680.
            self.assertTrue(view.btn_start.isVisible())
            self.assertLessEqual(
                view.btn_start.mapTo(view, view.btn_start.rect().bottomRight()).y(),
                view.height(),
            )
            # No source rows overlap each other geometrically.
            prev_bottom = -1
            for r in view._source_rows:
                top_y = r.radio.mapTo(view, r.radio.rect().topLeft()).y()
                bot_y = r.radio.mapTo(view, r.radio.rect().bottomLeft()).y()
                self.assertGreaterEqual(top_y, prev_bottom - 1)
                prev_bottom = bot_y
        finally:
            view.cancel_and_cleanup()
            view.deleteLater()

    def test_gpu_radio_click_updates_selection_and_precheck(self) -> None:
        from mambotts.infra.installer import GPU_PACKAGES

        with (
            tempfile.TemporaryDirectory(prefix="mambo_setup_radio_") as tmp,
            mock.patch("mambotts.ui.setup_view.shutil.disk_usage", side_effect=self._mock_disk_usage),
        ):
            view = EmbeddedSetupView(base_dir=tmp)
        try:
            view.show()
            keys = list(GPU_PACKAGES)
            if len(keys) < 2:
                self.skipTest("needs at least two GPU packages")
            initial = view._selected_gpu
            other = next(k for k in keys if k != initial)
            # Simulate the user clicking the other package's radio.
            view._radios[other].setChecked(True)
            self.assertEqual(
                view._selected_gpu, other,
                "radio click must override auto-detection",
            )
            self.assertTrue(view.disk_label.text(),
                            "disk precheck must refresh for the new package")
        finally:
            view.cancel_and_cleanup()
            view.deleteLater()

    def test_speedtest_auto_ranks_unless_manual_override(self) -> None:
        with (
            tempfile.TemporaryDirectory(prefix="mambo_setup_speed_") as tmp,
            mock.patch("mambotts.ui.setup_view.shutil.disk_usage", side_effect=self._mock_disk_usage),
        ):
            view = EmbeddedSetupView(base_dir=tmp)
        try:
            gen = view._speedtest_gen
            u0 = view._source_rows[0].source["url"]
            u1 = view._source_rows[1].source["url"]
            view._on_speedtest_result(gen, u0, 1.5, 240)
            view._on_speedtest_result(gen, u1, 25.0, 20)
            view._on_speedtest_finished(gen)
            self.assertTrue(view._source_rows[1].radio.isChecked())

            # Manual override suppresses auto switch on subsequent completion.
            view._source_rows[0].radio.setChecked(True)
            view._on_source_clicked()
            view._on_speedtest_finished(gen)
            self.assertTrue(view._source_rows[0].radio.isChecked())
        finally:
            view.cancel_and_cleanup()
            view.deleteLater()


class TestAppShellBugFixes(UIBaseCase):
    def test_bug_c_tray_toggle_at_runtime(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mambotts_tray_") as tmp:
            window = MamboStudioWindow(auto_start_engine=False, base_dir=tmp)
            try:
                self._quiesce(window)
                with mock.patch("mambotts.ui.app_shell.QSystemTrayIcon") as tray_cls:
                    tray_cls.isSystemTrayAvailable.return_value = True
                    inst = mock.MagicMock()
                    tray_cls.return_value = inst
                    window.chk_tray.setChecked(True)
                    self.assertTrue(window.cfg.minimize_to_tray)
                    self.assertIsNotNone(window._tray)
                    inst.show.assert_called_once()

                    window.chk_tray.setChecked(False)
                    self.assertFalse(window.cfg.minimize_to_tray)
                    self.assertIsNone(window._tray)
                    inst.hide.assert_called_once()
            finally:
                self._quiesce(window)
                window.deleteLater()

    def test_bug_f_realtime_output_summary_dot_preserve_and_invalid_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mambotts_bugf_") as tmp:
            window = MamboStudioWindow(auto_start_engine=False, base_dir=tmp)
            try:
                self._quiesce(window)
                uncreated = os.path.join(tmp, "not_created_yet")
                window.path_input.setText(uncreated)
                window.name_input.setText("story.v2")
                self.assertFalse(os.path.exists(uncreated))
                self.assertIn("story.v2", window.output_summary.text())

                params = window._build_job_params("你好，曼波。")
                self.assertTrue(os.path.isdir(uncreated))
                self.assertTrue(params["save_path"].endswith("story.v2.wav"))

                window.script_editor.text_input.setPlainText("你好。")
                window._engine_online = True
                window.path_input.setText(os.path.join(tmp, "bad<>dir"))
                with mock.patch.object(window, "_launch_worker") as launch_mock:
                    window._start_synthesis()
                launch_mock.assert_not_called()
                self.assertIn("不可写", window.status_label.text())
            finally:
                self._quiesce(window)
                window.deleteLater()

    def test_bug_g_url_edit_invalidates_stale_and_checks_port(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mambotts_bugg_") as tmp:
            window = MamboStudioWindow(auto_start_engine=False, base_dir=tmp)
            try:
                self._quiesce(window)
                window.show()
                window.supervisor.is_running = lambda: True
                window.supervisor._bound_api_url = "http://127.0.0.1:9880"
                window._engine_starting = True
                window.gsv_client._active_models = ("a.ckpt", "b.pth")

                window.api_input.setText("http://127.0.0.1:9999")
                with mock.patch.object(window, "_startup_probe") as probe_mock:
                    window._on_api_url_edited()
                probe_mock.assert_called_once_with(auto_start=False)
                self.assertIsNone(window.gsv_client._active_models)

                window._engine_starting = True
                window._set_engine_offline()
                self.assertEqual(window.engine_status_label.text(), "引擎离线")
            finally:
                self._quiesce(window)
                window.deleteLater()

    def test_bug_h_qbuffer_zero_lock_and_missing_card_and_delete(self) -> None:
        import struct
        import wave

        with tempfile.TemporaryDirectory(prefix="mambotts_bugh_") as tmp:
            wav_path = os.path.join(tmp, "sample.wav")
            with wave.open(wav_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(32000)
                wf.writeframes(struct.pack("<1600h", *([0] * 1600)))

            window = MamboStudioWindow(auto_start_engine=False, base_dir=tmp)
            try:
                self._quiesce(window)
                window.show()
                rec = HistoryRecord(
                    record_id=1,
                    path=wav_path,
                    text="锁测试语音",
                    time="2026-11-13 10:00",
                    duration=0.05,
                )
                window._history_records = [rec]
                window._render_history()
                window._load_audio(wav_path)
                _app().processEvents()

                # QBuffer leaves zero OS lock on wav_path even on Windows.
                os.remove(wav_path)
                self.assertFalse(os.path.exists(wav_path))

                # Clicking missing card disables buttons without opening QFileDialog.
                with mock.patch("mambotts.ui.app_shell.QFileDialog.getSaveFileName") as dlg:
                    window._save_history_copy(rec)
                dlg.assert_not_called()
                card = window._find_history_card(rec)
                self.assertIsNotNone(card)
                self.assertTrue(rec.missing)
                self.assertFalse(card.play_btn.isEnabled())
                self.assertFalse(card.save_btn.isEnabled())

                # Deleting active track resets player bar cleanly.
                window._delete_history_record(rec)
                self.assertFalse(window._has_audio)
                self.assertEqual(window._current_audio_path, "")
            finally:
                self._quiesce(window)
                window.deleteLater()


class TestUiAuditRegressions(UIBaseCase):
    """Regressions from post-edit Goldfish audit across setup_view, app_shell, script_editor."""

    def test_setup_view_guide_dialog_worker_signature_0pct_fail_and_auto_speedtest(self) -> None:
        import collections
        from mambotts.ui.setup_view import _GuideDialog

        du_tuple = collections.namedtuple("_DiskUsage", "total used free")(100 << 30, 0, 80 << 30)
        with tempfile.TemporaryDirectory(prefix="mambo_setup_audit_") as tmp:
            with (
                mock.patch("mambotts.ui.setup_view.detect_gpu_type", return_value="general"),
                mock.patch("mambotts.ui.setup_view.shutil.disk_usage", return_value=du_tuple),
            ):
                view = EmbeddedSetupView(base_dir=tmp)
            try:
                view.show()
                _app().processEvents()

                # 1) _GuideDialog uses self.body cleanly without AttributeError
                dlg = _GuideDialog(tmp, view)
                dlg.close()
                dlg.deleteLater()

                # 2) Auto speedtest by default when user did not manually pick a source
                self.assertFalse(view._manual_source_selected)
                called_prefs: list[object] = []

                def fake_install_ok(*, gpu_type, base_dir, log_cb, progress_cb, state_cb, preferred_url, pre_ranked_urls, cancel_cb):
                    called_prefs.append(preferred_url)
                    state_cb("正在准备…")
                    progress_cb(0.0)
                    state_cb("解压中…")
                    progress_cb(0.65)
                    progress_cb(1.0)
                    return (True, "ok")

                with mock.patch("mambotts.ui.setup_view.install_engine", side_effect=fake_install_ok):
                    view._on_start()
                    self.assertIsNotNone(view._worker)
                    view._worker.wait(5000)
                _app().processEvents()
                self.assertEqual(called_prefs, [None])
                self.assertIsNone(view._worker)

                # 3) 0% failure keeps progress_block visible with failure stage_label
                def fake_install_fail(*, gpu_type, base_dir, log_cb, progress_cb, state_cb, preferred_url, pre_ranked_urls, cancel_cb):
                    state_cb("下载失败")
                    progress_cb(0.0)
                    return (False, "所有源均失败")

                with mock.patch("mambotts.ui.setup_view.install_engine", side_effect=fake_install_fail):
                    view._on_start()
                    view._worker.wait(5000)
                _app().processEvents()
                self.assertTrue(view.progress_block.isVisible())
                self.assertIn("所有源均失败", view.stage_label.text())
                self.assertIsNone(view._worker)
                self.assertEqual(view.log_view.height(), 200)

                # 4) Canceling speedtest resets busy row badge cleanly
                def slow_probe(url, **_kw):
                    threading.Event().wait(0.2)
                    return (5.0 * 1024 * 1024, 10)

                with mock.patch("mambotts.ui.setup_view.probe_source_metrics", side_effect=slow_probe):
                    view._start_speedtest()
                    self.assertIsNotNone(view._speed_worker)
                    view._cancel_speedtest()
                _app().processEvents()
                self.assertIsNone(view._speed_worker)
                self.assertEqual(view.btn_speedtest.text(), "一键测速")
                for row in view._source_rows:
                    self.assertNotEqual(row.metric_label.text(), "测速中…")

                # 5) cancel_and_cleanup always kills 7zr even if worker cleanup runs
                with mock.patch("mambotts.ui.setup_view.terminate_active_install_processes") as kill_7z:
                    view.cancel_and_cleanup()
                kill_7z.assert_called()
            finally:
                view.cancel_and_cleanup()
                view.deleteLater()

    def test_shell_dot_isolation_tooltips_force_restart_and_config_preservation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mambo_shell_audit_") as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                fh.write('{"api_url": "ftp://invalid-url", "speed": 1.13}')

            window = MamboStudioWindow(auto_start_engine=False, base_dir=tmp)
            try:
                self._quiesce(window)
                window.show()
                _app().processEvents()

                # Invalid loaded api_url fell back to DEFAULT_API_URL; speed 1.13 preserved untouched
                self.assertTrue(window.cfg.api_url.startswith("http://127.0.0.1"))
                self.assertAlmostEqual(window._effective_speed(), 1.13, places=2)
                window._save_config()
                with open(cfg_path, "r", encoding="utf-8") as fh:
                    self.assertIn("1.13", fh.read())

                # engine_dot stays green even when transient UI warning happens
                window._set_engine_online()
                green_dot_qss = window.engine_dot.styleSheet()
                window._set_status("请先输入配音脚本", "warn")
                self.assertEqual(window.engine_dot.styleSheet(), green_dot_qss)

                # Unpronounceable text blocked cleanly
                window.script_editor.text_input.setPlainText("。。。？！")
                with mock.patch.object(window, "_launch_worker") as launch_mock:
                    window._start_synthesis()
                launch_mock.assert_not_called()
                self.assertIn("可朗读", window.status_label.text())

                # _set_busy preserves original tooltips after cycle
                orig_settings_tip = window.btn_settings.toolTip()
                orig_gen_tip = window.btn_generate.toolTip()
                window._set_busy(True)
                window._set_busy(False)
                self.assertEqual(window.btn_settings.toolTip(), orig_settings_tip)
                self.assertEqual(window.btn_generate.toolTip(), orig_gen_tip)

                # Force restart clears models via reset_active_models & does not flash "合成失败"
                fake_worker = mock.Mock()
                fake_worker.is_alive.return_value = True
                fake_worker.cancel_event = threading.Event()
                window._synth_worker = fake_worker
                window.supervisor.stop = mock.Mock()
                window.supervisor.is_running = lambda: True

                with (
                    mock.patch.object(window.gsv_client, "reset_active_models") as reset_mock,
                    mock.patch.object(window, "_start_engine_subprocess"),
                ):
                    window._force_restart_engine()
                    # Wait briefly for daemon stop thread to emit _force_restart_done
                    for _ in range(40):
                        _app().processEvents()
                        if reset_mock.called:
                            break
                        threading.Event().wait(0.02)
                reset_mock.assert_called_once()
                window._on_generate_done(SynthesisJobResult(False, "Connection aborted"))
                self.assertNotIn("失败", window.status_label.text())
            finally:
                self._quiesce(window)
                window.deleteLater()

    def test_shell_output_desktop_check_temp_playback_cleanup_and_unavailable_tray(self) -> None:
        import struct
        import wave

        with tempfile.TemporaryDirectory(prefix="mambo_out_audit_") as tmp:
            window = MamboStudioWindow(auto_start_engine=False, base_dir=tmp)
            try:
                self._quiesce(window)
                window.cfg.output_dir = tmp
                # Clearing path_input immediately targets Desktop, not stale cfg.output_dir
                window.path_input.setText("")
                self.assertEqual(window._output_path_display(), "桌面")
                self.assertNotEqual(
                    os.path.normcase(os.path.abspath(window._current_output_dir())),
                    os.path.normcase(os.path.abspath(tmp)),
                )

                # D:\Audio\Desktop must NOT be mislabeled as "桌面"
                fake_desktop = os.path.join(tmp, "Audio", "Desktop")
                window.path_input.setText(fake_desktop)
                self.assertNotEqual(window._output_path_display(), "桌面")
                self.assertIn("合成时自动新建", window.output_summary.text())

                # Unavailable system tray warns in status_label
                with mock.patch("mambotts.ui.app_shell.QSystemTrayIcon.isSystemTrayAvailable", return_value=False):
                    window.chk_tray.setChecked(True)
                self.assertFalse(window.chk_tray.isChecked())
                self.assertIn("托盘", window.status_label.text())

                # Player temp cleanup on >128 MiB simulated file size & _on_player_error
                wav_path = os.path.join(tmp, "large.wav")
                with wave.open(wav_path, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(32000)
                    wf.writeframes(struct.pack("<100h", *([0] * 100)))

                real_getsize = os.path.getsize
                with mock.patch(
                    "mambotts.ui.app_shell.os.path.getsize",
                    side_effect=lambda p: (130 * 1024 * 1024) if p == wav_path else real_getsize(p),
                ):
                    window._load_audio(wav_path)
                for _ in range(30):
                    _app().processEvents()
                    if window._temp_play_file and os.path.isfile(window._temp_play_file):
                        break
                    threading.Event().wait(0.02)
                staged = window._temp_play_file
                self.assertTrue(staged and os.path.isfile(staged))
                window._on_player_error(None, "decode error")
                self.assertIn("播放失败", window.status_label.text())
                self.assertFalse(window._has_audio)
                for _ in range(10):
                    _app().processEvents()
                    if not os.path.exists(staged):
                        break
                    threading.Event().wait(0.05)
                if os.path.exists(staged):
                    os.remove(staged)
            finally:
                self._quiesce(window)
                window.deleteLater()

    def test_script_editor_set_plain_text_preserves_line_height(self) -> None:
        from PySide6.QtGui import QTextBlockFormat

        editor = ScriptEditorWidget()
        try:
            editor.text_input.setPlainText("第一行\n第二行")
            fmt = editor.text_input.document().firstBlock().blockFormat()
            self.assertEqual(
                fmt.lineHeightType(),
                QTextBlockFormat.LineHeightTypes.ProportionalHeight.value,
            )
            self.assertAlmostEqual(fmt.lineHeight(), 175.0, places=1)
        finally:
            editor.deleteLater()

    def test_p2_and_p3_ui_hardening_regressions(self) -> None:
        import collections
        import time as _time
        from PySide6.QtWidgets import QWidget
        from mambotts.ui.app_shell import (
            _acquire_single_instance_mutex,
            _release_single_instance_mutex,
        )
        from mambotts.ui.player_history import friendly_time
        from mambotts.ui.setup_view import _GuideDialog
        from mambotts.ui.theme import _url
        from mambotts.ui.widgets import ShadowLayer

        # 1. SetupView without detected NVIDIA GPU defaults to "general" & prompts confirmation.
        Usage = collections.namedtuple("usage", ["total", "used", "free"])
        with (
            tempfile.TemporaryDirectory(prefix="mambo_nogpu_") as tmp,
            mock.patch("mambotts.ui.setup_view.detect_gpu_type", return_value=None),
            mock.patch("mambotts.ui.setup_view.shutil.disk_usage", return_value=Usage(200 << 30, 0, 100 << 30)),
        ):
            view = EmbeddedSetupView(base_dir=tmp)
            try:
                self.assertEqual(view._selected_gpu, "general")
                with (
                    mock.patch.object(view, "_ask_continue_without_nvidia", return_value=False) as ask,
                    mock.patch("mambotts.ui.setup_view.SetupWorker.start") as worker_start,
                ):
                    view._on_start()
                ask.assert_called_once()
                worker_start.assert_not_called()
            finally:
                view.cancel_and_cleanup()
                view.deleteLater()

        # 2. _GuideDialog puts default focus on "知道了", not "打开 engine_temp/".
        with tempfile.TemporaryDirectory(prefix="mambo_guide_focus_") as tmp:
            dlg = _GuideDialog(tmp)
            try:
                self.assertIsNotNone(dlg.first_focus_widget)
                self.assertEqual(dlg.first_focus_widget.text(), "知道了")
            finally:
                dlg.deleteLater()

        # 3. Theme _url quotes paths (including spaces), friendly_time accepts %H:%M:%S.
        self.assertEqual(_url(r"C:\Users\John Doe\icon.svg"), 'image: url("C:/Users/John Doe/icon.svg");')
        today_sec = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.assertTrue(friendly_time(today_sec).startswith("今天 "))

        # 4. ShadowLayer cache stays bounded across 25 resizes.
        parent = QWidget()
        parent.resize(800, 600)
        child = QWidget(parent)
        child.resize(200, 100)
        child.show()
        layer = ShadowLayer(parent)
        layer.resize(800, 600)
        layer.register(child, level=1)
        parent.show()
        _app().processEvents()
        for idx in range(25):
            child.resize(200 + idx * 4, 100 + idx * 2)
            _app().processEvents()
            layer.repaint()
        self.assertGreater(len(layer._cache), 0)
        self.assertLessEqual(len(layer._cache), layer._MAX_CACHE_ENTRIES)
        parent.deleteLater()

        # 5. Single-instance Win32 mutex rejects overlapping acquisitions for same base_dir.
        with tempfile.TemporaryDirectory(prefix="mambo_mutex_") as tmp:
            h1 = _acquire_single_instance_mutex(tmp)
            self.assertIsNotNone(h1)
            h2 = _acquire_single_instance_mutex(tmp)
            try:
                self.assertIsNone(h2)
            finally:
                _release_single_instance_mutex(h1)
            h3 = _acquire_single_instance_mutex(tmp)
            self.assertIsNotNone(h3)
            _release_single_instance_mutex(h3)

        # 6. Studio window: startup thread exception guarantees _start_done, timeout surfaces retry,
        # retry on stuck alive process invokes _force_restart_engine, and history copy works async.
        with self._isolated_studio_window() as (window, tmp):
            window.show()
            window.supervisor.start = mock.Mock(side_effect=OSError("boom"))
            window._start_engine_subprocess()
            for _ in range(40):
                _app().processEvents()
                if not window._starting_in_flight:
                    break
                threading.Event().wait(0.02)
            self.assertFalse(window._starting_in_flight)
            self.assertFalse(window._engine_starting)
            self.assertEqual(window.engine_status_label.text(), "启动失败")

            # Startup deadline expiration surfaces "启动超时" + retry -> force restart.
            window._engine_starting = True
            window._engine_start_deadline = _time.monotonic() - 1.0
            window.supervisor.is_running_for = lambda _url: True
            window._set_engine_offline()
            self.assertFalse(window._engine_starting)
            self.assertEqual(window.engine_status_label.text(), "启动超时")
            self.assertTrue(window.btn_engine_retry.isVisible())

            with mock.patch.object(window, "_force_restart_engine") as force_restart:
                window._on_engine_retry()
            force_restart.assert_called_once()

            # Async _save_history_copy copies wav + srt off the UI thread.
            src_wav = os.path.join(tmp, "src.wav")
            src_srt = os.path.join(tmp, "src.srt")
            dst_wav = os.path.join(tmp, "exported.wav")
            with open(src_wav, "wb") as fh:
                fh.write(b"RIFFdemo")
            with open(src_srt, "w", encoding="utf-8") as fh:
                fh.write("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
            rec = HistoryRecord(
                record_id=1,
                path=src_wav,
                text="hi",
                time=today_sec,
                duration=1.0,
                srt_path=src_srt,
            )
            with mock.patch("mambotts.ui.app_shell.QFileDialog.getSaveFileName", return_value=(dst_wav, "")):
                window._save_history_copy(rec)
            for _ in range(40):
                _app().processEvents()
                if os.path.isfile(dst_wav) and os.path.isfile(os.path.join(tmp, "exported.srt")):
                    break
                threading.Event().wait(0.02)
            self.assertTrue(os.path.isfile(dst_wav))
            self.assertTrue(os.path.isfile(os.path.join(tmp, "exported.srt")))


class TestProposalCLayoutContracts(UIBaseCase):
    """Regressions covering Proposal C modular cards, responsive wells, and native setup."""

    def test_script_editor_clear_button_and_locking(self) -> None:
        editor = ScriptEditorWidget()
        try:
            self.assertTrue(editor.btn_clear.isHidden())
            editor.text_input.setPlainText("一二三四")
            self.assertFalse(editor.btn_clear.isHidden())
            self.assertEqual(editor.count_label.text(), "4 字")

            editor.set_locked(True)
            self.assertTrue(editor.btn_clear.isHidden())
            # Calling clear_text programmatically while locked stays safe or waits until unlock
            editor.set_locked(False)
            self.assertFalse(editor.btn_clear.isHidden())

            editor.clear_text()
            self.assertEqual(editor.text_input.toPlainText(), "")
            self.assertTrue(editor.btn_clear.isHidden())
        finally:
            editor.deleteLater()

    def test_module_card_elided_label_and_history_playing_tag(self) -> None:
        from mambotts.ui.widgets import ElidedLabel, ModuleCard

        card = ModuleCard("模块标题", "初始副标题")
        lbl = ElidedLabel("初始")
        try:
            card.set_subtitle("更新副标题")
            self.assertEqual(card.header_bar.subtitle_label.text(), "更新副标题")
            lbl.setText("完整自适应文本")
            self.assertEqual(lbl.fullText(), "完整自适应文本")
        finally:
            card.deleteLater()
            lbl.deleteLater()

        healthy = HistoryItemWidget(
            HistoryRecord(
                record_id=1,
                path="ok.wav",
                text="正常音频",
                time="2026-11-14 12:00",
                duration=2.5,
            )
        )
        missing = HistoryItemWidget(
            HistoryRecord(
                record_id=2,
                path="Z:/missing/audio.wav",
                text="缺失音频",
                time="2026-11-14 12:00",
                duration=1.0,
                missing=True,
            )
        )
        try:
            self.assertEqual(healthy.playing_tag.text(), "正在试听")
            self.assertTrue(healthy.playing_tag.isHidden())
            healthy.set_playing(True)
            self.assertFalse(healthy.playing_tag.isHidden())
            healthy.set_playing(False)
            self.assertTrue(healthy.playing_tag.isHidden())

            missing.set_playing(True)
            self.assertTrue(missing.playing_tag.isHidden())
        finally:
            healthy.deleteLater()
            missing.deleteLater()

    def test_studio_proposal_c_log_strip_engine_pill_and_responsive_split(self) -> None:
        from PySide6.QtWidgets import QBoxLayout

        with self._isolated_studio_window() as (window, _tmp):
            self._quiesce(window)
            window.show()
            _app().processEvents()

            # TopBar engine pill and model-directory entry
            self.assertEqual(window.btn_top_models.text().strip(), "模型目录")
            window._set_engine_dot("ok")
            self.assertEqual(window.engine_pill.property("state"), "ok")
            window._set_engine_dot("busy")
            self.assertEqual(window.engine_pill.property("state"), "busy")

            # Collapsed log strip tracks toggle state, receives live preview, and expands
            window.btn_toggle_log.setChecked(False)
            _app().processEvents()
            self.assertFalse(window.log_panel.isVisible())
            self.assertTrue(window.log_collapsed_strip.isVisible())

            window._log("info", "ProposalC日志摘要校验")
            self.assertTrue(window.log_preview_label.fullText().startswith("· 最新："))
            self.assertIn("ProposalC日志摘要校验", window.log_preview_label.fullText())

            window.btn_expand_log.click()
            _app().processEvents()
            self.assertTrue(window.btn_toggle_log.isChecked())
            self.assertTrue(window.log_panel.isVisible())
            self.assertFalse(window.log_collapsed_strip.isVisible())

            # Control card stacks export well above synthesis well at all widths
            window.resize(1240, 800)
            QTest.qWait(80)
            _app().processEvents()
            self.assertEqual(window._ctrl_split.direction(), QBoxLayout.Direction.TopToBottom)
            self.assertGreater(
                window.synth_well.geometry().top(),
                window.out_well.geometry().bottom(),
            )
            self.assertGreaterEqual(window.path_input.width(), 360)
            self.assertGreaterEqual(window.name_input.width(), 420)

            window.model_banner.setVisible(True)
            window.path_input.setText(f"{_tmp}_missing")
            window.resize(1000, 680)
            QTest.qWait(80)
            _app().processEvents()
            self.assertEqual(window._ctrl_split.direction(), QBoxLayout.Direction.TopToBottom)
            self.assertGreater(
                window.synth_well.geometry().top(),
                window.out_well.geometry().bottom(),
            )
            self.assertTrue(window._strip_status.isVisible())
            right_edge = window.btn_generate.mapTo(window, window.btn_generate.rect().topRight()).x()
            self.assertLessEqual(right_edge, window.width())
            btn_bottom = window.btn_generate.mapTo(
                window.ctrl_card,
                window.btn_generate.rect().bottomLeft(),
            ).y()
            self.assertLessEqual(btn_bottom, window.ctrl_card.height())

            # Elided status_label honors setText -> fullText contract
            window.status_label.setText("自定义状态栏提示")
            self.assertEqual(window.status_label.fullText(), "自定义状态栏提示")

            # Direct output directory input + browse + open-folder live in left out_well
            self.assertFalse(hasattr(window, "btn_output_dir"))
            self.assertFalse(hasattr(window, "output_editor"))
            self.assertTrue(window.out_well.isAncestorOf(window.path_input))
            self.assertTrue(window.out_well.isAncestorOf(window.btn_browse))
            self.assertTrue(window.out_well.isAncestorOf(window.btn_open_folder))
            self.assertNotIn(window.btn_open_folder, window._action_widgets)
            self.assertEqual(window.path_input.placeholderText(), "桌面（默认）")
            self.assertGreater(
                window.btn_open_folder.geometry().x(),
                window.btn_browse.geometry().x(),
            )

    def test_setup_view_native_layout_log_pct_pill_and_guide_dialog(self) -> None:
        from mambotts.ui.setup_view import _GuideDialog

        with (
            tempfile.TemporaryDirectory(prefix="mambo_setup_propc_") as tmp,
            mock.patch("mambotts.ui.setup_view.detect_gpu_type", return_value="general"),
            mock.patch(
                "mambotts.ui.setup_view.shutil.disk_usage",
                side_effect=TestSetupView._mock_disk_usage,
            ),
        ):
            view = EmbeddedSetupView(base_dir=tmp)
            dlg = None
            try:
                view.show()
                _app().processEvents()
                view._on_progress(0.5, "正在下载引擎…")
                self.assertEqual(view.percent_label.text(), "50%")
                self.assertEqual(view._log_pct_pill.text(), "50%")

                view._on_install_finished(True, "安装完成")
                self.assertEqual(view.percent_label.text(), "100%")
                self.assertEqual(view._log_pct_pill.text(), "100%")

                dlg = _GuideDialog(tmp, view)
                footer_texts = [btn.text() for btn in dlg.findChildren(QPushButton)]
                self.assertIn("打开 engine_temp/", footer_texts)
                self.assertIn("打开 models/", footer_texts)
                self.assertIn("知道了", footer_texts)
            finally:
                if dlg is not None:
                    dlg.close()
                    dlg.deleteLater()
                view.cancel_and_cleanup()
                view.deleteLater()

    def test_custom_app_icon_rounded_multires_and_window_binding(self) -> None:
        from PySide6.QtCore import QSize
        from mambotts.ui import app_shell

        self.assertTrue(os.path.isfile(app_shell._APP_ICON_PATH))
        icon = app_shell._make_app_icon()
        self.assertFalse(icon.isNull())
        avail = icon.availableSizes()
        for sz in (16, 32, 64, 256):
            self.assertIn(QSize(sz, sz), avail)

        img = icon.pixmap(256, 256).toImage()
        self.assertEqual(img.pixelColor(0, 0).alpha(), 0)
        self.assertEqual(img.pixelColor(128, 128).alpha(), 255)

        with self._isolated_studio_window() as (window, _tmp):
            self._quiesce(window)
            self.assertFalse(window.windowIcon().isNull())
            self.assertIn(QSize(32, 32), window.windowIcon().availableSizes())

        # Missing asset falls back gracefully to programmatic monogram
        with mock.patch.object(app_shell, "_APP_ICON_PATH", os.path.join(os.path.dirname(app_shell._APP_ICON_PATH), "__missing__.png")):
            fallback = app_shell._make_app_icon()
            self.assertFalse(fallback.isNull())
            self.assertIn(QSize(64, 64), fallback.availableSizes())

    def test_egm_studio_and_setup_regressions(self) -> None:
        from mambotts.domain.models import SynthesisJobResult
        from mambotts.ui.setup_view import _GuideDialog

        with self._isolated_studio_window() as (window, tmp):
            self._quiesce(window)
            window.show()
            _app().processEvents()

            # 1. B1: bumping _probe_generation while probe is in flight must never permanently wedge _probe_in_flight.
            window._probe_in_flight = True
            gen_old = window._probe_generation
            window._startup_probe(auto_start=False)
            self.assertFalse(window._probe_in_flight)
            window._probe_in_flight = True
            gen_cur = window._probe_generation
            window._on_heartbeat_result(False, gen=gen_old, url=window._current_api_url())
            self.assertTrue(window._probe_in_flight)  # stale gen did not clear active gen
            window._on_heartbeat_result(True, gen=gen_cur, url=window._current_api_url())
            self.assertFalse(window._probe_in_flight)

            # 2. B2: empty/whitespace _log never crashes on tuple entry.
            window._log("info", "")
            window._log("warn", "   ")
            self.assertTrue(window.btn_open_engine_log is not None)

            # 3. PR #8: native backslash path display & _open_output_folder ignores stale _last_save_path.
            slash_sub = os.path.join(tmp, "custom_out").replace("\\", "/")
            with mock.patch("mambotts.ui.app_shell.QFileDialog.getExistingDirectory", return_value=slash_sub):
                window._browse_output_dir()
            self.assertEqual(window.path_input.text(), os.path.normpath(slash_sub))
            self.assertEqual(window._output_path_display("D:\\"), "D:\\")

            stale_dir = os.path.join(tmp, "stale")
            os.makedirs(stale_dir, exist_ok=True)
            stale_wav = os.path.join(stale_dir, "old.wav")
            with open(stale_wav, "wb") as fh:
                fh.write(b"RIFF")
            window._last_save_path = stale_wav
            opened_urls: list[str] = []
            with mock.patch(
                "mambotts.ui.app_shell.QDesktopServices.openUrl",
                side_effect=lambda u: opened_urls.append(os.path.normpath(u.toLocalFile())),
            ):
                window._open_output_folder()
            self.assertEqual(opened_urls, [os.path.normpath(slash_sub)])

            # 4. _on_generate_done surfaces concise reason instead of bare "合成失败".
            window._on_generate_done(SynthesisJobResult(False, "引擎显存不足\n建议分段"))
            self.assertIn("引擎显存不足", window.status_label.text())

            # 5. Setup view cancel blocks stale progress, GPU text updated, _GuideDialog lists refer.txt as 推荐.
            window.show_setup_view()
            window._on_generate_shortcut()  # must no-op off studio page
            self._quiesce(window)

        from PySide6.QtWidgets import QLabel

        with (
            tempfile.TemporaryDirectory(prefix="mambo_setup_egm_") as tmp,
            mock.patch("mambotts.ui.setup_view.detect_gpu_type", return_value=None),
            mock.patch("mambotts.ui.setup_view.shutil.disk_usage", side_effect=TestSetupView._mock_disk_usage),
        ):
            view = EmbeddedSetupView(base_dir=tmp)
            dlg = None
            try:
                labels = " ".join(lbl.text() for lbl in view.findChildren(QLabel))
                self.assertIn("其他显卡", labels)
                self.assertNotIn("通用 N 卡整合包", labels)
                view._on_cancel()
                view._on_progress(0.88, "滞后进度")
                self.assertIn("正在停止", view.stage_label.text())
                dlg = _GuideDialog(tmp, view)
                dlg_text = " ".join(lbl.text() for lbl in dlg.findChildren(QLabel))
                self.assertIn("推荐", dlg_text)
                self.assertIn("音色可能漂移", dlg_text)
            finally:
                if dlg is not None:
                    dlg.close()
                    dlg.deleteLater()
                view.cancel_and_cleanup()
                view.deleteLater()

    def test_audit_third_round_ui_regressions(self) -> None:
        with self._isolated_studio_window() as (window, tmp):
            self._quiesce(window)

            # 1. _startup_probe(preserve_status=True) keeps "合成失败：..." visible on probe & offline.
            window._on_generate_done(SynthesisJobResult(False, "引擎断开了连接\n详情"))
            self.assertIn("合成失败：引擎断开了连接", window.status_label.text())
            with mock.patch("mambotts.ui.app_shell.threading.Thread"):
                window._startup_probe(auto_start=False, preserve_status=True)
            self.assertIn("合成失败：引擎断开了连接", window.status_label.text())
            window._on_startup_probe(
                False,
                gen=window._probe_generation,
                url=window._current_api_url(),
                auto_start=False,
            )
            self.assertIn("合成失败：引擎断开了连接", window.status_label.text())

            # 2. Canceling on external/CLI engine must not promise "10 秒后可强制重启".
            fake_worker = mock.Mock()
            fake_worker.is_alive.return_value = True
            fake_worker.cancel_event = threading.Event()
            window._synth_worker = fake_worker
            window.supervisor.is_running = mock.Mock(return_value=False)
            window.cancel_synthesis()
            self.assertNotIn("强制重启", window.status_label.text())
            self.assertIn("外部/独立引擎", window.status_label.text())
            window._cancel_wait_timer.stop()
            window._synth_worker = None
            window._cancel_requested = False

            # 3. _validate_output_dir_text handles UNC shares cleanly + missing local drive.
            target_ok, err_ok = window._validate_output_dir_text(tmp)
            self.assertEqual(target_ok, os.path.abspath(tmp))
            self.assertEqual(err_ok, "")

            missing_drv = ""
            for letter in "ZYXWVUTSRQPONMLKJIHGFED":
                cand = f"{letter}:\\"
                if not os.path.exists(cand):
                    missing_drv = cand
                    break
            if missing_drv:
                t_bad, e_bad = window._validate_output_dir_text(os.path.join(missing_drv, "out"))
                self.assertIsNone(t_bad)
                self.assertIn("磁盘或驱动器不存在", e_bad)

            unc = r"\\127.0.0.1\definitely_missing_mambo_share\sub"
            with mock.patch("mambotts.ui.app_shell.os.path.exists", return_value=False):
                t_unc_bad, e_unc_bad = window._validate_output_dir_text(unc)
            self.assertIsNone(t_unc_bad)
            self.assertIn("网络共享路径不可访问", e_unc_bad)

            orig_exists = os.path.exists
            orig_isdir = os.path.isdir

            def fake_unc_exists(p: str) -> bool:
                if str(p).startswith(r"\\127.0.0.1\definitely_missing_mambo_share"):
                    return True
                return orig_exists(p)

            def fake_unc_isdir(p: str) -> bool:
                if str(p).startswith(r"\\127.0.0.1\definitely_missing_mambo_share"):
                    return True
                return orig_isdir(p)

            with (
                mock.patch("mambotts.ui.app_shell.os.path.exists", side_effect=fake_unc_exists),
                mock.patch("mambotts.ui.app_shell.os.path.isdir", side_effect=fake_unc_isdir),
            ):
                t_unc_ok, e_unc_ok = window._validate_output_dir_text(unc)
            self.assertIsNotNone(t_unc_ok)
            self.assertEqual(e_unc_ok, "")

            # 4. Elapsed timer reports queued status clearly on waits >= 8s.
            idle_worker = mock.Mock()
            idle_worker.is_alive.return_value = True
            window._synth_worker = idle_worker
            window._cancel_requested = False
            window._synth_stage_text = "合成中…"
            window.progress_bar.setValue(0)
            with mock.patch("mambotts.ui.app_shell.time.monotonic", return_value=110.0):
                window._synth_started_mono = 100.0
                window._on_synth_elapsed_tick()
            self.assertIn("单路引擎处理/排队中", window.status_label.text())
            self.assertIn("10s", window.status_label.text())
            window._synth_elapsed_timer.stop()
            window._synth_worker = None


if __name__ == "__main__":
    unittest.main()
