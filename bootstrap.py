"""
MamboTTS 启动引导
- 用 tkinter 显示引导窗口（Python 标准库，无需预装）
- 负责: 创建 venv + 安装 PySide6/requests
- 装完后: 启动 launcher.py
"""
import os
import re
import sys
import shutil
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from datetime import datetime

# 与主界面一致的双主题配色。引导期运行在系统 Python 上，
# theme.py 不依赖 PySide6 可安全导入；万一导入失败回退硬编码，引导绝不能因配色崩。
try:
    from theme import apply_theme, THEME as _T
    # 跟随用户持久化的主题偏好（config.json 可能不存在 → 默认深色）
    try:
        import json as _json
        _cfg = _json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "config.json"), encoding="utf-8"))
        if _cfg.get("theme") == "light":
            apply_theme("light")
    except Exception:
        pass
    BG = _T["bg"]; CARD = _T["surface"]; FIELD = _T["surface2"]
    BORDER = _T["border"]; ACCENT = _T["accent"]; TEXT = _T["text"]; MUTED = _T["muted"]
    DIM = _T["dim"]
except Exception:
    # 黑白灰 fallback（与 theme.py 的 Monochrome 一致）
    BG = "#0E0E10"; CARD = "#151517"; FIELD = "#1C1C1F"
    BORDER = "#27272B"; ACCENT = "#F4F4F5"; TEXT = "#F4F4F5"; MUTED = "#A1A1AA"
    DIM = "#6E6E79"


def _project_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _venv_python():
    base = _project_dir()
    return os.path.join(base, ".venv", "Scripts", "python.exe")


def _venv_ready():
    return os.path.exists(_venv_python())


def _python_cmd_if_ok(cmd):
    """校验 cmd 指向的 Python 是否 ≥3.10，是则返回该命令"""
    try:
        out = subprocess.check_output(
            [cmd, "--version"], stderr=subprocess.STDOUT, timeout=5
        ).decode("utf-8", "replace")  # 形如 "Python 3.11.5"
        m = re.search(r"Python\s+(\d+)\.(\d+)", out)
        if m and (int(m.group(1)), int(m.group(2))) >= (3, 10):
            return cmd
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def _system_python():
    """返回可用的系统 Python 命令（≥3.10）。

    不仅验证命令存在，还验证版本：README 宣称最低 3.10，且旧版 PySide6
    根本没有 3.8/3.9 的 wheel——不预检会一路走完 venv 创建，到 pip
    装依赖才报晦涩错误。默认 python/py 版本不够时，再枚举 py 启动器
    管理的多版本（py -0p），避免「装了 3.11 但默认是 3.9」的机器被误判。"""
    for cmd in ("py", "python"):
        ok = _python_cmd_if_ok(cmd)
        if ok:
            return ok
    # py 启动器多版本枚举：行形如 "  -V:3.11 *        C:\Program Files\Python311\python.exe"
    try:
        out = subprocess.check_output(["py", "-0p"], stderr=subprocess.STDOUT,
                                      timeout=5).decode("utf-8", "replace")
        best = None
        for line in out.splitlines():
            # 路径可能含空格，不能只取 \S+：用分隔符切出 " -V:x.y " 之后的部分
            m = re.search(r"-V:(\d+)\.(\d+)[^ ]*\s+(.+)$", line)
            if not m:
                continue
            if (int(m.group(1)), int(m.group(2))) >= (3, 10):
                cand = m.group(3).strip().strip('"')
                if cand.lower().endswith(".exe"):
                    best = cand
                    break  # py -0p 按版本降序列出，第一个即最新
        if best:
            ok = _python_cmd_if_ok(best)
            if ok:
                return ok
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


# ============================================================
# 主引导窗口
# ============================================================

