import os
import sys
import re
import wave
import shutil
import subprocess
import time
import threading
import webbrowser
import requests
from datetime import datetime
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QTextEdit, QPushButton, QLabel,
                             QLineEdit, QFileDialog, QFrame, QProgressBar,
                             QMessageBox, QSlider, QScrollArea, QProgressDialog)
from PySide6.QtCore import Qt, QUrl, Signal, QTimer, QDir
from PySide6.QtGui import QKeySequence, QShortcut, QPixmap, QPainter, QFont, QColor, QIcon
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput

# 导入推理客户端与共用模块（主题/文件命名/配置/历史组件均已解耦）
from inference import TTSClient
from engine_contract import API_PORT
from theme import THEME, base_qss, status_style, log_color, apply_theme, current_theme_name
from file_naming import filename_from_text, sanitize_stem, ensure_wav_suffix, unique_path, resolve_output_dir
from config_store import load as load_config_file, save as save_config_file
import history_store
from history_widget import HistoryItemWidget

# 当前版本号，用于 GitHub 新版本检查
CURRENT_VERSION = "v1.2.1"
GITHUB_REPO = "Tsukimisaka/MamboTTS"

# 配置文件路径：与 app.py 同目录，便于携带
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# ANSI 终端转义序列（CSI 颜色 + OSC 窗口标题等），进入 GUI 前需剥离
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Z0-9]|\x1b[=>]")


class GenerateWorker(threading.Thread):
    """后台合成线程（daemon）：不阻塞 GUI，也不像 QThread 那样存在
    「窗口销毁时线程仍在运行 → QThread: Destroyed while thread is still
    running」的生命周期陷阱——daemon 线程随进程退出自然结束。

    完成结果通过主窗口的 worker_done 信号投递（跨线程 emit 由 Qt 排队到
    主线程执行槽函数），本线程绝不直接触碰控件。
    seq 为任务自增序号：主线程据此丢弃过期回调（防御性，杜绝旧结果
    干扰新一轮合成的 UI/历史）。
    api_url / save_path 均在创建时快照，防止合成期间主线程改 URL 或路径框。"""

    def __init__(self, tts_client, api_url, text, save_path, speed, seq, done_cb):
        super().__init__(daemon=True)
        self.tts_client = tts_client
        self.api_url = api_url
        self.text = text
        self.save_path = save_path
        self.speed = speed
        self.seq = seq
        self.done_cb = done_cb

    def run(self):
        try:
            success, message = self.tts_client.generate_speech(
                self.text, self.save_path, speed=self.speed, api_url=self.api_url
            )
        except Exception as e:
            success, message = False, f"后台合成异常：{type(e).__name__}: {e}"
        try:
            self.done_cb(self.seq, success, message, self.save_path)
        except Exception:
            pass  # 窗口已销毁时结果无处投递，静默丢弃即可


