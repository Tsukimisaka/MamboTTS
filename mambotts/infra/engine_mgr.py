"""Local Engine Process Supervisor & 64-Bit Win32 Job Object Binding（spec item 10）。

职责：
- 引擎安装状态 / 预检 / 端口探测等纯函数工具；
- 把 api.py 子进程绑定到 JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE 的 64 位 Job
  Object（内核级「客户端死则引擎陪葬」，防孤儿进程）；
- EngineSupervisor：引擎子进程的独家管理者，busy（合成中）与 online
  （心跳）状态解耦——合成期间挂起 /control 健康轮询，避免单 worker
  Uvicorn 忙于推理时健康检查被拖到超时误报离线。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import datetime
import ipaddress
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import urllib.parse

# PROJECT_ROOT 的单一事实来源是包根 mambotts/__init__.py（spec item 1）
from mambotts import PROJECT_ROOT
from mambotts.domain.models import (
    API_HOST,
    API_PORT,
    DEFAULT_API_URL,
    GPT_MODEL,
    REF_WAV,
    SOVITS_MODEL,
)
from mambotts.domain.voice_repo import builtin_ref_text

from .gsv_client import GSVHttpClient

logger = logging.getLogger("mambotts.infra.engine_mgr")

# 安装完成标记（installer 写入；is_engine_installed 检查）
INSTALL_MARKER = ".mambo_install_ok"
ENGINE_LOG_NAME = "engine.log"
ENGINE_LOG_MAX_BYTES = 2 * 1024 * 1024
_ENGINE_LOG_LOCK = threading.Lock()


def engine_log_path(base_dir: str = PROJECT_ROOT) -> str:
    """Return path to persistent ``engine.log`` in ``base_dir``."""
    return os.path.join(base_dir or PROJECT_ROOT, ENGINE_LOG_NAME)


def _rotate_engine_log_if_needed(log_path: str) -> None:
    """Rotate ``engine.log`` -> ``engine.log.1`` when exceeding 2 MiB."""
    try:
        if os.path.exists(log_path) and os.path.getsize(log_path) >= ENGINE_LOG_MAX_BYTES:
            backup = f"{log_path}.1"
            if os.path.exists(backup):
                os.remove(backup)
            os.replace(log_path, backup)
    except OSError:
        pass


class _NamedSharedLogStream:
    """Thin file-object wrapper keeping a string ``.name`` for wrapped OS descriptors."""

    def __init__(self, stream, path: str) -> None:
        self._stream = stream
        self.name = path

    def fileno(self) -> int:
        return self._stream.fileno()

    @property
    def closed(self) -> bool:
        return self._stream.closed

    def close(self) -> None:
        self._stream.close()


def _open_detached_log_handle(log_path: str):
    """Open ``engine.log`` for child stdout with Win32 ``FILE_SHARE_DELETE`` so logs rotate live."""
    os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
    if sys.platform != "win32":
        return open(log_path, "ab")
    try:
        import msvcrt

        class _SecurityAttributes(ctypes.Structure):
            _fields_ = [
                ("nLength", wintypes.DWORD),
                ("lpSecurityDescriptor", wintypes.LPVOID),
                ("bInheritHandle", wintypes.BOOL),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_SecurityAttributes),
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        k32.CreateFileW.restype = wintypes.HANDLE

        FILE_APPEND_DATA = 0x0004
        SYNCHRONIZE = 0x00100000
        FILE_SHARE_ALL = 0x1 | 0x2 | 0x4  # READ | WRITE | DELETE
        OPEN_ALWAYS = 4
        FILE_ATTRIBUTE_NORMAL = 0x80
        INVALID_HANDLE = ctypes.c_void_p(-1).value

        sa = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), None, 1)
        handle = k32.CreateFileW(
            os.path.abspath(log_path),
            FILE_APPEND_DATA | SYNCHRONIZE,
            FILE_SHARE_ALL,
            ctypes.byref(sa),
            OPEN_ALWAYS,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if not handle or handle == INVALID_HANDLE:
            raise OSError(f"CreateFileW failed err={ctypes.get_last_error()}")
        fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_APPEND)
        raw_stream = os.fdopen(fd, "ab", closefd=True)
        return _NamedSharedLogStream(raw_stream, log_path)
    except Exception:
        return open(log_path, "ab")


def append_engine_log(line: str, base_dir: str = PROJECT_ROOT) -> None:
    """Append a timestamped UTF-8 line to ``engine.log`` (best-effort, thread-safe)."""
    clean = (line or "").rstrip("\r\n")
    if not clean:
        return
    log_path = engine_log_path(base_dir)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _ENGINE_LOG_LOCK:
        try:
            parent = os.path.dirname(os.path.abspath(log_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            _rotate_engine_log_if_needed(log_path)
            with open(log_path, "a", encoding="utf-8", errors="replace") as fh:
                fh.write(f"[{stamp}] {clean}\n")
        except OSError:
            pass

# ANSI 终端转义序列（CSI 颜色 + OSC 窗口标题 + 字符集切换），进 GUI 日志前剥离
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Z0-9]|\x1b[=>]"
)
# tqdm 高频进度刷屏过滤（universal_newlines=True 会把 \r 转为换行；保守匹配避免误杀业务日志）
_TQDM_RE = re.compile(
    r"^\s*(?:\d{1,3}%\|.*|\d+(?:\.\d+)?\s*it/s(?:\s|\W|$).*|\d+\s*it\s*\[.*\])\s*$"
)

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JobObjectExtendedLimitInformation = 9  # JOBOBJECTINFOCLASS 枚举值


# ----------------------------------------------------------------------
# 纯函数工具
# ----------------------------------------------------------------------

def _engine_default_ref_text(ref_wav_path: str) -> str:
    """-dt payload: delegate to shared :func:`builtin_ref_text` next to ref_wav."""
    return builtin_ref_text(os.path.dirname(ref_wav_path))


def engine_paths(base_dir: str = PROJECT_ROOT) -> dict[str, str]:
    """集中返回引擎与模型文件路径；installer 生成 go-api-mambo.bat 时复用
    同一清单，消灭「模型改名要改多处」的隐患。"""
    base_dir = base_dir or PROJECT_ROOT
    gsv_dir = os.path.join(base_dir, "GPT-SoVITS")
    models_dir = os.path.join(base_dir, "models")
    return {
        "base_dir": base_dir,
        "gsv_dir": gsv_dir,
        "models_dir": models_dir,
        "python_exe": os.path.join(gsv_dir, "runtime", "python.exe"),
        "api_py": os.path.join(gsv_dir, "api.py"),
        "sovits": os.path.join(models_dir, SOVITS_MODEL),
        "gpt": os.path.join(models_dir, GPT_MODEL),
        "ref_wav": os.path.join(models_dir, REF_WAV),
    }


def preflight_check(base_dir: str = PROJECT_ROOT) -> list[str]:
    """启动前检查引擎与模型文件完整性，返回缺失项列表（空 = 就绪）。

    避免半安装状态下静默卡 60 秒后报「启动超时」，让人摸不着头脑。"""
    p = engine_paths(base_dir)
    required = [
        ("GPT-SoVITS 整合包目录", p["gsv_dir"]),
        ("引擎入口 api.py", p["api_py"]),
        ("内置 Python runtime", p["python_exe"]),
        ("SoVITS 模型", p["sovits"]),
        ("GPT 模型", p["gpt"]),
        ("参考音频", p["ref_wav"]),
    ]
    missing = [f"{name}: {path}" for name, path in required if not os.path.exists(path)]
    if missing and all(
        "模型" in item or "参考音频" in item for item in missing
    ):
        # Integration bundle is fine; only the mambo voice assets are absent.
        missing.append(
            "提示：缺少的是曼波音色模型文件（非整合包）。请从项目发布页下载 "
            f"{GPT_MODEL} / {SOVITS_MODEL} / {REF_WAV} 并放入 models/ 目录。"
        )
    return missing


def port_in_use(host: str = API_HOST, port: int = API_PORT) -> bool:
    """探测端口是否已被其他进程占用（兼容 IPv4 / IPv6；解析或探测异常返回 False）。"""
    try:
        infos = socket.getaddrinfo(
            host,
            port,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
        )
    except OSError:
        return False
    for af, socktype, proto, _canon, sockaddr in infos:
        try:
            with socket.socket(af, socktype, proto) as s:
                s.settimeout(1.0)
                if s.connect_ex(sockaddr) == 0:
                    return True
        except OSError:
            continue
    return False


def is_engine_installed(base_dir: str = PROJECT_ROOT,
                        required_pkg: str | None = None) -> bool:
    """判定引擎是否已安装：
    - 核心文件存在（api.py + runtime/python.exe）
    - 且满足其一：存在安装完成标记（新安装）/ runtime 内含 torch（老安装兼容）

    设计约束：
    1. 不能把 models/ 纳入判定——整合包不含曼波模型，模型缺失走安装流程
       会形成「装完依旧未安装 → 反复下载 8GB」的死循环；模型缺失由
       preflight_check 在引擎启动阶段报错。
    2. 半途中断的解压可能留下 api.py + python.exe 但缺 torch，
       用标记/torch 双重校验识别「半成品」。
    3. required_pkg 给定时，标记中的 package= 行必须匹配（防 GPU 包类型
       装错后误判已安装）；无 package= 行的旧标记仍视为已安装。
    """
    p = engine_paths(base_dir)
    if not (os.path.exists(p["api_py"]) and os.path.exists(p["python_exe"])):
        return False
    marker_path = os.path.join(p["gsv_dir"], INSTALL_MARKER)
    if os.path.exists(marker_path):
        if required_pkg is None:
            return True
        try:
            with open(marker_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            return True  # 标记不可读：按「文件在即已装」处理，避免误重装
        for line in content.splitlines():
            if line.startswith("package="):
                return line[len("package="):].strip() == required_pkg
        return True  # 标记无 package= 行：老格式，视为已安装
    # 老安装（标记机制引入前完成）：用 runtime 内 torch 目录作为完整性代理
    torch_dir = os.path.join(p["gsv_dir"], "runtime", "Lib", "site-packages", "torch")
    return os.path.isdir(torch_dir)


def validate_http_api_url(api_url: str) -> str | None:
    """校验并规范化 HTTP/HTTPS API 地址；非法或含路径/查询/凭据时返回 None。"""
    if not api_url or not isinstance(api_url, str):
        return None
    raw = api_url.strip()
    if not raw or any(ch.isspace() for ch in raw):
        return None
    try:
        parts = urllib.parse.urlsplit(raw)
        if parts.scheme.lower() not in ("http", "https"):
            return None
        if parts.username is not None or parts.password is not None:
            return None
        if parts.query or parts.fragment or (parts.path not in ("", "/")):
            return None
        host = parts.hostname
        if not host:
            return None
        port = parts.port  # 畸形/越界端口在此抛 ValueError
        if port is not None and not (1 <= port <= 65535):
            return None
    except ValueError:
        return None
    return raw.rstrip("/")


def is_local_api_url(api_url: str) -> bool:
    """校验 API 地址是否指向本机回环（localhost 或 ``ipaddress.is_loopback``）。"""
    normalized = validate_http_api_url(api_url)
    if normalized is None:
        return False
    try:
        parts = urllib.parse.urlsplit(normalized)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _host_port_from_url(api_url: str) -> tuple[str, int]:
    """从 API URL 解析 (host, port)，解析失败回落默认 127.0.0.1:9880。"""
    try:
        parts = urllib.parse.urlsplit(api_url.strip())
        try:
            port = parts.port
        except ValueError:
            port = None
        host = parts.hostname or API_HOST
        if port is not None:
            return host, int(port)
        if parts.scheme.lower() == "https":
            return host, 443
        return host, (API_PORT if is_local_api_url(api_url) else 80)
    except Exception:
        return API_HOST, API_PORT


# ----------------------------------------------------------------------
# 64 位 Win32 Job Object 绑定
# ----------------------------------------------------------------------

if sys.platform == "win32":
    import ctypes.wintypes as wintypes

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        # 字段类型严格对齐 winnt.h 的 x64 布局（c_size_t/c_uint64），
        # x64 上 sizeof(_JOBOBJECT_EXTENDED_LIMIT_INFORMATION) == 144
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_uint64),   # LARGE_INTEGER
            ("PerJobUserTimeLimit", ctypes.c_uint64),       # LARGE_INTEGER
            ("LimitFlags", ctypes.c_uint32),                # DWORD (+4 对齐)
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),        # DWORD (+4 对齐)
            ("Affinity", ctypes.c_size_t),                  # ULONG_PTR
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _KERNEL32.CreateJobObjectW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    _KERNEL32.CreateJobObjectW.restype = wintypes.HANDLE
    _KERNEL32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
    ]
    _KERNEL32.SetInformationJobObject.restype = wintypes.BOOL
    _KERNEL32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _KERNEL32.AssignProcessToJobObject.restype = wintypes.BOOL
    _KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    _KERNEL32.CloseHandle.restype = wintypes.BOOL


def bind_process_to_job_object(proc: subprocess.Popen) -> int | None:
    """把子进程绑定到 KILL_ON_JOB_CLOSE 的 Job Object，返回 job 句柄（int）。

    客户端无论正常退出、崩溃还是被任务管理器强杀，内核都会立即终止
    Job 内的 api.py 及其全部子进程，从 OS 层面保证零孤儿引擎。
    任一步失败（权限/平台差异）都优雅返回 None，不阻断启动。"""
    if sys.platform != "win32":
        return None
    try:
        job = _KERNEL32.CreateJobObjectW(None, None)
        if not job:
            logger.warning("CreateJobObjectW 失败（WinError=%s）",
                           ctypes.get_last_error())
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _KERNEL32.SetInformationJobObject(
                job, _JobObjectExtendedLimitInformation,
                ctypes.byref(info), ctypes.sizeof(info)):
            logger.warning("SetInformationJobObject 失败（WinError=%s，sizeof=%s）",
                           ctypes.get_last_error(),
                           ctypes.sizeof(_JOBOBJECT_EXTENDED_LIMIT_INFORMATION))
            _KERNEL32.CloseHandle(job)
            return None
        if not _KERNEL32.AssignProcessToJobObject(job, wintypes.HANDLE(int(proc._handle))):
            logger.warning("AssignProcessToJobObject 失败（WinError=%s）——"
                           "引擎进程可能在客户端退出后残留", ctypes.get_last_error())
            _KERNEL32.CloseHandle(job)
            return None
        return int(job)
    except Exception as e:  # 任何意外都不应阻断引擎启动
        logger.warning("绑定 Job Object 异常（忽略）: %s", e)
        return None


def _close_job_handle(job: int | None) -> None:
    """关闭 Job Object 句柄（KILL_ON_JOB_CLOSE 生效点之一）。"""
    if not job or sys.platform != "win32":
        return
    try:
        _KERNEL32.CloseHandle(wintypes.HANDLE(int(job)))
    except Exception:
        pass


# ----------------------------------------------------------------------
# EngineSupervisor
# ----------------------------------------------------------------------

class EngineSupervisor:
    """GPT-SoVITS 引擎子进程的独家管理者。

    心跳互锁（spec Invariant 2）：busy（正在合成）与 online（引擎在线）
    是两个独立状态——set_synthesis_busy 永远不碰 online；活跃合成期间
    should_poll_health() 返回 False，调用方据此挂起周期性 /control 探测
    （单 worker Uvicorn 忙于推理时探测会超时误报离线）；online 只由
    「验证过的 HTTP 响应」（mark_online）更新。"""

    def __init__(self, base_dir: str = PROJECT_ROOT, engine_line_cb=None):
        self.base_dir = base_dir or PROJECT_ROOT
        self.engine_line_cb = engine_line_cb
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._job_handle: int | None = None
        self._pump_thread: threading.Thread | None = None
        self._synthesis_busy = False
        self._online = False
        self._stopping = False
        self._bound_api_url: str = ""

    # ---------------- 心跳互锁状态（不碰进程） ----------------

    def set_synthesis_busy(self, busy: bool) -> None:
        """标记合成忙/闲。绝不触碰 online 状态——忙闲只影响健康轮询。"""
        with self._lock:
            self._synthesis_busy = bool(busy)

    def mark_online(self, online: bool = True) -> None:
        """由「验证过的 HTTP 响应」更新引擎在线状态（唯一写入入口）。"""
        with self._lock:
            self._online = bool(online)

    def should_poll_health(self) -> bool:
        """是否允许本轮 /control 健康探测：活跃合成（或正在停止）期间
        返回 False，调用方跳过本轮探测即可，无需改引擎。"""
        with self._lock:
            return (not self._synthesis_busy) and (not self._stopping)

    def is_running(self) -> bool:
        """托管的引擎子进程是否仍存活。"""
        with self._lock:
            proc = self._proc
        return proc is not None and proc.poll() is None

    def bound_api_url(self) -> str:
        """当前托管子进程绑定的 API 地址（未运行或未绑定时为空字符串）。"""
        with self._lock:
            return self._bound_api_url

    def is_running_for(self, api_url: str) -> bool:
        """托管子进程是否正在运行且端口与 ``api_url`` 一致。"""
        if not self.is_running():
            return False
        with self._lock:
            bound = self._bound_api_url
        if not bound:
            return True
        return _host_port_from_url(bound) == _host_port_from_url(api_url)

    # ---------------- 子进程生命周期 ----------------

    def start(self, api_url: str = DEFAULT_API_URL,
              bind_job_object: bool = True) -> tuple[bool, str]:
        """启动引擎子进程（runtime/python.exe -u api.py）。"""
        with self._lifecycle_lock:
            return self._start_locked(api_url=api_url, bind_job_object=bind_job_object)

    def _start_locked(self, api_url: str = DEFAULT_API_URL,
                      bind_job_object: bool = True) -> tuple[bool, str]:
        """在生命周期锁保护下启动引擎子进程，防止与 stop() 并发竞态。"""
        if not is_local_api_url(api_url):
            return False, (f"非本机 API 地址，拒绝启动引擎：{api_url}\n"
                           "引擎只允许绑定在 127.0.0.1 回环地址上。")
        missing = preflight_check(self.base_dir)
        if missing:
            return False, ("引擎文件不完整，无法启动：\n"
                           + "\n".join(f"  - {m}" for m in missing)
                           + "\n请先完成引擎安装。")

        if self.is_running():
            if self.is_running_for(api_url):
                return True, "已有引擎子进程在运行，跳过重复启动。"
            logger.info("目标引擎端口切换 (%s -> %s)，重启托管引擎...",
                        self.bound_api_url(), api_url)
            self._stop_locked()

        try:
            host, port = _host_port_from_url(api_url)
            if port_in_use(host, port):
                client = GSVHttpClient(api_url=api_url)
                if client.is_api_running(api_url=api_url):
                    self.mark_online(True)
                    return True, f"检测到引擎已在线（{api_url}），跳过子进程启动。"
                return False, (f"端口 {host}:{port} 已被其他程序占用，无法启动引擎。\n"
                               "请关闭占用该端口的程序后重试。")
        except OSError as exc:
            return False, f"探测引擎端口失败：{exc}"

        p = engine_paths(self.base_dir)
        cmd = [
            p["python_exe"], "-u", p["api_py"],
            "-a", host, "-p", str(port),
            "-s", p["sovits"], "-g", p["gpt"],
            "-dr", p["ref_wav"], "-dt", _engine_default_ref_text(p["ref_wav"]), "-dl", "zh",
        ]
        # 强制子进程（含其孙进程 api.py，经环境继承）以 UTF-8 无缓冲输出：
        # Windows 默认按 cp936 写管道，父端按 utf-8 解码会把中文日志刷成乱码
        env = {**os.environ,
               "PYTHONUTF8": "1",
               "PYTHONIOENCODING": "utf-8",
               "PYTHONUNBUFFERED": "1",
               "TQDM_DISABLE": "1"}
        creation_flags = 0
        if sys.platform == "win32":
            # 不弹黑窗 + 新进程组（便于按树回收）
            creation_flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        # 分离守护模式（CLI `engine start` / `--auto-start-engine`）下启动方会立即退出：
        # 绝不能使用 PIPE（否则关闭管道后 BrokenPipeError 触发断流），重定向到持久化 engine.log。
        detached = not bind_job_object
        bound_url_clean = (validate_http_api_url(api_url) or api_url).rstrip("/")
        append_engine_log(
            f"=== 启动本地引擎 ({bound_url_clean}, detached={detached}) ===",
            base_dir=self.base_dir,
        )
        try:
            if detached:
                log_path = engine_log_path(self.base_dir)
                _rotate_engine_log_if_needed(log_path)
                log_handle = None
                try:
                    log_handle = _open_detached_log_handle(log_path)
                except OSError:
                    log_handle = None
                try:
                    proc = subprocess.Popen(
                        cmd, cwd=p["gsv_dir"],
                        stdin=subprocess.DEVNULL,
                        stdout=log_handle if log_handle is not None else subprocess.DEVNULL,
                        stderr=subprocess.STDOUT,
                        creationflags=creation_flags,
                        env=env,
                    )
                finally:
                    if log_handle is not None:
                        try:
                            log_handle.close()
                        except OSError:
                            pass
            else:
                proc = subprocess.Popen(
                    cmd, cwd=p["gsv_dir"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=creation_flags,
                    bufsize=1, universal_newlines=True,
                    encoding="utf-8", errors="replace", env=env,
                )
        except OSError as e:
            append_engine_log(f"启动引擎进程失败：{e}", base_dir=self.base_dir)
            return False, f"启动引擎进程失败：{e}"

        with self._lock:
            self._proc = proc
            self._stopping = False
            self._bound_api_url = bound_url_clean
        logger.info("已启动引擎子进程 (PID=%s, url=%s)，等待加载完成...",
                    proc.pid, self._bound_api_url)

        if bind_job_object and sys.platform == "win32":
            job = bind_process_to_job_object(proc)
            if job is None:
                logger.warning("引擎进程未能绑定 Job Object（客户端崩溃时可能残留 api.py）。")
            else:
                with self._lock:
                    # A crashed engine left the old handle behind; close it
                    # before overwriting, or the kernel object leaks per retry.
                    _close_job_handle(self._job_handle)
                    self._job_handle = job

        if proc.stdout is not None:
            self._pump_thread = threading.Thread(
                target=self._pump_stdout, args=(proc,), daemon=True, name="engine-stdout-pump")
            self._pump_thread.start()
        return True, f"引擎进程已启动 (PID={proc.pid})，正在加载模型（约 10-30 秒）..."

    def stop(self) -> None:
        """终止引擎进程树并关闭 Job Object 句柄。幂等且线程安全。"""
        with self._lifecycle_lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        """在生命周期锁保护下终止引擎进程树。"""
        with self._lock:
            proc = self._proc
            self._stopping = True
        if proc is not None:
            pid = proc.pid
            if sys.platform == "win32":
                # Python 持有进程句柄期间 Windows 不会复用该 pid，
                # taskkill 不会误杀；/T 确保孙子进程 api.py 一并回收
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True,
                        timeout=10,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                except (OSError, subprocess.SubprocessError):
                    pass
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        with self._lock:
            job = self._job_handle
            self._job_handle = None
        _close_job_handle(job)
        pump = self._pump_thread
        if pump is not None and pump.is_alive():
            pump.join(timeout=2)
        with self._lock:
            self._proc = None
            self._pump_thread = None
            self._stopping = False
            self._online = False
            self._bound_api_url = ""
        logger.info("引擎进程已停止。")

    # ---------------- stdout 泵 ----------------

    def _pump_stdout(self, proc: subprocess.Popen) -> None:
        """后台线程：逐行读取引擎 stdout（含合并的 stderr），剥离 ANSI
        转义码后回调 engine_line_cb。绝不直接操作 GUI 控件——回调实现方
        自行负责 marshal 回 UI 线程。"""
        stream = proc.stdout
        if stream is None:
            return
        try:
            for line in iter(stream.readline, ""):
                if self._stopping:
                    break
                clean = _ANSI_RE.sub("", line).strip()
                if not clean or _TQDM_RE.match(clean):
                    continue
                self._emit_line(clean)
        except Exception:
            # 读取异常（如进程已退出、管道关闭）静默收尾
            pass
        if not self._stopping:
            rc = proc.poll()
            if rc is None:
                try:
                    rc = proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    rc = proc.poll()
            if rc is not None and rc != 0:
                self._emit_line(f"引擎子进程意外退出（退出码 {rc}），请查看 engine.log 定位。")

    def _emit_line(self, line: str) -> None:
        logger.info("[引擎] %s", line)
        append_engine_log(f"[引擎] {line}", base_dir=self.base_dir)
        cb = self.engine_line_cb
        if cb is not None:
            try:
                cb(line)
            except Exception:
                # 日志回调自身异常不应影响引擎运行
                logger.exception("engine_line_cb 回调异常（已忽略）")