class BootstrapWindow:
    """tkinter 引导窗口，显示 venv + 依赖安装进度"""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("MamboTTS 启动引导")
        self.root.geometry("640x480")
        self.root.minsize(560, 400)

        # 取消状态：工作线程在阶段边界与 pip 输出循环中检查该标志
        self._canceled = False
        self._worker_thread = None
        self._cur_proc = None  # 当前正在运行的子进程（供取消时终止）
        self._proc_lock = threading.Lock()

        # 接管右上角 X：与「取消」按钮同一逻辑
        self.root.protocol("WM_DELETE_WINDOW", self._on_cancel)

        self._build_ui()

    def _build_ui(self):
        self.root.configure(bg=BG)

        # Header（黑白扁平卡片：深灰底 + 白色反白"M"品牌标）
        header = tk.Frame(self.root, bg=CARD, height=60, highlightbackground=BORDER, highlightthickness=1)
        header.pack(fill="x")
        header.pack_propagate(False)
        logo = tk.Label(header, text="M", bg=ACCENT, fg="#0B0B0D",
                        font=("Segoe UI", 15, "bold"), width=3, height=1)
        logo.pack(side="left", padx=(14, 10), pady=12)
        tk.Label(
            header, text="MamboTTS", bg=CARD, fg=TEXT,
            font=("Segoe UI", 15, "bold")
        ).pack(side="left", pady=10)
        tk.Label(
            header, text="正在准备运行环境...", bg=CARD, fg=MUTED,
            font=("Segoe UI", 10)
        ).pack(side="left", padx=8, pady=10, anchor="s")

        # 主体
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=20, pady=16)

        # 当前阶段
        stage_frame = tk.Frame(body, bg=BG)
        stage_frame.pack(fill="x", pady=(0, 8))
        self.stage_label = tk.Label(
            stage_frame, text="准备中...", bg=BG, fg=TEXT,
            font=("Segoe UI", 12, "bold")
        )
        self.stage_label.pack(anchor="w")

        # 进度条
        self.progress = ttk.Progressbar(
            body, orient="horizontal", mode="determinate", length=580, maximum=100
        )
        self.progress.pack(fill="x", pady=(0, 8))

        # 进度数值
        self.progress_label = tk.Label(
            body, text="0%", bg=BG, fg=MUTED,
            font=("Segoe UI", 9)
        )
        self.progress_label.pack(anchor="e")

        # 日志面板
        tk.Label(
            body, text="安装日志:", bg=BG, fg=TEXT,
            font=("Segoe UI", 10)
        ).pack(anchor="w", pady=(8, 4))

        log_frame = tk.Frame(body, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        log_frame.pack(fill="both", expand=True)
        self.log_view = scrolledtext.ScrolledText(
            log_frame, wrap="word", bg=CARD, fg=MUTED, insertbackground=TEXT,
            font=("Consolas", 9), relief="flat", padx=8, pady=6,
            height=10
        )
        self.log_view.pack(fill="both", expand=True)
        # 语义标签（黑白灰：错误白字加粗、警告中灰，不飘红绿）
        self.log_view.tag_configure("err", foreground="#F4F4F5", font=("Consolas", 9, "bold"))
        self.log_view.tag_configure("warn", foreground="#A1A1AA")
        self.log_view.configure(state="disabled")

        # 底部按钮
        btn_frame = tk.Frame(self.root, bg=BG)
        btn_frame.pack(fill="x", padx=20, pady=(0, 16))
        self.cancel_btn = tk.Button(
            btn_frame, text="取消", bg=FIELD, fg=TEXT, activebackground="#26262C",
            font=("Segoe UI", 10), relief="flat", padx=16, pady=4,
            command=self._on_cancel
        )
        self.cancel_btn.pack(side="right")

    # ============================================================
    # 线程安全的 UI 更新
    # ============================================================

    def _run_in_main_thread(self, func, *args):
        """把 UI 更新调度到主线程执行；root 已销毁时静默忽略"""
        try:
            self.root.after(0, lambda: func(*args))
        except (RuntimeError, tk.TclError):
            pass  # 取消/关闭后工作线程迟到的回调，丢弃即可

    def set_stage(self, stage_text):
        self._run_in_main_thread(self.stage_label.configure, {"text": stage_text})

    def set_progress(self, percent):
        """设置整体进度（0-100）"""
        clamped = max(0, min(100, percent))
        self._run_in_main_thread(self.progress.configure, {"value": clamped})
        self._run_in_main_thread(self.progress_label.configure, {"text": f"{clamped:.0f}%"})

    def append_log(self, message):
        """向日志面板追加一行（带时间戳）；错误/警告前缀按语义着色"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}\n"
        if "[错误]" in message or "Error" in message or "error" in message[:30]:
            tag = "err"
        elif message.startswith("[警告]") or "[警告]" in message[:30]:
            tag = "warn"
        else:
            tag = ""
        def _do():
            self.log_view.configure(state="normal")
            self.log_view.insert("end", line, tag)
            self.log_view.see("end")
            self.log_view.configure(state="disabled")
        self._run_in_main_thread(_do)

    def _on_cancel(self):
        """取消引导：置标志 + 终止当前子进程；工作线程检测标志后中止且绝不拉起主程序"""
        if self._worker_thread and self._worker_thread.is_alive():
            if not messagebox.askyesno("确认取消", "确定要取消环境准备吗？\n已创建的虚拟环境会保留。"):
                return
            self._canceled = True
            self.cancel_btn.configure(state="disabled")
            self.append_log("正在取消...（终止进行中的子进程）")
            with self._proc_lock:
                if self._cur_proc and self._cur_proc.poll() is None:
                    try:
                        self._cur_proc.terminate()
                    except OSError:
                        pass
        else:
            self.root.destroy()

    # ============================================================
    # 主流程
    # ============================================================

    def run(self):
        """启动后台工作线程，主线程进入 tk 消息循环"""
        self._worker_thread = threading.Thread(target=self._work, daemon=True)
        self._worker_thread.start()
        self.root.mainloop()

        # mainloop 退出后，根据状态决定
        if self._canceled:
            sys.exit(0)

    def _check_canceled(self):
        """阶段边界统一检查：已取消则直接结束（不拉起主程序）"""
        if self._canceled:
            self.append_log("引导已取消。")
            self._run_in_main_thread(self.root.destroy)
            return True
        return False

    def _work(self):
        """后台工作线程：负责 venv + 依赖安装 + 启动 launcher"""
        try:
            # ===== 阶段 0: 环境检测 =====
            self.set_stage("阶段 1/4: 环境检测")
            self.append_log("开始环境检测...")

            sys_python = _system_python()
            if not sys_python:
                self.append_log("[错误] 未检测到 Python，请先安装 Python 3.10+")
                self.set_stage("错误: 未安装 Python")
                self._show_error_and_exit(
                    "未检测到 Python",
                    "请安装 Python 3.10 或更高版本:\nhttps://www.python.org/downloads/\n\n"
                    "安装时请勾选 'Add Python to PATH'。"
                )
                return

            self.append_log(f"检测到系统 Python: {sys_python}")
            self.set_progress(5)

            if self._check_canceled():
                return

            # 检查 venv 是否已就绪且版本合格：
            # 旧 Python 建的 venv 会让 pip 找不到 PySide6 wheel，报错晦涩；
            # 版本不对直接重建，别把用户往坑里带
            venv_ok = _venv_ready() and _python_cmd_if_ok(_venv_python()) is not None
            if venv_ok:
                self.append_log("虚拟环境已存在且版本合格，跳过创建步骤")
                self.set_progress(35)
            else:
                if _venv_ready():
                    self.append_log("[警告] 现有 .venv 的 Python 版本过旧（<3.10）或已损坏，将删除重建...")
                    shutil.rmtree(os.path.join(_project_dir(), ".venv"), ignore_errors=True)
                # ===== 阶段 1: 创建 venv =====
                self.set_stage("阶段 2/4: 创建虚拟环境")
                self.append_log("正在创建虚拟环境 .venv ...")
                base = _project_dir()
                venv_path = os.path.join(base, ".venv")
                try:
                    # Popen + 轮询取消：venv 创建可能几十秒，阻塞 check_call
                    # 会让"取消"期间界面无反应
                    proc = subprocess.Popen(
                        [sys_python, "-m", "venv", venv_path],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.STDOUT,
                    )
                    with self._proc_lock:
                        self._cur_proc = proc
                    while proc.poll() is None:
                        if self._canceled:
                            if sys.platform == "win32":
                                try:
                                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                   timeout=5)
                                except Exception:
                                    proc.terminate()
                            else:
                                proc.terminate()
                            break
                        time.sleep(0.2)
                    rc = proc.wait()
                    if self._canceled:
                        self.append_log("引导已取消。")
                        self._run_in_main_thread(self.root.destroy)
                        return
                    if rc != 0:
                        raise subprocess.CalledProcessError(rc, [sys_python, "-m", "venv", venv_path])
                except subprocess.CalledProcessError as e:
                    self.append_log(f"[错误] 创建虚拟环境失败: {e}")
                    self._show_error_and_exit(
                        "创建虚拟环境失败",
                        f"可能原因:\n1. 权限不足（请用管理员身份运行）\n2. Python 安装不完整\n\n"
                        f"详细错误: {e}"
                    )
                    return
                finally:
                    with self._proc_lock:
                        self._cur_proc = None
                self.append_log("虚拟环境创建完成")
                self.set_progress(35)

            if self._check_canceled():
                return

            # ===== 阶段 2: 安装依赖 =====
            self.set_stage("阶段 3/4: 安装依赖")
            # 先预下载两个大 wheel（PySide6-Addons 168MB / Essentials 77MB），
            # 用 urllib 流式下载显示实时百分比（pip 自身不吐字节进度）
            venv_python = _venv_python()
            req_path = os.path.join(_project_dir(), "requirements.txt")
            wheelhouse = os.path.join(_project_dir(), "wheelhouse")

            self.append_log("正在准备依赖（PySide6 大包预下载，实时显示进度）...")
            self.append_log("这可能需要 1-3 分钟，取决于带宽...")
            self._prefetch_big_wheels(wheelhouse)
            if self._check_canceled():
                return

            # 进度条转确定模式（预下载已在 35-70% 区间实时走）
            def _to_determinate():
                self.progress.stop()
                self.progress.configure(mode="determinate")
            self._run_in_main_thread(_to_determinate)

            # 先尝试国内镜像
            self.append_log("正在通过国内镜像安装依赖...")
            install_ok = self._pip_install(venv_python, req_path, use_mirror=True)
            if not install_ok:
                self.append_log("[警告] 镜像安装失败，尝试默认源...")
                install_ok = self._pip_install(venv_python, req_path, use_mirror=False)

            if self._check_canceled():
                return

            if not install_ok:
                self._show_error_and_exit(
                    "依赖安装失败",
                    "无法安装 PySide6/requests。\n请检查网络连接后重试。"
                )
                return

            self.append_log("依赖安装完成")
            self.set_progress(90)
            # 预下载缓存使命完成，清理省磁盘
            try:
                shutil.rmtree(wheelhouse, ignore_errors=True)
            except Exception:
                pass

            # ===== 阶段 3: 启动主程序 =====
            self.set_stage("阶段 4/4: 启动主程序")
            self.append_log("正在启动 MamboTTS 主程序...")

            launcher_path = os.path.join(_project_dir(), "launcher.py")
            if not os.path.exists(launcher_path):
                self._show_error_and_exit(
                    "文件缺失",
                    f"未找到 launcher.py:\n{launcher_path}"
                )
                return

            self.set_progress(95)

            # 启动 launcher.py（PySide6 主程序），用 venv 的 python
            # 不阻塞等待：launcher 自己接管 GUI
            if not _spawn_launcher(venv_python, launcher_path):
                self._show_error_and_exit(
                    "主程序启动失败",
                    "launcher.py 启动后立即退出。\n详细错误见 launcher_error.log（位于项目目录）。"
                )
                return
            self.append_log("主程序已启动")

            self.set_progress(100)
            self.set_stage("启动完成")

            # 等待主程序窗口出现（给一点缓冲时间）
            time.sleep(1.5)
            self.append_log("引导完成，即将关闭此窗口...")

            # 退出引导窗口
            self._run_in_main_thread(self.root.destroy)

        except Exception as e:
            self.append_log(f"[错误] 引导过程异常: {type(e).__name__}: {e}")
            self._show_error_and_exit(
                "启动异常",
                f"引导过程发生异常:\n{type(e).__name__}: {e}"
            )

    class _PrefetchCancel(Exception):
        """预下载被用户取消的内部信号（避免吞真实 Ctrl-C）"""

    # PySide6 大 wheel（占依赖体积 95%+）：预下载显示实时进度。
    # 版本与 requirements.txt 的 >= 约束对齐到当前最新；若日后 PyPI 出新版，
    # pip 会忽略本地旧 wheel 走网络（功能正确，仅预下载失效），不会装错。
    _BIG_WHEELS = [("PySide6-Addons", "6.11.2"), ("PySide6-Essentials", "6.11.2")]

    def _prefetch_big_wheels(self, wheel_dir):
        """用 urllib 流式预下载 PySide6 两个大 wheel（约 245MB），
        进度实时更新（MB/百分比/速度）。pip 自身不吐字节级进度。

        结构（muse 审核骨架）：
        1) 先解析全部 meta（url/size/filename/sha256）
        2) 动态算总字节做加权进度
        3) 逐包下载：sha256+size 双校验，节流 200ms 更新 UI
        4) 任何失败/取消都清理 .part 并让 pip 接管，绝不中断安装
        """
        import hashlib
        import json as _json
        import urllib.request as _urlreq
        try:
            os.makedirs(wheel_dir, exist_ok=True)
        except OSError as e:
            self.append_log(f"[警告] 无法创建预下载目录（{e}），交给 pip")
            return True

        # 阶段 A：解析 meta（拿 url/size/真实文件名/sha256）
        metas = []
        arch = "win_amd64"
        py_tag = "cp%d%d" % sys.version_info[:2]
        for pkg, ver in self._BIG_WHEELS:
            if self._canceled:
                return False
            try:
                api = f"https://pypi.org/pypi/{pkg}/{ver}/json"
                req = _urlreq.Request(api, headers={"User-Agent": "MamboTTS-bootstrap/1.0"})
                with _urlreq.urlopen(req, timeout=30) as r:
                    meta = _json.loads(r.read().decode("utf-8"))
                cands = [f for f in meta["urls"]
                         if f["filename"].lower().endswith(arch + ".whl")]
                pick = None
                for f in cands:  # 精确 cpXY-cpXY 优先（文件名形如 pkg-ver-cp311-cp311-win_amd64）
                    fn_low = f["filename"].lower()
                    if f"{py_tag}-{py_tag}" in fn_low or fn_low.startswith(f"{py_tag}-"):
                        pick = f
                        break
                if pick is None:
                    for f in cands:  # 退而求其次 abi3 通包
                        if "abi3" in f["filename"].lower():
                            pick = f
                            break
                if pick is None:
                    self.append_log(f"[警告] 未找到 {pkg} 的 {arch} wheel，交给 pip")
                    continue
                metas.append({
                    "pkg": pkg, "url": pick["url"], "size": pick["size"],
                    "fname": pick["filename"],
                    "sha256": (pick.get("digests") or {}).get("sha256", ""),
                })
            except Exception as e:
                self.append_log(f"[警告] 解析 {pkg} 下载地址失败（{e}），交给 pip")
                continue

        if not metas:
            return True
        total_big = sum(m["size"] for m in metas if m["size"]) or 245_000_000
        base_progress = 35.0
        span_total = 35.0
        acc_span = 0.0
        last_ui = 0.0

        # 阶段 B：逐包流式下载
        for m in metas:
            if self._canceled:
                return False
            size = m["size"]
            target = os.path.join(wheel_dir, m["fname"])
            # 已存在且 size+sha256 全过 → 秒跳（包 try 防 TOCTOU/权限）
            try:
                _exists_ok = size and os.path.exists(target) and os.path.getsize(target) == size
            except OSError:
                _exists_ok = False
            if _exists_ok:
                if m["sha256"]:
                    try:
                        h = hashlib.sha256()
                        with open(target, "rb") as f:
                            for c in iter(lambda: f.read(1 << 20), b""):
                                h.update(c)
                        if h.hexdigest() == m["sha256"]:
                            self.append_log(f"[状态] {m['pkg']} wheel 已存在（校验通过），跳过")
                            acc_span += span_total * size / total_big
                            continue
                    except Exception:
                        pass
                else:
                    self.append_log(f"[状态] {m['pkg']} wheel 已存在，跳过")
                    acc_span += span_total * size / total_big
                    continue
            tmp = target + ".part"
            self.append_log(f"[状态] 预下载 {m['pkg']}"
                             f"（{(size or 0) / (1024*1024):.0f}MB）...")
            done = 0
            t0 = time.time()
            ok = False
            try:
                dreq = _urlreq.Request(m["url"],
                                       headers={"User-Agent": "MamboTTS-bootstrap/1.0"})
                with _urlreq.urlopen(dreq, timeout=60) as resp, open(tmp, "wb") as f:
                    h = hashlib.sha256()
                    while True:
                        if self._canceled:
                            raise self._PrefetchCancel()
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        h.update(chunk)
                        done += len(chunk)
                        now = time.time()
                        if now - last_ui > 0.2 and size:
                            last_ui = now
                            pct = min(done * 100.0 / size, 99.9)
                            speed = done / (1024 * 1024) / max(now - t0, 0.1)
                            span = span_total * size / total_big
                            self.set_progress(base_progress + acc_span + pct / 100.0 * span)
                            self.set_stage(
                                f"阶段 3/4: 安装依赖 · {m['pkg']} "
                                f"{done / (1024*1024):.1f}/{size / (1024*1024):.0f}MB"
                                f"（{pct:.0f}%） 速度 {speed:.1f}MB/s"
                            )
                if size and done != size:
                    raise IOError(f"下载不完整 {done}/{size}")
                if m["sha256"] and h.hexdigest() != m["sha256"]:
                    raise IOError("SHA256 校验失败")
                os.replace(tmp, target)
                ok = True
                acc_span += span_total * size / total_big
                self.append_log(f"[状态] {m['pkg']} 预下载完成（{size / (1024*1024):.0f}MB）")
            except self._PrefetchCancel:
                # 用户取消：清 .part，返回 False → 外层终止安装
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                return False
            except Exception as e:
                self.append_log(f"[警告] {m['pkg']} 预下载失败（{e}），交给 pip")
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        self.set_progress(base_progress + acc_span)
        return True


    def _pip_install(self, venv_python, req_path, use_mirror=True):
        """用 venv python -m pip 安装依赖，实时输出日志，支持取消终止"""
        # 走 python -m pip 而非直接调 pip.exe：pip.exe 包装器损坏或被杀软
        # 拦截是常见故障，此路径不受影响
        wheelhouse = os.path.join(_project_dir(), "wheelhouse")
        has_local = os.path.isdir(wheelhouse) and any(
            f.endswith(".whl") for f in os.listdir(wheelhouse))
        if os.path.exists(req_path):
            cmd = [venv_python, "-m", "pip", "install", "-r", req_path]
        else:
            # requirements.txt 缺失时的显式回退（旧版误把 req_path 传给 -r，必失败）
            cmd = [venv_python, "-m", "pip", "install", "PySide6", "requests"]
        if has_local:
            # 优先用本地预下载的大 wheel（pip 不再重复下载 245MB）
            cmd += ["--find-links", wheelhouse, "--prefer-binary"]
        if use_mirror:
            cmd += ["-i", "https://mirrors.aliyun.com/pypi/simple/"]

        try:
            # pip 强制直连：清掉 FlClash/ghost 注入的代理 env（GHOST_PROXY 等），
            # 否则 pip 会绕道外网代理访问国内镜像，慢且易断
            clean_env = {k: v for k, v in os.environ.items()
                         if "proxy" not in k.lower()}
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=clean_env,
            )
            with self._proc_lock:
                self._cur_proc = proc
            pip_start = time.time()
            cur_pkg = "（解析依赖中...）"
            for line in proc.stdout:
                if self._canceled:
                    # Windows 上 terminate() 只杀 pip 本体；pip 偶发的子进程
                    # （wheel 编译/下载）用进程树清理兜底，避免占用 .venv 文件
                    if sys.platform == "win32":
                        try:
                            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           timeout=5)
                        except Exception:
                            proc.terminate()
                    else:
                        proc.terminate()
                    break
                line = line.rstrip()
                if line:
                    # 解析当前正在下载/安装的包名 → 更新进度提示（不是干等）
                    m = re.search(r"(?:Downloading|Collecting)\s+([A-Za-z0-9_.\-]+)", line)
                    if m:
                        cur_pkg = m.group(1)
                    # 过滤掉过长的进度条行，避免日志爆炸
                    if len(line) < 200:
                        self.append_log(line)
                # 每行都刷新"正在做什么 + 已等多久"（用户知道在动，不会以为卡死）
                elapsed = time.time() - pip_start
                self.set_stage(f"阶段 3/4: 安装依赖 · 正在处理 {cur_pkg}（已等待 {int(elapsed)}s）")
            proc.wait()
            return proc.returncode == 0 and not self._canceled
        except Exception as e:
            self.append_log(f"[错误] pip 执行异常: {e}")
            return False
        finally:
            with self._proc_lock:
                self._cur_proc = None

    def _show_error_and_exit(self, title, message):
        """显示错误对话框并退出"""
        def _do():
            messagebox.showerror(title, message)
            self.root.destroy()
        self._run_in_main_thread(_do)


# ============================================================
# 入口
# ============================================================

def _spawn_launcher(venv_python, launcher_path):
    """启动 launcher.py 并确认其存活 2 秒；崩溃时把输出写入 launcher_error.log。

    返回 True=正常运行；False=立即退出（调用方决定如何提示）。
    """
    err_log = os.path.join(_project_dir(), "launcher_error.log")
    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        with open(err_log, "wb") as errf:
            proc = subprocess.Popen(
                [venv_python, launcher_path],
                stdout=errf,
                stderr=subprocess.STDOUT,
                creationflags=creation_flags,
            )
            time.sleep(2.0)
            return proc.poll() is None
    except Exception:
        return False


def main():
    # 如果 venv 已就绪，跳过 tk 窗口直接启动 launcher.py（避免空白 tk 窗口）
    if _venv_ready():
        if _spawn_launcher(_venv_python(), os.path.join(_project_dir(), "launcher.py")):
            return
        # launcher 秒崩（如依赖被破坏）：引导窗口重装依赖并给出可见错误
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "MamboTTS 启动失败",
            "主程序启动后立即退出，将进入环境修复引导。\n"
            "详细错误见项目目录下 launcher_error.log。"
        )
        root.destroy()

    # 设置 ttk 主题（clam + 黑白灰配色，与主界面统一）
    try:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Horizontal.TProgressbar",
            troughcolor=FIELD, background="#E4E4E7",
            bordercolor=BORDER, lightcolor="#E4E4E7", darkcolor="#E4E4E7",
        )
    except Exception:
        pass

    win = BootstrapWindow()
    win.run()


if __name__ == "__main__":
    main()