class MamboTTSApp(QMainWindow):
    # 跨线程日志/事件汇聚信号：普通线程只能 emit 信号，
    # 严禁直接触碰控件（QTimer.singleShot 在无事件循环的线程里不会触发）。
    log_line = Signal(str, str)          # (level, message)
    engine_log_line = Signal(str)        # 引擎子进程原始日志行
    update_found = Signal(str, str)      # (latest_tag, release_url)
    api_probe_done = Signal(bool, str)   # (online, mode)
    worker_done = Signal(int, bool, str, str)  # 合成完成 (seq, success, message, save_path)

    def __init__(self):
        super().__init__()
        # TTSClient 初始地址与持久化配置一致：避免任何默认路径探测都打到出厂 127.0.0.1
        # 配置损坏告警先缓冲，UI 建好后打进日志面板（stdout 对 GUI 用户不可见）
        self._config_warnings = []
        config = load_config_file(CONFIG_PATH, warn_cb=self._config_warnings.append)
        self.tts_client = TTSClient(api_url=config["api_url"])
        # 默认输出目录（桌面），会被配置文件覆盖
        default_desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        self.output_dir = default_desktop
        # output_file 仅作为运行期「下次合成目标路径」使用，不再持久化完整路径
        self.output_file = os.path.join(default_desktop, "mambo_output.wav")

        # 读取持久化配置（API URL 已在上方读取并注入 TTSClient）
        self.saved_api_url = config["api_url"]
        self.saved_speed = config["speed"]

        saved_dir = config["output_dir"]
        if saved_dir and os.path.isdir(saved_dir):
            self.output_dir = saved_dir
        # 同步 output_file 到当前目录
        self.output_file = os.path.join(self.output_dir, "mambo_output.wav")

        # 自定义文件命名（留空则用文案前20字），跨次启动保留
        self.saved_name = config["name"]
        # 记住主题偏好，init_ui 前应用（THEME 原地更新，base_qss 用当前值生成）
        self._theme_name = config["theme"]
        # 状态 chip 语义记录（主题切换后按 kind 重刷颜色）
        self._engine_status_kind = "busy"
        self._action_status_kind = "info"

        # 音频播放器初始化
        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        # 播放器与历史记录状态
        self._last_synth_text = ""

        # 引擎子进程引用：随客户端启动/关闭
        self.engine_proc = None
        self._engine_start_time = time.time()
        self._engine_installed = True
        self._engine_timeout_logged = False

        # 状态机字段
        self._worker = None
        self._worker_seq = 0          # 合成任务自增序号：回调过期守卫
        self._probe_thread_busy = False
        self._probe_pending_mode = None
        self._engine_online = False   # 心跳基线：在线后持续低频探测，掉线可感知
        self._engine_spawned = False  # 本次会话是否由我们拉起引擎（区分外部引擎文案）
        self._closing = False         # 关闭流程进行中标记：防 processEvents 派发二次关闭事件重入

        # 信号先连接，保证 UI 建好前到达的日志也能安全排队
        self.log_line.connect(self._on_log_line)
        self.engine_log_line.connect(self._append_engine_log_line)
        self.update_found.connect(self._show_update_dialog)
        self.api_probe_done.connect(self._on_api_probe_done)
        self.worker_done.connect(self.on_generate_finished)

        self.init_ui()
        # 注入日志回调：inference 的日志来自后台线程，只能经信号 marshal 回主线程
        self.tts_client.set_log_handler(
            lambda level, msg: self.log_line.emit(level, msg)
        )
        self.log_line.emit("INFO", "MamboTTS 已启动。如遇问题，请查看此处日志。")
        # 补报启动早期捕获的配置损坏告警
        for w in self._config_warnings:
            self.log_line.emit("WARNING", w)
        self._config_warnings = []

        # 启动引擎子进程（如果引擎没在线）
        self._start_engine_subprocess()

        # 启动 API 在线状态轮询（每 2 秒异步检查一次，直到在线；超时后转 10 秒慢速）
        self._api_check_timer = QTimer(self)
        self._api_check_timer.timeout.connect(self._poll_engine_status)
        self._api_check_timer.start(2000)

        # API URL 输入防抖定时器：用户停止输入 600ms 后才检查在线状态并写配置，
        # 避免每个键击都触发 HTTP 请求 + 写盘导致输入卡顿
        self._api_url_debounce = QTimer(self)
        self._api_url_debounce.setSingleShot(True)
        self._api_url_debounce.timeout.connect(self._on_api_url_debounced)

        self.check_api_status()

        # 恢复上次会话的合成历史（文件仍存在的有效条目）
        self._load_history()

        # 启动 3 秒后后台检查 GitHub 是否有新版本（非阻塞，失败静默）
        QTimer.singleShot(3000, self._check_for_updates_async)

    # ============ 后台线程 → GUI 的唯一通道：信号 ============

    def _on_log_line(self, level, message):
        self.append_log(level, message)

    def _check_for_updates_async(self):
        """在后台线程中检查 GitHub 是否有新版本；结果经信号回到主线程弹窗。"""
        def _check():
            try:
                resp = requests.get(
                    f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                    timeout=5,
                    headers={"Accept": "application/vnd.github.v3+json"}
                )
                if resp.status_code == 200:
                    data = resp.json()
                    latest_tag = data.get("tag_name", "")
                    release_url = data.get("html_url", "")
                    if latest_tag and self._is_newer_version(latest_tag, CURRENT_VERSION):
                        self.update_found.emit(latest_tag, release_url)
            except Exception as e:
                # 静默失败也要留一行线索：用户不至于完全不知道发生过更新检查
                self.log_line.emit("INFO", f"检查更新未完成（{type(e).__name__}），不影响使用。")

        threading.Thread(target=_check, daemon=True).start()

    def _is_newer_version(self, latest, current):
        """比较版本号，如 v1.2.0 > v1.1.0；预发布 tag（v1.3.0-beta 等）
        先剥离 '-' 后缀再比较数字段，避免尾段被 isdigit 过滤导致比较失真。"""
        def parse(v):
            v = v.lstrip('v').strip()
            v = v.split('-')[0].split('+')[0]  # 剥离预发布/构建元数据
            parts = v.split('.')
            return [int(p) for p in parts if p.isdigit()]
        try:
            l = parse(latest)
            c = parse(current)
            while len(l) < len(c):
                l.append(0)
            while len(c) < len(l):
                c.append(0)
            return l > c
        except Exception:
            return False

    def _show_update_dialog(self, latest_tag, release_url):
        """显示新版本更新提示对话框（主线程槽函数）"""
        reply = QMessageBox.question(
            self,
            "发现新版本",
            f"MamboTTS 有新版本可用！\n\n"
            f"当前版本: {CURRENT_VERSION}\n"
            f"最新版本: {latest_tag}\n\n"
            f"是否前往 GitHub 下载新版本？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
        )
        if reply == QMessageBox.Yes and release_url:
            webbrowser.open(release_url)

    # ============ 引擎子进程管理 ============

    def _start_engine_subprocess(self):
        """启动 GPT-SoVITS 引擎子进程（run_engine.py）"""
        # 先检查引擎是否已在线（避免重复启动）；
        # 探测必须指向用户实际配置的地址，而非 TTSClient 构造默认值
        saved_url = self.api_input.text().strip()
        if self.tts_client.is_api_running(api_url=saved_url):
            self.log_line.emit("INFO", "检测到引擎已在线，跳过子进程启动。")
            return

        base_dir = os.path.dirname(os.path.abspath(__file__))
        run_engine_path = os.path.join(base_dir, "run_engine.py")

        if not os.path.exists(run_engine_path):
            self.log_line.emit("WARNING", "未找到 run_engine.py，跳过引擎子进程启动。")
            return

        # 检查 GPT-SoVITS 是否已安装：未安装时立刻明示，而不是等 60 秒误报「启动超时」
        gsv_dir = os.path.join(base_dir, "GPT-SoVITS")
        if not os.path.exists(gsv_dir):
            self._engine_installed = False
            self._engine_timeout_logged = True  # 无需再走超时分支
            self.log_line.emit("ERROR", "GPT-SoVITS 未安装，请先运行 MamboTTS.bat 完成一键安装。")
            self._set_engine_status("error", "✕ 未安装引擎")
            self._set_action_status("error", "请先运行 MamboTTS.bat 安装引擎（约 8GB，一次性）")
            self.btn_generate.setToolTip("请先运行 MamboTTS.bat 安装引擎")
            # 当前窗口内直接给安装入口（launcher 未拦截到的调试路径也能自救）
            self.btn_engine_retry.setText("打开安装引导")
            self.btn_engine_retry.setVisible(True)
            # 注意：_start_engine_subprocess 在 __init__ 中早于定时器创建，
            # 此处绝不能碰 _api_check_timer（未安装态由 _poll_engine_status 自行停轮询）
            return

        try:
            # 用当前 Python 启动 run_engine.py，不在新窗口里跑（避免黑窗）
            # creationflags=CREATE_NO_WINDOW 让 Windows 不弹黑窗
            # 新进程组：便于关闭时按进程树 kill 孙子进程（api.py）
            creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            if sys.platform == "win32":
                creation_flags |= subprocess.CREATE_NEW_PROCESS_GROUP
            self.engine_proc = subprocess.Popen(
                [sys.executable, run_engine_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=creation_flags,
                bufsize=1,
                universal_newlines=True,
                encoding="utf-8",
                errors="replace",
                # 强制子进程（含其孙进程 api.py，经环境继承）以 UTF-8 输出：
                # Windows 默认按 cp936 写管道，父端按 utf-8 解码会把中文日志刷成乱码
                env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            )
            self._engine_spawned = True
            self.log_line.emit("INFO", f"已启动引擎子进程 (PID={self.engine_proc.pid})，等待加载完成...")
            # 后台线程读取引擎 stdout（含 stderr 合并），经信号转发到 GUI 日志面板
            self._engine_log_thread = threading.Thread(
                target=self._pump_engine_log, daemon=True
            )
            self._engine_log_thread.start()
        except Exception as e:
            self.log_line.emit("ERROR", f"启动引擎子进程失败: {e}")

    def _pump_engine_log(self):
        """后台线程：读取引擎子进程 stdout（含合并的 stderr），逐行 emit 信号。

        GPT-SoVITS 的 uvicorn/模型加载日志会输出到这里。
        注意：这里绝对不能直接操作控件——普通 Python 线程没有 Qt 事件循环，
        QTimer.singleShot 的回调永远不会触发（旧版实现在此处静默丢日志）。
        信号跨线程 emit 由 Qt 自动排队到主线程执行槽函数。
        """
        # 启动时局部快照管道句柄：_pump 线程生命周期可能跨过 engine_proc
        # 重新赋值（用户点「重启引擎」），每轮解引用 self 会与新泵线程并发读同一管道
        proc = self.engine_proc
        stream = proc.stdout if proc else None
        if not stream:
            return
        try:
            for line in iter(stream.readline, ''):
                line = line.rstrip()
                if not line:
                    continue
                self.engine_log_line.emit(line)
        except Exception:
            # 读取异常（如进程已退出）静默结束
            pass

    def _append_engine_log_line(self, msg):
        """把引擎子进程的一行日志追加到 GUI 日志面板（主线程槽函数）"""
        msg = _ANSI_RE.sub("", msg)  # 剥离彩色日志的 ANSI 转义码
        level = "INFO"
        upper = msg.upper()
        if "ERROR" in upper or "TRACEBACK" in upper or "EXCEPTION" in upper:
            level = "ERROR"
        elif "WARN" in upper:
            level = "WARNING"
        self.append_log(level, f"[引擎] {msg}")

    # ============ 异步引擎状态探测 ============

    def check_api_status(self):
        """立即异步检查一次 API 状态（startup 模式，结果由轮询上下文解读）"""
        self._start_async_probe("startup")

    def _start_async_probe(self, mode):
        """把 is_api_running 的 HTTP 探测放到后台线程执行，GUI 不阻塞。
        mode: startup(启动期/离线轮询) | heartbeat(在线心跳) | url(用户改地址的一次性检查)

        线程忙时：定时类探测（startup/heartbeat）直接跳过等下个 tick 重试；
        url 探测记录为 pending（当前探测完成后补发最新一次），避免用户改完
        地址恰逢引擎启动轮询在跑，导致新地址永远没被检查过。"""
        if self._probe_thread_busy:
            if mode == "url":
                self._probe_pending_mode = "url"
            return
        self._probe_thread_busy = True
        probe_url = self.api_input.text().strip()
        # 只读探测：不写共享的 tts_client.api_url，避免与合成线程的快照语义混淆
        if mode == "url":
            self._set_engine_status("busy", "◐ 检测中...")

        def _probe():
            try:
                online = self.tts_client.is_api_running(api_url=probe_url) if probe_url else False
            except Exception:
                online = False
            # busy 标志只在主线程槽里复位（见 _on_api_probe_done）：
            # 若在 worker 线程 emit 前复位，新轮询可能与上一结果交错。
            # emit 要包异常：窗口销毁后 C++ 信号对象可能先亡（RuntimeError），
            # 守护线程里的未捕获异常会污染退出日志。
            try:
                self.api_probe_done.emit(online, mode)
            except Exception:
                pass

        threading.Thread(target=_probe, daemon=True).start()

    def _on_api_probe_done(self, online, mode):
        """探测结果回主线程后的统一处理（槽函数）：单一状态出口。

        在线后定时器不停止，转为 10 秒心跳——引擎进程崩溃/被外部关闭时
        UI 不会永远停留在绿灯。离线恢复的判定也全在这里，避免多点写状态。"""
        self._probe_thread_busy = False
        was_online = self._engine_online
        pending = self._probe_pending_mode
        self._probe_pending_mode = None

        if online:
            self._engine_online = True
            self._set_engine_status("ok", "● 引擎在线")
            # 合成进行中不能因心跳恢复而解锁按钮（会与"合成中"禁用冲突）
            if not (self._worker is not None and self._worker.is_alive()):
                self.btn_generate.setEnabled(True)
                self.btn_generate.setToolTip("Ctrl+Enter 快捷合成")
            self.btn_engine_retry.setVisible(False)
            self._api_check_timer.setInterval(10000)
            if not was_online:
                if mode == "startup":
                    elapsed = time.time() - self._engine_start_time
                    if not self._engine_spawned:
                        # 启动时已探测到在线（跳过子进程拉起）：0.0 秒加载是假象，措辞区分
                        self.log_line.emit("INFO", "检测到引擎服务已在线（外部启动）。")
                    else:
                        self.log_line.emit("INFO", f"引擎已在线，本次加载耗时 {elapsed:.1f} 秒。")
                else:
                    self.log_line.emit("INFO", "引擎已恢复在线（心跳检测）。")
                    self._set_action_status("ok", "引擎恢复在线")
            if mode == "url":
                self.append_log("INFO", f"API 服务在线: {self.api_input.text().strip()}")
                self.save_config()
        else:
            self._engine_online = False
            self.btn_generate.setEnabled(False)
            self._api_check_timer.setInterval(5000 if mode != "startup" or self._engine_timeout_logged else 2000)
            if mode == "startup":
                self._handle_startup_offline()
            elif mode == "heartbeat":
                if was_online:
                    self.log_line.emit("WARNING", "检测到引擎掉线（心跳失败），将在后台持续等待恢复...")
                self._set_engine_status("warn", "▲ 引擎离线（等待恢复）")
                self._set_action_status("warn", "引擎离线：请等待引擎恢复后重新点击合成")
            else:  # url
                self._set_engine_status("warn", "▲ 引擎离线")
                current_url = self.api_input.text().strip()
                self.append_log("WARNING", f"API 服务离线: {current_url}")
                # 端口一致性提示：本地引擎固定监听 9880，填其他端口永远探不到
                try:
                    from urllib.parse import urlparse
                    port = urlparse(current_url).port
                    if port is not None and port != API_PORT:
                        self.append_log(
                            "WARNING",
                            f"注意：本机自动启动的引擎固定监听端口 {API_PORT}，"
                            f"当前地址端口 {port} 与之一致性存疑（远程引擎可忽略本提示）。")
                except Exception:
                    pass
                self.save_config()

        # 补发排队的 url 探测
        if pending:
            self._start_async_probe(pending)

    def _handle_startup_offline(self):
        """启动期离线细分：区分「引擎进程已退出」与「仍在加载/超时」。"""
        elapsed = time.time() - self._engine_start_time

        # 引擎子进程提前退出：第一时间报明确错误，而不是让用户干等超时
        if self.engine_proc and self.engine_proc.poll() is not None:
            if not self._engine_timeout_logged:
                self._engine_timeout_logged = True
                self.log_line.emit("ERROR", f"引擎进程已退出 (code={self.engine_proc.returncode})，请查看日志面板定位原因。")
                self._set_engine_status("error", "✕ 引擎启动失败")
                self._set_action_status("error", "引擎进程退出，详见「运行日志」面板")
                self.btn_engine_retry.setVisible(True)
                self._api_check_timer.setInterval(5000)  # 保留轮询，外部手动起引擎时能自动恢复
            return

        # 超时判断：60 秒还没起来先提示，降频继续轮询（慢机器冷启动可能超过 60s）
        if elapsed > 60 and not self._engine_timeout_logged:
            self._engine_timeout_logged = True
            self.log_line.emit("ERROR", "引擎启动超过 60 秒仍未就绪。将继续在后台检测；若模型已就位可稍候，或查看日志排查。")
            self._set_engine_status("error", "✕ 引擎启动超时")
            self._set_action_status("error", "仍在后台检测引擎状态；可查看运行日志排查，或重新启动客户端")
            self.btn_engine_retry.setVisible(True)
            self._api_check_timer.setInterval(5000)
            return

        # 仍在等待中；无子进程可等（run_engine.py 缺失/拉起失败）时明示，
        # 而不是让状态 chip 永远停在「⏳ 检测中...」
        if self.engine_proc is not None:
            self._set_engine_status("busy", f"◐ 引擎启动中... ({elapsed:.0f}s)")
        elif not self._engine_timeout_logged:
            self._engine_timeout_logged = True
            self.log_line.emit("ERROR", "引擎未运行且本地无可用启动器，请通过 MamboTTS.bat 启动或检查 run_engine.py。")
            self._set_engine_status("error", "✕ 引擎启动器缺失")
            self._api_check_timer.setInterval(5000)

    def _poll_engine_status(self):
        """定时器驱动的异步探测：startup 直到在线，在线后转 heartbeat 常驻。"""
        if not self._engine_installed:
            # 未安装态：安装入口由 chip 旁按钮提供，无需持续探测
            self._api_check_timer.stop()
            return
        if self._probe_thread_busy:
            return
        mode = "heartbeat" if self._engine_online else "startup"
        self._start_async_probe(mode)

    def _retry_engine_start(self):
        """引擎异常时的上下文按钮：
        - 已安装：重启引擎子进程（免重启整个客户端）
        - 未安装（仅调试入口可能到达此态）：拉起 launcher 走安装引导"""
        if not self._engine_installed:
            reply = QMessageBox.question(
                self, "打开安装引导",
                "检测到 GPT-SoVITS 未安装，是否现在打开安装引导？\n\n"
                "提示：安装完成后安装引导会自行启动一个主界面窗口，\n"
                "请关闭本窗口避免同时运行两个实例（争用 9880 端口）。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
            )
            if reply != QMessageBox.Yes:
                return
            base_dir = os.path.dirname(os.path.abspath(__file__))
            launcher_path = os.path.join(base_dir, "launcher.py")
            if os.path.exists(launcher_path):
                self.append_log("INFO", "引擎未安装，正在打开安装引导程序...")
                try:
                    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                    subprocess.Popen([sys.executable, launcher_path],
                                     creationflags=creation_flags)
                    return
                except Exception as e:
                    self.append_log("ERROR", f"无法打开安装引导：{e}")
            else:
                self.append_log("ERROR", "未找到 launcher.py，请重新下载整合包或运行 MamboTTS.bat。")
            return
        if self.engine_proc is not None and self.engine_proc.poll() is None:
            self.append_log("INFO", "引擎进程仍在运行，无需重启，继续等待…")
            return
        self.btn_engine_retry.setVisible(False)
        self._engine_timeout_logged = False
        self._engine_start_time = time.time()
        self._set_engine_status("busy", "◐ 正在重启引擎...")
        self._api_check_timer.start(2000)
        self.append_log("INFO", "手动重启引擎子进程。")
        self._start_engine_subprocess()

    # ============ 配置 ============

    def save_config(self):
        """把当前 GUI 状态持久化到 config.json。
        注意：只存输出目录（output_dir），不存完整文件路径，
        避免合成时自动生成的文件名污染用户选择的目录。
        """
        try:
            # 从路径输入框提取目录部分
            current_path = self.path_input.text().strip()
            dir_to_save = os.path.dirname(current_path) or self.output_dir
            data = {
                "api_url": self.api_input.text().strip(),
                "speed": self.speed_slider.value() / 100.0,
                "output_dir": dir_to_save,
                "name": self.name_input.text().strip(),
                "theme": current_theme_name(),
            }
            save_config_file(CONFIG_PATH, data)
        except Exception as e:
            self.append_log("WARNING", f"保存配置失败: {e}")

    # ============ UI ============

    def init_ui(self):
        self.setWindowTitle("MamboTTS - 曼波配音工具")
        self.resize(1280, 800)
        self.setMinimumSize(960, 600)

        # 应用持久化主题（dark/light），THEME 原地更新后 base_qss 生成对应样式
        if self._theme_name not in ("dark", "light"):
            self._theme_name = "dark"
        apply_theme(self._theme_name)
        self.setStyleSheet(base_qss())

        # 主布局：顶部 Header + 下方左右分栏（左控制区 + 右历史记录）
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        root_layout = QVBoxLayout(central_widget)
        root_layout.setContentsMargins(20, 20, 20, 20)
        root_layout.setSpacing(15)

        # 1. 顶部 Header 栏（横跨全宽，扁平深灰卡片，去渐变）
        header_card = QFrame()
        header_card.setObjectName("HeaderCard")
        header_card.setFixedHeight(64)

        header_layout = QHBoxLayout(header_card)
        header_layout.setContentsMargins(16, 10, 16, 10)
        header_layout.setSpacing(12)
        # 左侧 3px 白色竖条：黑白主题下仅有的"品牌高光"
        accent_bar = QFrame()
        accent_bar.setObjectName("AccentBar")
        accent_bar.setFixedWidth(3)
        header_layout.addWidget(accent_bar)
        header_left = QVBoxLayout()
        header_left.setSpacing(2)
        header_title = QLabel("MamboTTS")
        header_title.setObjectName("HeaderTitle")
        # 样式由 base_qss 的 #HeaderTitle 控制（随主题切换）
        header_subtitle = QLabel('本地专属"曼波"文字转语音配音助手')
        header_subtitle.setObjectName("HeaderSub")
        # 样式由 base_qss 的 #HeaderSub 控制（随主题切换）
        header_left.addWidget(header_title)
        header_left.addWidget(header_subtitle)
        header_layout.addLayout(header_left)
        header_layout.addStretch()
        # 主题切换按钮：深色/浅色一键互换（状态持久化到 config）
        self.btn_theme = QPushButton()
        self.btn_theme.setObjectName("ThemeBtn")
        self.btn_theme.setFixedHeight(30)
        self.btn_theme.setCursor(Qt.PointingHandCursor)
        self.btn_theme.clicked.connect(self.toggle_theme)
        self._update_theme_button_text()
        header_layout.addWidget(self.btn_theme, 0, Qt.AlignVCenter)
        root_layout.addWidget(header_card)

        # 2. 内容区域：左右分栏
        content_layout = QHBoxLayout()
        content_layout.setSpacing(15)

        # ===== 左列：主控制区 =====
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(12)

        # 2.1 主卡片（API + 文案 + 语速 + 路径 + 进度）
        main_card = QFrame()
        main_card.setObjectName("MainCard")
        main_card_layout = QVBoxLayout(main_card)
        main_card_layout.setContentsMargins(15, 15, 15, 15)
        main_card_layout.setSpacing(10)

        # API 设置与引擎状态（引擎状态独立成 chip，不被合成消息覆盖）
        api_layout = QHBoxLayout()
        api_label = QLabel("引擎接口:")
        api_label.setFixedWidth(96)
        api_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.api_input = QLineEdit(self.saved_api_url)
        self.api_input.textChanged.connect(self.on_api_url_changed)
        self.engine_status_label = QLabel("◐ 检测中...")
        self.engine_status_label.setMinimumWidth(220)
        self.engine_status_label.setStyleSheet(status_style("busy"))
        # 引擎启动失败/超时后出现的重启入口：对小白免去「重启整个客户端」
        self.btn_engine_retry = QPushButton("重启引擎")
        self.btn_engine_retry.setObjectName("SecondaryBtn")
        self.btn_engine_retry.setFixedHeight(26)
        self.btn_engine_retry.setCursor(Qt.PointingHandCursor)
        self.btn_engine_retry.setVisible(False)
        self.btn_engine_retry.clicked.connect(self._retry_engine_start)
        api_layout.addWidget(api_label)
        api_layout.addWidget(self.api_input)
        api_layout.addWidget(self.engine_status_label)
        api_layout.addWidget(self.btn_engine_retry)
        main_card_layout.addLayout(api_layout)

        # 文案输入：label 定宽与其它行一致，输入区左缘对齐
        text_layout = QHBoxLayout()
        text_layout.setSpacing(10)
        text_label = QLabel("配音文案内容:")
        text_label.setFixedWidth(96)
        text_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        text_layout.addWidget(text_label)
        self.text_input = QTextEdit()
        # 只给占位提示不预填内容：预填会让首次用户误触合成为文案里的示例句
        self.text_input.setPlaceholderText('在此输入想要配音的文字。例如："大家好，我是曼波，今天给大家讲一个非常炸裂的故事……"')
        self.text_input.setAcceptRichText(False)
        self.text_input.setMinimumHeight(100)
        text_layout.addWidget(self.text_input, 1)
        main_card_layout.addLayout(text_layout)

        # 语速控制：0.1x~3.0x 连续滑条（与 README 宣传一致，旧版下拉框只能选 9 档）
        speed_layout = QHBoxLayout()
        speed_label = QLabel("语速调节:")
        speed_label.setFixedWidth(96)
        speed_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.speed_slider = QSlider(Qt.Horizontal)
        self.speed_slider.setRange(10, 300)   # 值 ×100 = 倍速
        self.speed_slider.setSingleStep(5)
        self.speed_slider.setPageStep(25)
        # 主刻度 0.5x + 每档 0.05 细分：鼠标拖动有参照，README 的 1.15x 可精确停靠
        self.speed_slider.setTickPosition(QSlider.TicksBelow)
        # 0.05 一格太密（59 格如梳齿）：主刻度取 0.25 一档，配合右侧数值标签读值
        self.speed_slider.setTickInterval(25)
        saved_speed_val = int(round(max(0.1, min(3.0, self.saved_speed)) * 100))
        self.speed_slider.setValue(saved_speed_val)
        self.speed_value_label = QLabel(f"{saved_speed_val / 100:.2f}x")
        self.speed_value_label.setMinimumWidth(48)
        # 颜色随主题：见 _refresh_theme_inline
        self.speed_slider.valueChanged.connect(self._on_speed_slider_changed)
        speed_layout.addWidget(speed_label)
        speed_layout.addWidget(self.speed_slider, 1)
        speed_layout.addWidget(self.speed_value_label)
        main_card_layout.addLayout(speed_layout)

        # 保存路径（浏览选文件时会把文件名同步到「配音文件命名」，所见即所得）
        path_layout = QHBoxLayout()
        path_label = QLabel("保存路径:")
        path_label.setFixedWidth(96)
        path_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.path_input = QLineEdit(QDir.toNativeSeparators(self.output_file))
        self.btn_browse = QPushButton("浏览...")
        self.btn_browse.setObjectName("SecondaryBtn")
        self.btn_browse.setToolTip("选择保存位置与文件名模板；文件名同时会填入「配音文件命名」。\n"
                                   "若目标文件已存在会自动追加 _1 序号防覆盖")
        self.btn_browse.clicked.connect(self.browse_path)
        path_layout.addWidget(path_label)
        path_layout.addWidget(self.path_input)
        path_layout.addWidget(self.btn_browse)
        main_card_layout.addLayout(path_layout)

        # 配音文件命名：用户可自定义文件名，留空则默认用文案前20字
        name_layout = QHBoxLayout()
        name_label = QLabel("配音文件命名:")
        name_label.setFixedWidth(96)
        name_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.name_input = QLineEdit(self.saved_name)
        self.name_input.setPlaceholderText("留空则默认使用配音文案内容的前20个字（无需填写 .wav 后缀）")
        name_layout.addWidget(name_label)
        name_layout.addWidget(self.name_input, 1)
        main_card_layout.addLayout(name_layout)

        # 合成进度条：空闲时整体隐藏（不留空轨道），有合成任务才出现
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(20)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setVisible(False)
        main_card_layout.addWidget(self.progress_bar)

        # 操作状态行（合成/播放等业务状态，与引擎状态分离）。
        # 初始无内容 → 不套任何样式，避免显示成"空药丸"。
        self.status_label = QLabel("")
        main_card_layout.addWidget(self.status_label)

        self._synth_timer = QTimer(self)
        self._synth_timer.timeout.connect(self._update_synth_progress)
        self._synth_start_time = 0
        self._synth_estimated_sec = 5.0

        # 语速持久化防抖：方向键/点击轨道等改值也落盘（800ms 静止后写）
        self._speed_save_timer = QTimer(self)
        self._speed_save_timer.setSingleShot(True)
        self._speed_save_timer.timeout.connect(self.save_config)

        left_layout.addWidget(main_card)

        # 播放器控制栏：唯一圆形反白播放键 + 时间 + 细滑条
        player_layout = QHBoxLayout()
        self.btn_play_pause = QPushButton("▶")
        self.btn_play_pause.setObjectName("PlayBtn")
        self.btn_play_pause.setFixedSize(40, 40)
        # 播放键样式随主题：见 _refresh_theme_inline
        self.btn_play_pause.setEnabled(False)
        self.btn_play_pause.setToolTip("播放 / 暂停（先合成或选择音频）")
        self.btn_play_pause.clicked.connect(self.toggle_playback)
        player_layout.addWidget(self.btn_play_pause)

        self.time_label = QLabel("00:00 / 00:00")
        # 最小宽度而非固定宽度：高 DPI/字体缩放下时间戳不被截断
        self.time_label.setMinimumWidth(120)
        # 颜色随主题：见 _refresh_theme_inline
        self.time_label.setAlignment(Qt.AlignCenter)
        player_layout.addWidget(self.time_label)

        self.seek_slider = QSlider(Qt.Horizontal)
        self.seek_slider.setRange(0, 1000)
        self.seek_slider.setValue(0)
        self.seek_slider.setEnabled(False)
        self.seek_slider.sliderPressed.connect(self.on_seek_slider_pressed)
        self.seek_slider.sliderMoved.connect(self.on_seek_slider_moved)
        self.seek_slider.sliderReleased.connect(self.on_seek_slider_released)
        player_layout.addWidget(self.seek_slider, 1)
        # 播放器整栏包进容器：空闲（无音频）时整体隐藏，不留空条；有音频后显示
        self.player_bar = QWidget()
        self.player_bar.setLayout(player_layout)
        self.player_bar.setVisible(False)
        left_layout.addWidget(self.player_bar)

        # 操作按钮栏：每屏只允许 1 个白色主按钮（合成配音）
        actions_layout = QHBoxLayout()
        self.btn_generate = QPushButton("合成配音  (Ctrl+Enter)")
        self.btn_generate.setFixedHeight(40)
        # 引擎在线前禁用：与其点击后吃一次失败提示，不如前置引导
        self.btn_generate.setEnabled(False)
        self.btn_generate.setToolTip("等待引擎在线后自动可用")
        self.btn_generate.clicked.connect(self.generate_voice)
        btn_open_folder = QPushButton("打开输出目录")
        btn_open_folder.setObjectName("SecondaryBtn")
        btn_open_folder.setFixedHeight(40)
        btn_open_folder.clicked.connect(self.open_output_folder)
        actions_layout.addWidget(self.btn_generate, 2)
        actions_layout.addWidget(btn_open_folder, 1)
        left_layout.addLayout(actions_layout)

        # Ctrl+Enter 快捷键触发合成（显式 connect；self 持有防 GC）
        # 经由按钮 enabled 状态门控：引擎离线/busy 时快捷键与按钮行为一致，不得绕过
        self._shortcut_generate = QShortcut(QKeySequence("Ctrl+Return"), self)
        self._shortcut_generate.activated.connect(
            lambda: self.btn_generate.isEnabled() and self.generate_voice())

        # 程序化生成窗口图标（免外部资源文件）：多尺寸渲染，高 DPI 下不发虚
        icon = QIcon()
        for px in (16, 24, 32, 48, 64, 128):
            pix = QPixmap(px, px)
            pix.fill(Qt.transparent)
            pr = QPainter(pix)
            pr.setRenderHint(QPainter.Antialiasing)
            pr.setBrush(QColor(THEME["accent"]))
            pr.setPen(Qt.NoPen)
            pr.drawRoundedRect(pix.rect(), px * 0.18, px * 0.18)
            pr.setPen(QColor(THEME["surface2"]))
            pr.setFont(QFont("Segoe UI Emoji", int(px * 0.62)))
            pr.drawText(pix.rect(), Qt.AlignCenter, "M")
            pr.end()
            icon.addPixmap(pix)
        self.setWindowIcon(icon)

        # 运行日志
        log_label = QLabel("运行日志:")
        left_layout.addWidget(log_label)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("运行日志将显示在此处，便于排查问题。")
        # 日志是排障主渠道，60px 太浅；给足一屏可见度
        self.log_view.setMinimumHeight(120)
        self.log_view.setObjectName("LogView")
        # 限制最大行数，长期运行不至于吃爆内存
        self.log_view.document().setMaximumBlockCount(2000)
        left_layout.addWidget(self.log_view)

        content_layout.addWidget(left_widget, 3)

        # ===== 右列：合成历史记录 =====
        right_widget = QWidget()
        # 窗口缩小时历史记录卡片的按钮行不被挤到截断
        right_widget.setMinimumWidth(300)
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        self.history_label = QLabel("合成历史记录")
        # 样式随主题：见 _refresh_theme_inline
        right_layout.addWidget(self.history_label)

        self.history_scroll = QScrollArea()
        self.history_scroll.setWidgetResizable(True)
        self.history_container = QWidget()
        self.history_container.setStyleSheet("background-color: transparent;")
        self.history_container_layout = QVBoxLayout(self.history_container)
        self.history_container_layout.setAlignment(Qt.AlignTop)
        self.history_container_layout.setSpacing(8)
        self.history_container_layout.setContentsMargins(6, 6, 6, 6)
        # 末尾弹性占位：卡片保持内容高度，多余空间被 stretch 吸收，
        # 否则 widgetResizable 会把单卡片拉满整栏（"展开占满右半边"根因）
        self.history_container_layout.addStretch(1)
        self.history_scroll.setWidget(self.history_container)
        right_layout.addWidget(self.history_scroll, 1)
        content_layout.addWidget(right_widget, 2)

        # 空状态占位：常驻控件，有记录时 hide、删空时 show（不销毁，
        # 避免 deleteLater 异步残留导致画面出现幽灵文本）
        self._history_empty_hint = QLabel("暂无合成记录\n合成的配音会按时间倒序出现在这里")
        self._history_empty_hint.setAlignment(Qt.AlignCenter)
        self._history_empty_hint.setStyleSheet(f"color: {THEME['dim']}; font-size: 12px; background: transparent;")
        # hint 插到 stretch 之前（stretch 是最后一项），这样 hint 显示时不被压底
        self.history_container_layout.insertWidget(
            self.history_container_layout.count() - 1, self._history_empty_hint)

        root_layout.addLayout(content_layout, 1)

        # 历史记录数据：[{record_id, path, text, time, duration}, ...]
        self._history_data = []
        self._history_widgets = {}
        self._history_seq = 0  # 自增 record_id，避免路径复用导致记录覆盖
        self._seeking = False
        # 跨启动持久化的历史文件（与 config.json 同目录，便携随包走）
        self._history_path = os.path.join(os.path.dirname(CONFIG_PATH), "history.json")

        # 连接播放器信号
        self.player.positionChanged.connect(self.on_position_changed)
        self.player.durationChanged.connect(self.on_duration_changed)
        self.player.playbackStateChanged.connect(self.on_playback_state_changed)
        self.player.mediaStatusChanged.connect(self.on_media_status_changed)
        # 解码失败/文件损坏要有可见反馈，而不是无声变回播放按钮
        self.player.errorOccurred.connect(self._on_player_error)

        # 应用依赖 THEME 的内联样式（播放键/语速值/时间/历史标题等）
        self._refresh_theme_inline()

    @staticmethod
    def _fmt_mmss(ms):
        s = max(0, int(ms) // 1000)
        return f"{s // 60:02d}:{s % 60:02d}"

    def _on_player_error(self, error):
        # int 比较兼容不同 PySide6 的枚举 scoping（NoError 恒为 0）
        if int(error) == 0:
            return
        self._set_action_status("error", "✕ 播放失败（文件损坏或系统缺少解码器），请重新合成或另选音频")
        self.append_log("ERROR", f"音频播放错误：{error}")
        # 不禁用按钮：用户可点其它历史/重新合成后重试（_play_path 会重新 setSource）
        self.btn_play_pause.setText("▶")
        self.seek_slider.setEnabled(False)

    # ============ 状态栏双通道（引擎 / 操作） ============

    def _set_engine_status(self, kind, text):
        self._engine_status_kind = kind  # 记录语义，主题切换后重刷
        self.engine_status_label.setText(text)
        self.engine_status_label.setStyleSheet(status_style(kind))

    def _set_action_status(self, kind, text):
        self._action_status_kind = kind
        self.status_label.setText(text)
        self.status_label.setStyleSheet(status_style(kind))

    def append_log(self, level, message):
        """把日志追加到 GUI 日志面板。

        语义表达：只有 [ERROR]/[WARNING] 级别标签着色（红/黄），
        正文保持浅灰——避免整行飘红导致大面积刺眼。
        行首符号用 ASCII（! / ! / ·）避免特殊字体缺字变豆腐块。"""
        # 级别标签色 + 行首符号（ASCII，跨字体安全）；色值随主题（深色亮/浅色压深）
        level_style = {
            "ERROR": (THEME["danger"], '!'),
            "WARNING": (THEME["warning"], '!'),
            "INFO": (THEME["dim"], '·'),
        }
        tag_color, prefix = level_style.get(level, (THEME["dim"], '·'))
        timestamp = datetime.now().strftime("%H:%M:%S")
        # 用 HTML 转义避免日志内容里的 < > & 破坏渲染
        safe_msg = (message.replace("&", "&amp;")
                           .replace("<", "&lt;")
                           .replace(">", "&gt;"))
        body_color = THEME["muted"]
        self.log_view.append(
            f'<span style="color:{THEME["dim"]};">{prefix} [{timestamp}] </span>'
            f'<span style="color:{tag_color}; font-weight:bold;">[{level}]</span> '
            f'<span style="color:{body_color};">{safe_msg}</span>'
        )
        # 智能滚底：仅当用户本就在底部时才跟随，上翻查日志不被拽回
        sb = self.log_view.verticalScrollBar()
        if sb.value() >= sb.maximum() - 24:
            sb.setValue(sb.maximum())

    # ============ 输入联动 ============

    def on_api_url_changed(self):
        """URL 输入变化时不立即检查，启动防抖定时器（600ms 无新输入才执行）"""
        self._api_url_debounce.start(600)

    def _on_api_url_debounced(self):
        """防抖到期后：异步检查 API 状态并持久化配置"""
        self._start_async_probe("url")

    def _on_speed_slider_changed(self, value):
        """滑条实时显示倍速值；800ms 静止后统一持久化
        （拖动 / 方向键 / 点击轨道改值都会走 valueChanged，不再只靠 sliderReleased）"""
        self.speed_value_label.setText(f"{value / 100:.2f}x")
        self._speed_save_timer.start(800)

    def browse_path(self):
        file_path, _ = QFileDialog.getSaveFileName(
            self, "选择保存路径", self.output_file, "WAV Files (*.wav)"
        )
        if file_path:
            # 浏览选择文件名：仅当"配音文件命名"为空时才同步为模板，
            # 避免静默覆盖用户手填的自定义命名（用户命名应被尊重）
            if not self.name_input.text().strip():
                stem = os.path.splitext(os.path.basename(file_path))[0]
                self.name_input.setText(stem)
            self.output_file = file_path
            self.output_dir = os.path.dirname(file_path) or self.output_dir
            self.path_input.setText(QDir.toNativeSeparators(file_path))
            self.save_config()
            self.append_log("INFO", f"已选择输出路径: {file_path}")

    # ============ 生命周期 ============

    def closeEvent(self, event):
        """窗口关闭时：停止播放器、停止轮询、等待合成收尾、终止引擎子进程、保存配置"""
        # 重入保护：等待循环里的 processEvents 可能再次派发关闭事件（Alt+F4/任务栏右键），
        # 重入会导致嵌套弹窗、重复 kill 引擎、重复 save_config
        if self._closing:
            event.ignore()
            return
        self._closing = True
        try:
            self._do_close(event)
        finally:
            if not event.isAccepted():
                self._closing = False

    def _do_close(self, event):
        # 先停所有定时器再处理等待：避免等待循环 processEvents 期间
        # 进度/状态文字还在"关闭中"的窗口里刷新，观感像关不掉
        for timer in (getattr(self, "_synth_timer", None),
                      getattr(self, "_api_check_timer", None),
                      getattr(self, "_api_url_debounce", None),
                      getattr(self, "_speed_save_timer", None)):
            if timer is not None:
                timer.stop()

        # 合成进行中：等待或强退二选一。守护线程随进程退出自然收尾，
        # 不再有 QThread 生命周期崩溃问题；等待路径则确保 WAV 完整。
        if self._worker is not None and self._worker.is_alive():
            reply = QMessageBox.question(
                self, "合成进行中",
                "配音仍在合成。\n\n是：等待合成完成后退出（结果完整保留）\n"
                "否：立即强制退出（本次合成结果可能不完整）",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
            )
            if reply == QMessageBox.Yes:
                self.append_log("INFO", "等待合成线程收尾...")
                # 不确定进度对话框 + 「放弃等待」按钮：长时间等待不再是无出口的假死
                progress = QProgressDialog("正在等待合成完成，请稍候...", "放弃等待并强制退出", 0, 0, self)
                progress.setWindowTitle("合成进行中")
                progress.setWindowModality(Qt.ApplicationModal)
                progress.setCancelButtonText("放弃等待并强制退出")
                progress.setMinimumDuration(0)
                progress.setValue(0)
                waited = 0.0
                aborted = False
                # HTTP 读超时封顶 600s + 余量 630s
                while self._worker.is_alive() and waited < 630.0:
                    QApplication.processEvents()
                    time.sleep(0.1)
                    waited += 0.1
                    if progress.wasCanceled():
                        aborted = True
                        break
                progress.close()
                if aborted:
                    self.append_log("WARNING", "已放弃等待，强制退出（本次合成结果可能不完整）。")
                elif self._worker.is_alive():
                    self.append_log("ERROR", "合成等待超时仍未完成，已取消退出，请检查引擎状态。")
                    # 取消退出：恢复被停掉的轮询/进度，窗口继续可用，
                    # 状态灯不能冻结在等待期的最后一帧
                    self._synth_timer.start(200)
                    self._api_check_timer.start()
                    event.ignore()
                    return
            else:
                self.append_log("WARNING", "强制退出：合成线程尚未收尾，本次结果可能不完整。")

        # 停止音频播放器
        if hasattr(self, "player"):
            self.player.stop()
        # 终止引擎子进程（随客户端关闭而关闭）
        # 用 taskkill /T /F 按进程树关闭，确保孙子进程 api.py 也被回收，
        # 避免 9880 端口被孤儿进程占用导致下次启动失败
        if self.engine_proc:
            # 注意：即使根进程已退出也要按树清理——api.py 孙子进程可能还活着占 9880。
            # Python 持有进程句柄期间 Windows 不会复用该 pid，taskkill 不会误杀。
            self.append_log("INFO", "正在关闭引擎子进程...")
            try:
                self._terminate_engine_tree(self.engine_proc.pid)
                # 等待最多 5 秒
                try:
                    self.engine_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # 兜底：非 Windows 平台或 taskkill 失败时直接终止进程树根
                    self.engine_proc.terminate()
                    try:
                        self.engine_proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.engine_proc.kill()
                        self.engine_proc.wait(timeout=2)
                self.append_log("INFO", "引擎子进程已关闭。")
            except Exception as e:
                self.append_log("WARNING",
                                f"关闭引擎子进程异常: {e}。若下次启动报 9880 端口占用，"
                                f"请手动结束残留的 runtime/python.exe 进程。")
            self.engine_proc = None
        # 保存配置
        self.save_config()
        super().closeEvent(event)

    def _terminate_engine_tree(self, pid):
        """按进程树终止引擎子进程及其所有派生进程（含孙子进程 api.py）。

        Windows 用 taskkill /T /F <pid>；非 Windows 由调用方 terminate 兜底。
        静默处理失败，由调用方兜底。
        """
        if sys.platform != "win32":
            return
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except Exception:
            pass

    # ============ 合成流程 ============

    def _update_synth_progress(self):
        """基于已用时间 / 估算时间更新进度条百分比（上限 95%）"""
        if self._synth_estimated_sec <= 0:
            return
        elapsed = time.time() - self._synth_start_time
        pct = min(int(elapsed / self._synth_estimated_sec * 100), 95)
        self.progress_bar.setValue(pct)
        # 估时已到、仍在等引擎收尾时，进度文本明确“仍在进行”并给出超时上限，避免误判卡死
        if elapsed > self._synth_estimated_sec:
            self.progress_bar.setFormat(f"◐ 合成中... ({int(elapsed)}s / 上限 600s)")
        else:
            self.progress_bar.setFormat("%p%")

    def generate_voice(self):
        text = self.text_input.toPlainText().strip()
        if not text:
            self._set_action_status("error", "✕ 请输入配音文案")
            return

        # 防止重复点击：合成进行中再次点击直接忽略。
        # busy 检查必须先于 _last_synth_text 赋值——否则在途任务完成时
        # 历史记录会拿到第二次点击的文案，与实际音频内容不符。
        if self._worker is not None and self._worker.is_alive():
            self.append_log("WARNING", "上一次合成仍在进行中，请稍候。")
            return
        self._last_synth_text = text

        self.btn_generate.setEnabled(False)
        self.btn_generate.setText("◐ 正在合成中...")
        # 合成期间锁定全部输出相关输入：中途改路径/文案会让显示与实际写盘不一致
        self.text_input.setEnabled(False)
        self.name_input.setEnabled(False)
        self.path_input.setEnabled(False)
        self.btn_browse.setEnabled(False)
        self.speed_slider.setEnabled(False)  # 语速同样已快照，改了要下次才生效
        # 变灰要有解释，否则用户不知道为什么不能编辑
        self.text_input.setToolTip("合成期间文案已锁定，完成后自动解锁")
        self.name_input.setToolTip("合成期间命名已锁定，完成后自动解锁")
        # 进度条切换为确定模式，基于文本长度估算合成时间
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%p%")
        self._set_action_status("busy", "◐ 正在合成，请稍候...")

        # 生成文件名：优先使用用户填写的「配音文件命名」，留空则用文案前20字
        out_dir = resolve_output_dir(self.path_input.text(), self.output_dir)
        custom_name = self.name_input.text().strip()
        if custom_name:
            new_filename = ensure_wav_suffix(sanitize_stem(custom_name))
        else:
            new_filename = filename_from_text(text)
        save_path = unique_path(os.path.join(out_dir, new_filename))
        # 自动改名防覆盖要明说：用户浏览时选定的文件名若撞车，不能静默变成 _1
        if os.path.basename(save_path) != new_filename:
            self.append_log("INFO",
                            f"目标文件已存在，自动另存为：{os.path.basename(save_path)}")
        self.path_input.setText(QDir.toNativeSeparators(save_path))
        self.output_file = save_path
        # 同步目录状态，下次启动沿用此目录
        self.output_dir = out_dir
        # 持久化（save_config 只存目录，不存完整文件路径，避免文件名污染配置）
        self.save_config()

        speed_val = self.speed_slider.value() / 100.0
        # 快照当前接口地址：合成期间即使 URL 框被修改，本次请求仍发往点击时的地址
        api_url_snapshot = self.api_input.text().strip()

        # 估算合成时间：约 0.15 秒/字，最低 5 秒，最高 120 秒
        text_len = len(text)
        self._synth_estimated_sec = max(5.0, min(120.0, text_len * 0.15))
        self._synth_start_time = time.time()
        # 每 200ms 更新一次进度条
        self._synth_timer.start(200)

        # 用后台线程执行合成，避免阻塞 GUI；结果经 worker_done 信号回到主线程。
        # 序号守卫：强制退出后旧线程若姗姗来迟 emit，seq 不匹配即丢弃，
        # 不会误恢复新一轮合成的 UI 或用旧结果污染历史。
        self._worker_seq += 1
        self._worker = GenerateWorker(self.tts_client, api_url_snapshot, text, save_path,
                                      speed_val, self._worker_seq, done_cb=self.worker_done.emit)
        self._worker.start()

    def on_generate_finished(self, seq, success, message, save_path):
        """合成线程完成回调：恢复 UI 状态并展示结果（save_path 为线程启动时快照）"""
        # 丢弃过期回调：当前有更新的任务序号（用户强退后又发起了新合成）
        if seq != self._worker_seq:
            self.append_log("INFO", "忽略已过期的合成结果（seq 不匹配）。")
            return
        # 停止进度定时器
        self._synth_timer.stop()
        self.progress_bar.setFormat("%p%")
        # 成功置 100% 后整体隐藏（不留空轨道）；失败保留显示"合成失败"
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100 if success else 0)
        if not success:
            self.progress_bar.setFormat("合成失败")
        else:
            self.progress_bar.setVisible(False)
            self.progress_bar.setTextVisible(False)
        # 恢复按钮要尊重引擎在线状态：若合成期间引擎掉线，心跳可能恰好处于
        # 两次探测间隙，不能无条件解禁
        self.btn_generate.setEnabled(self._engine_online)
        self.btn_generate.setText("合成配音  (Ctrl+Enter)")
        self.text_input.setEnabled(True)
        self.name_input.setEnabled(True)
        self.path_input.setEnabled(True)
        self.btn_browse.setEnabled(True)
        self.speed_slider.setEnabled(True)
        self.text_input.setToolTip("")
        self.name_input.setToolTip("")

        if success:
            self._set_action_status("ok", "✓ 合成成功")
            self.append_log("INFO", message)
            # 合成成功后自动添加到历史记录并启用播放按钮
            self._add_to_history(save_path, self._last_synth_text)
            self.player_bar.setVisible(True)
            self.btn_play_pause.setEnabled(True)
        else:
            # 失败首行摘要直接进状态栏，长日志里翻找原因对新手不友好
            first_line = message.splitlines()[0][:60] if message else "未知错误"
            self._set_action_status("error", f"✕ 合成失败：{first_line}（详见日志）")
            # 把具体的错误原因显示在日志面板，让用户能自助排查
            self.append_log("ERROR", f"合成失败：{message}")

    def play_voice(self):
        """兼容旧调用：播放当前路径的音频"""
        save_path = self.path_input.text().strip()
        if os.path.exists(save_path):
            self._play_path(save_path)
        else:
            self._set_action_status("error", "✕ 未找到音频文件")

    # ============ 播放器控制 ============

    def _play_path(self, path):
        """加载并播放指定路径的音频"""
        if not os.path.exists(path):
            self._set_action_status("error", "✕ 未找到音频文件")
            return
        self.player_bar.setVisible(True)  # 有音频了，播放条出现
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.play()
        self.btn_play_pause.setEnabled(True)
        self.btn_play_pause.setText("❚❚")
        self.seek_slider.setEnabled(True)
        self._set_action_status("ok", "● 正在播放")

    def toggle_playback(self):
        """播放/暂停切换"""
        if self.player.source().isEmpty():
            # 没有加载音频，尝试播放当前路径
            self.play_voice()
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _refresh_theme_inline(self):
        """主题切换后重刷所有"构建时固化了颜色"的内联样式。
        全局 QSS 由 setStyleSheet(base_qss()) 重刷，但这些控件用了
        内联 setStyleSheet（优先级高于 QSS），必须逐个重建。"""
        t = THEME
        # 语速数值（强调色）
        if hasattr(self, "speed_value_label"):
            self.speed_value_label.setStyleSheet(
                f"color: {t['accent']}; font-weight: bold;")
        # 播放键（圆形反白）
        if hasattr(self, "btn_play_pause"):
            self.btn_play_pause.setStyleSheet(
                f"QPushButton {{ background-color: {t['accent']}; color: {t['accent_text']};"
                f" border-radius: 20px; font-size: 15px; font-weight: 700; padding: 0px; border: none; }}"
                f"QPushButton:hover {{ background-color: {t['accent_hover']}; }}"
                f"QPushButton:pressed {{ background-color: {t['accent_pressed']}; }}"
                f"QPushButton:disabled {{ background-color: {t['surface2']}; color: {t['dim']};"
                f" border: 1px solid {t['border_hover']}; }}"
            )
        # 时间标签
        if hasattr(self, "time_label"):
            self.time_label.setStyleSheet(f"color: {t['dim']}; font-size: 12px;")
        # 历史标题 / 空态
        if hasattr(self, "history_label"):
            self.history_label.setStyleSheet(
                f"font-size: 14px; font-weight: bold; color: {t['text']};")
        if self._history_empty_hint is not None:
            self._history_empty_hint.setStyleSheet(
                f"color: {t['dim']}; font-size: 12px; background: transparent;")
        # placeholder 色：QSS 控制不了，需 QPalette（深色浅灰 / 浅色中灰）
        from PySide6.QtGui import QPalette
        ph_color = QColor("#6B7280") if current_theme_name() == "light" else QColor("#A1A1AA")
        for w in (getattr(self, "text_input", None),
                  getattr(self, "name_input", None),
                  getattr(self, "log_view", None)):
            if w is not None:
                pal = w.palette()
                pal.setColor(QPalette.PlaceholderText, ph_color)
                w.setPalette(pal)

    def _update_theme_button_text(self):
        """按钮文字显示"将切换到的主题"（当前深色→显示'浅色'）"""
        cur = current_theme_name()
        self.btn_theme.setText("浅色模式" if cur == "dark" else "深色模式")

    def toggle_theme(self):
        """深/浅主题一键切换：apply_theme 重刷全局 QSS + 内联样式 + 状态 chip + 持久化"""
        new_name = "light" if current_theme_name() == "dark" else "dark"
        apply_theme(new_name)           # THEME 原地更新
        self._theme_name = new_name
        self.setStyleSheet(base_qss())  # 全局 QSS 重刷
        self._refresh_theme_inline()    # 内联样式重刷
        # 状态 chip 按记录的语义 kind 重刷颜色
        if hasattr(self, "engine_status_label"):
            self.engine_status_label.setStyleSheet(status_style(getattr(self, "_engine_status_kind", "info")))
        if hasattr(self, "status_label"):
            self.status_label.setStyleSheet(status_style(getattr(self, "_action_status_kind", "info")))
        self._update_theme_button_text()
        # 历史卡片逐卡重刷内联样式（QSS 对动态子控件背景可能不生效，双保险）
        for wid in list(self._history_widgets.values()):
            try:
                wid.refresh_theme()
            except Exception:
                pass
        # 旧日志行是切主题前用旧色值写的 HTML（颜色固化），浅/深底上会对比错乱：
        # 清空重来，避免误导
        self.log_view.clear()
        self.save_config()              # 统一入口持久化（含 theme）
        self.append_log("INFO", f"已切换到{'浅色' if new_name == 'light' else '深色'}模式。")

    def _reset_player(self):
        """复位播放器 UI 到空闲态：停播、清 source、隐藏播放条、复位进度。
        在删除当前播放文件/播放出错等场景调用，避免残留指向已删文件的 UI。"""
        self._seeking = False
        self.player.stop()
        self.player.setSource(QUrl())
        self.btn_play_pause.setEnabled(False)
        self.btn_play_pause.setText("▶")
        self.seek_slider.setEnabled(False)
        self.seek_slider.setValue(0)
        self.time_label.setText("00:00 / 00:00")
        self.player_bar.setVisible(False)

    def on_playback_state_changed(self, state):
        """播放状态变化时更新按钮文字"""
        if state == QMediaPlayer.PlayingState:
            self.btn_play_pause.setText("❚❚")
        else:
            self.btn_play_pause.setText("▶")

    def on_media_status_changed(self, status):
        """媒体状态变化：以 EndOfMedia 作为自然播完的可靠判据（时序稳定），
        复位进度条并提示播放完毕。"""
        if status == QMediaPlayer.EndOfMedia:
            duration = self.player.duration()
            self.seek_slider.setValue(0)
            self.time_label.setText(f"00:00 / {self._fmt_mmss(duration)}")
            self._set_action_status("ok", "播放完毕")

    def on_position_changed(self, position):
        """播放位置变化时更新进度条和时间标签"""
        duration = self.player.duration()
        self.time_label.setText(f"{self._fmt_mmss(position)} / {self._fmt_mmss(duration)}")
        # 更新进度条（拖动中不自动更新，避免抢滑块）
        if not self._seeking and duration > 0:
            self.seek_slider.setValue(int(position / duration * 1000))

    def on_duration_changed(self, duration):
        """音频总时长变化时更新时间标签"""
        self.time_label.setText(f"{self._fmt_mmss(self.player.position())} / {self._fmt_mmss(duration)}")

    # ---- 进度条拖动：按住实时预览、松手跳转 ----

    def on_seek_slider_pressed(self):
        """按下进度条：进入拖动态，冻结自动更新"""
        self._seeking = True

    def on_seek_slider_moved(self, value):
        """拖动进度条：实时更新时间标签（位置预览），不打断播放"""
        duration = self.player.duration()
        if duration > 0:
            pos_ms = int(value / 1000 * duration)
            self.time_label.setText(f"{self._fmt_mmss(pos_ms)} / {self._fmt_mmss(duration)}")

    def on_seek_slider_released(self):
        """释放进度条：跳转到所选位置并恢复自动更新"""
        duration = self.player.duration()
        if duration > 0:
            position = int(self.seek_slider.value() / 1000 * duration)
            self.player.setPosition(position)
            # 跳转后立刻同步时间标签（等待 positionChanged 可能有一帧延迟）
            self.time_label.setText(f"{self._fmt_mmss(position)} / {self._fmt_mmss(duration)}")
        self._seeking = False

    # ============ 合成历史记录 ============

    def _add_to_history(self, path, text, time_str=None, duration_sec=None,
                        record_id=None, _is_restore=False):
        """合成成功后添加到历史记录（跨启动持久化），最新记录插入最上方。

        time_str / duration_sec / record_id 可显式传入（恢复历史时）。
        会话内同样遵循 MAX_RECORDS 上限：超出的最老记录从列表与界面移除，
        与落盘截断保持一致，避免无限增长的内存/控件泄漏。"""
        if duration_sec is None:
            duration_sec = 0
            try:
                with wave.open(path, 'rb') as wf:
                    frames = wf.getnframes()
                    rate = wf.getframerate()
                    if rate > 0:
                        duration_sec = frames / rate
            except Exception:
                pass
        if time_str is None:
            time_str = datetime.now().strftime("%H:%M:%S")

        # 有记录时隐藏空态占位（常驻控件，show/hide 切换不销毁）
        if self._history_empty_hint is not None:
            self._history_empty_hint.hide()
        # 用自增 record_id 作为唯一标识，避免路径复用导致记录覆盖
        if record_id is None:
            self._history_seq += 1
            record_id = self._history_seq
        else:
            # 恢复历史：沿用持久化的 id，并抬升自增基线避免后续冲突
            if record_id >= self._history_seq:
                self._history_seq = record_id
        record = {"record_id": record_id, "path": path, "text": text, "time": time_str, "duration": duration_sec}
        self._history_data.append(record)

        # 创建历史记录组件并插入到最上方（index 0）；通过回调解耦，不传主窗口
        item_widget = HistoryItemWidget(
            record_id, path, text, time_str, duration_sec,
            on_play=self._play_path,
            on_save_as=self._history_save_as_path,
            on_delete=self._history_delete_path,
        )
        self.history_container_layout.insertWidget(0, item_widget)
        self._history_widgets[record_id] = item_widget
        # 内联样式立即应用（QSS 对刚插入动态卡片的背景渲染在部分平台不可靠）
        item_widget.refresh_theme()

        # 会话内上限：移除最老记录（与 history_store.MAX_RECORDS 对齐）
        while len(self._history_data) > history_store.MAX_RECORDS:
            oldest = self._history_data.pop(0)  # data 按插入序，最老在头
            wid = self._history_widgets.pop(oldest["record_id"], None)
            if wid is not None:
                self.history_container_layout.removeWidget(wid)
                wid.deleteLater()
        # 恢复历史时跳过逐条落盘（启动期避免 N 次全量写盘），由 _load_history 结束后统一落盘
        if not _is_restore:
            self._persist_history()

    def _persist_history(self):
        """把当前历史（最新在前）落盘"""
        records = sorted(self._history_data, key=lambda r: r["record_id"], reverse=True)
        history_store.save(self._history_path, records)

    def _load_history(self):
        """启动时恢复历史记录（仅文件仍在的条目；旧→新依次插入顶部→最终最新在前）。
        结束后统一落盘一次（恢复过程跳过逐条写盘）。"""
        for rec in reversed(history_store.load(self._history_path)):
            self._add_to_history(rec["path"], rec["text"],
                                 time_str=rec["time"], duration_sec=rec["duration"],
                                 record_id=rec["record_id"], _is_restore=True)
        self._persist_history()

    def _history_save_as_path(self, src_path):
        """将指定路径的音频另存为新文件"""
        if not src_path or not os.path.exists(src_path):
            self.append_log("WARNING", "源文件不存在，无法另存为。")
            return
        save_path, _ = QFileDialog.getSaveFileName(
            self, "另存为", src_path, "WAV Files (*.wav)"
        )
        if save_path:
            try:
                shutil.copy2(src_path, save_path)
                self.append_log("INFO", f"已另存为：{save_path}")
            except Exception as e:
                self.append_log("ERROR", f"另存为失败：{e}")

    def _history_delete_path(self, record_id, widget):
        """删除历史记录：询问是否连磁盘文件一并删除，避免歧义"""
        path = widget.path
        box = QMessageBox(self)
        box.setWindowTitle("删除历史记录")
        box.setText(f"如何处理「{os.path.basename(path)}」？")
        btn_del_file = box.addButton("删除文件并移除", QMessageBox.DestructiveRole)
        btn_keep = box.addButton("仅移除记录", QMessageBox.ActionRole)
        btn_cancel = box.addButton(QMessageBox.Cancel)
        box.exec()
        clicked = box.clickedButton()
        # 点 X / ESC 关闭对话框时 clickedButton() 为 None —— 必须当作取消，
        # 否则会落入下方"仅移除记录"分支，用户明明想取消却被移除了记录
        if clicked is None or clicked is btn_cancel:
            return

        if clicked is btn_del_file:
            # 正在播放的文件要先停播并复位播放器 UI，否则残留可点 UI 指向已删文件
            if self.player.source() == QUrl.fromLocalFile(path):
                self._reset_player()
            try:
                if os.path.exists(path):
                    os.remove(path)
                    self.append_log("INFO", f"已删除磁盘文件：{os.path.basename(path)}")
            except Exception as e:
                # 删除失败时保留记录：文件还在，移除记录会让它失去唯一入口
                self.append_log("ERROR", f"删除文件失败，记录已保留：{e}")
                self._set_action_status("error", "删除文件失败（文件被占用？关闭占用程序后重试）")
                return

        self.history_container_layout.removeWidget(widget)
        widget.deleteLater()
        if record_id in self._history_widgets:
            del self._history_widgets[record_id]
        self._history_data = [r for r in self._history_data if r.get("record_id") != record_id]
        self._persist_history()
        # 删空后恢复空态占位（常驻控件直接 show）
        if not self._history_widgets and self._history_empty_hint is not None:
            self._history_empty_hint.show()
        self.append_log("INFO", f"已从历史记录移除：{os.path.basename(path)}")

    def open_output_folder(self):
        save_path = self.path_input.text().strip()
        folder_path = os.path.dirname(save_path)
        if not folder_path:
            self._set_action_status("error", "✕ 保存路径为空，无法打开目录")
            self.append_log("WARNING", "保存路径为空，无法打开目录。")
            return
        if not os.path.exists(folder_path):
            self._set_action_status("error", f"✕ 目录不存在：{folder_path}")
            self.append_log("WARNING", f"目录不存在：{folder_path}")
            return
        try:
            if sys.platform == "win32":
                # 若当前文件已存在，直接让资源管理器选中它，免去长文件名下翻找。
                # 注意 /select, 与路径必须是同一个参数（拆成两个 explorer 不识别）
                if os.path.exists(save_path):
                    subprocess.Popen(
                        ["explorer", f"/select,{os.path.normpath(save_path)}"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                else:
                    os.startfile(folder_path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder_path])
            else:
                subprocess.Popen(["xdg-open", folder_path])
        except Exception as e:
            self._set_action_status("error", "✕ 打开目录失败，详见日志")
            self.append_log("ERROR", f"打开目录失败：{e}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MamboTTSApp()
    window.show()
    sys.exit(app.exec())
