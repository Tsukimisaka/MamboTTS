"""GPU Detector, Multi-Mirror Streaming Downloader & 7-Zip Extractor（spec item 11）。

首次运行的引擎一键安装：GPU 档位探测 → 多源测速（可选手选优选源）→ Range 断点
续传流式下载 GPT-SoVITS v5 整合包 → 大小 + 魔数 + SHA-256 校验 → 7zr 静默解压
（-bsp1 进度）→ 清理旧版残留与扁平化 → 生成启动脚本与安装标记。

关键设计：
- Proxy Immunity：所有下载请求 requests.Session trust_env=False 且逐请求
  显式 proxies={"http": None, "https": None}，本地代理工具无法拦截。
- 换源续传安全：仅当服务端返回明确的偏移语义拒绝（HTTP 416、忽略 Range
  的 200、或 206 总长小于本地断点）时，Pass 2 才允许清除 `.part`；遇到
  网络超时、`RequestException`、`429`、`5xx`、`404` 时绝不误删断点。
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

import requests

# PROJECT_ROOT 的单一事实来源是包根 mambotts/__init__.py（spec item 1）
from mambotts import PROJECT_ROOT
from mambotts.domain.models import (
    DEFAULT_REF_TEXT,
    GPT_MODEL,
    REF_WAV,
    SOVITS_MODEL,
)
from mambotts.domain.storage_repo import remove_quiet, replace_with_retry

from .engine_mgr import INSTALL_MARKER, _engine_default_ref_text, engine_paths

logger = logging.getLogger("mambotts.infra.installer")


class _InstallCancelled(Exception):
    """Internal control-flow signal when user cancels installation mid-step."""


class _DiskWriteError(OSError):
    """Local filesystem open/write failure (not retryable across download mirrors)."""


_INSTALL_DIRS_LOCK = threading.Lock()
_ACTIVE_INSTALL_DIRS: set[str] = set()


@contextlib.contextmanager
def _acquire_install_lock(base_dir: str):
    """Acquire an in-process + cross-process exclusive lock scoped to ``base_dir``."""
    norm_dir = os.path.normcase(os.path.abspath(base_dir or PROJECT_ROOT))
    digest = hashlib.sha1(norm_dir.encode("utf-8", errors="replace")).hexdigest()[:12]
    lock_path = os.path.join(tempfile.gettempdir(), f"mambotts_install_{digest}.lock")

    with _INSTALL_DIRS_LOCK:
        if norm_dir in _ACTIVE_INSTALL_DIRS:
            raise BlockingIOError("in-process install already active")
        _ACTIVE_INSTALL_DIRS.add(norm_dir)

    fh = None
    locked = False
    try:
        try:
            fh = open(lock_path, "a+b")
            fh.seek(0)
            if fh.tell() == 0:
                fh.seek(0, os.SEEK_END)
                if fh.tell() == 0:
                    fh.write(b"0")
                    fh.flush()
            fh.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise BlockingIOError(str(exc)) from exc
        yield
    finally:
        if fh is not None:
            if locked:
                try:
                    fh.seek(0)
                    if sys.platform == "win32":
                        import msvcrt

                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                fh.close()
            except OSError:
                pass
        with _INSTALL_DIRS_LOCK:
            _ACTIVE_INSTALL_DIRS.discard(norm_dir)


def _noop(*args, **kwargs):
    pass


def _false() -> bool:
    return False


_ACTIVE_PROCS_LOCK = threading.Lock()
_ACTIVE_EXTRACT_PROCS: set[subprocess.Popen] = set()
_ACTIVE_IO_LOCK = threading.Lock()
_ACTIVE_SESSIONS: set[requests.Session] = set()
_ACTIVE_RESPONSES: set[requests.Response] = set()


def _register_extract_proc(proc: subprocess.Popen) -> None:
    with _ACTIVE_PROCS_LOCK:
        _ACTIVE_EXTRACT_PROCS.add(proc)


def _unregister_extract_proc(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    with _ACTIVE_PROCS_LOCK:
        _ACTIVE_EXTRACT_PROCS.discard(proc)


def _register_http_io(*, session: requests.Session | None = None,
                      response: requests.Response | None = None) -> None:
    with _ACTIVE_IO_LOCK:
        if session is not None:
            _ACTIVE_SESSIONS.add(session)
        if response is not None:
            _ACTIVE_RESPONSES.add(response)


def _unregister_http_io(*, session: requests.Session | None = None,
                        response: requests.Response | None = None) -> None:
    with _ACTIVE_IO_LOCK:
        if session is not None:
            _ACTIVE_SESSIONS.discard(session)
        if response is not None:
            _ACTIVE_RESPONSES.discard(response)


@contextlib.contextmanager
def _tracked_http_io(*, session: requests.Session | None = None,
                     response: requests.Response | None = None):
    _register_http_io(session=session, response=response)
    try:
        yield
    finally:
        _unregister_http_io(session=session, response=response)


def _abort_response_socket(resp: requests.Response) -> None:
    """Close underlying urllib3 socket/response so blocked reads return immediately."""
    try:
        raw = getattr(resp, "raw", None)
        conn = getattr(raw, "_connection", None)
        sock = getattr(conn, "sock", None)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        fp = getattr(getattr(raw, "_fp", None), "fp", None)
        fp_raw_sock = getattr(getattr(fp, "raw", None), "_sock", None)
        if fp_raw_sock is not None:
            try:
                fp_raw_sock.close()
            except OSError:
                pass
        if raw is not None:
            raw.close()
    except Exception:
        pass
    try:
        resp.close()
    except Exception:
        pass


def abort_active_install_io() -> None:
    """Abort any in-flight installer HTTP requests so cancel/close returns promptly."""
    with _ACTIVE_IO_LOCK:
        responses = list(_ACTIVE_RESPONSES)
        sessions = list(_ACTIVE_SESSIONS)
    for resp in responses:
        _abort_response_socket(resp)
    for sess in sessions:
        try:
            sess.close()
        except Exception:
            pass


def _kill_proc_tree(proc: subprocess.Popen) -> None:
    if proc is None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def terminate_active_install_processes() -> None:
    """立即终止正在运行的 7zr 解压子进程与在途 HTTP 下载（窗口退出清理用）。"""
    abort_active_install_io()
    with _ACTIVE_PROCS_LOCK:
        procs = list(_ACTIVE_EXTRACT_PROCS)
    for proc in procs:
        _kill_proc_tree(proc)


# GPU 型号 → GPT-SoVITS v5 (20261005) 整合包映射
# 源自语雀官方发布页清单（去除百度网盘后的 4 个直链/镜像通道）：
# size_bytes / sha256 取自 ModelScope 仓库 API 权威元数据（同一文件跨源 SHA256 一致）。
GPU_PACKAGES = {
    "nvidia50": {
        "file": "GPT-SoVITS-v5-20261005nvidia50x0.7z",
        "label": "NVIDIA 50 系显卡 (RTX 5070/5080/5090 等)",
        "size_bytes": 10644626883,   # ~9.91 GiB（ModelScope repo API 实测）
        "sha256": "27b269fc1c53823dc9d404ada83f4444b300e455011db9861ec72e01b6fc5d5e",
    },
    "general": {
        "file": "GPT-SoVITS-v5-20261005.7z",
        "label": "其他显卡 (NVIDIA RTX 30/40 系等)",
        "size_bytes": 10869111823,   # ~10.12 GiB（ModelScope repo API 实测）
        "sha256": "2c1cd034af0e76854a8023d0f1fd6156ef2414bcfd5c88aae3309e1263e0fffe",
    },
}

MODELSCOPE_REPO = "FlowerCry/gpt-sovits-7z-pacakges"
HF_PKG_REPO = "lj1995/GPT-SoVITS-windows-package"
DISK_SPACE_FACTOR = 3.6
_SEVEN_Z_MAGIC = b"7z\xbc\xaf\x27\x1c"

BUILTIN_SOURCE_META = [
    {
        "id": "modelscope_direct",
        "name": "ModelScope 直链（推荐 · 多线程满速）",
        "channel": "ModelScope 直链",
        "url_template": "https://www.modelscope.cn/models/" + MODELSCOPE_REPO + "/resolve/master/{file}",
    },
    {
        "id": "modelscope_api",
        "name": "ModelScope API 通道（备用）",
        "channel": "ModelScope API",
        "url_template": "https://modelscope.cn/api/v1/models/" + MODELSCOPE_REPO + "/repo?Revision=master&FilePath={file}",
    },
    {
        "id": "hf_mirror",
        "name": "HF-Mirror 国内镜像（高速下载）",
        "channel": "HF-Mirror",
        "url_template": "https://hf-mirror.com/" + HF_PKG_REPO + "/resolve/main/{file}?download=true",
    },
    {
        "id": "huggingface",
        "name": "HuggingFace 官方（直连 · 需 TUN）",
        "channel": "HF 官方",
        "url_template": "https://huggingface.co/" + HF_PKG_REPO + "/resolve/main/{file}?download=true",
    },
]

# 候选直链源模板（兼容外部调用与单测，不含百度网盘）。
BUILTIN_SOURCES = [entry["url_template"] for entry in BUILTIN_SOURCE_META]

# 安装阶段定义（阶段名, 权重），用于整体进度条计算
STAGES = [
    ("下载整合包", 0.50),    # 多源测速 + Range 续传
    ("校验完整性", 0.05),    # 大小 + SHA256
    ("解压整合包", 0.35),    # 7zr 解压（-bsp1 进度）
    ("收尾配置", 0.10),      # 目录整理 + 启动脚本
]

_PROBE_BYTES = 2 * 1024 * 1024      # 每源测速采样 2MB
_PROBE_TIMEOUT = (6, 15)
_DL_CHUNK = 1024 * 1024             # 下载写盘块 1MB
_NO_PROXY = {"http": None, "https": None}
_EXTRACT_TIMEOUT_SEC = 1800         # 7zr 解压硬超时（30 分钟）
_BAD_BINARY_CTYPES = ("text/html", "application/json", "application/xml", "text/xml")

_PCT_RE = re.compile(r"(\d{1,3})%")


# ============================================================
# GPU 探测 / 镜像清单
# ============================================================

def detect_gpu_type() -> str | None:
    """用 nvidia-smi 探测本机 NVIDIA 显卡，返回建议的整合包类型。

    命中 RTX 50 系 → 'nvidia50'，其余 NVIDIA → 'general'；
    探测失败（无 nvidia-smi/无 A 卡等）返回 None，交由默认值兜底。"""
    try:
        kwargs: dict = {"stderr": subprocess.DEVNULL, "timeout": 6}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            **kwargs,
        ).decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return None
    if re.search(r"RTX\s*50\d0|50\d0\s+Ti|B\d\d\d", out, re.IGNORECASE):
        return "nvidia50"
    if "NVIDIA" in out or "RTX" in out or "GTX" in out:
        return "general"
    return None


def _is_valid_http_mirror_url(candidate: str) -> bool:
    if not candidate or any(ch.isspace() for ch in candidate):
        return False
    try:
        parts = urllib.parse.urlsplit(candidate)
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.netloc)


def load_mirror_urls(file_name, base_dir):
    """读取 mirrors.txt（应用根目录）中的自定义镜像。

    每行一个 URL 模板，{file} 会替换为整合包文件名，# 开头为注释。
    用途：内置源整体失效时，用户可自行添加可用镜像自救，无需改代码。"""
    path = os.path.join(base_dir, "mirrors.txt")
    urls = []
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                candidate = line.replace("{file}", file_name).strip()
                if not _is_valid_http_mirror_url(candidate):
                    logger.warning("忽略 mirrors.txt 非法镜像地址: %s", candidate[:80])
                    continue
                urls.append(candidate)
    except FileNotFoundError:
        pass
    except OSError:
        pass
    return urls


def describe_sources(gpu_pkg: dict, base_dir: str = PROJECT_ROOT) -> list[dict[str, str]]:
    """返回完整可展示与测速的下载源列表（mirrors.txt 自定义源置顶 + 内置 4 源）。"""
    fname = gpu_pkg["file"]
    by_url = {
        meta["url_template"].replace("{file}", fname): meta
        for meta in BUILTIN_SOURCE_META
    }
    described: list[dict[str, str]] = []
    seen: set[str] = set()
    for custom_idx, url in enumerate(load_mirror_urls(fname, base_dir), start=1):
        if url in seen:
            continue
        seen.add(url)
        meta = by_url.get(url)
        if meta is not None:
            described.append({
                "id": meta["id"],
                "name": meta["name"],
                "channel": meta["channel"],
                "url": url,
            })
        else:
            described.append({
                "id": f"custom_{custom_idx}",
                "name": f"自定义镜像 #{custom_idx}（mirrors.txt）",
                "channel": "mirrors.txt",
                "url": url,
            })
    for meta in BUILTIN_SOURCE_META:
        url = meta["url_template"].replace("{file}", fname)
        if url in seen:
            continue
        seen.add(url)
        described.append({
            "id": meta["id"],
            "name": meta["name"],
            "channel": meta["channel"],
            "url": url,
        })
    return described


def build_source_urls(gpu_pkg, base_dir):
    """候选直链列表：用户自定义镜像优先（往往是用户确认可用的源），
    随后是内置四源。实际顺序稍后由测速重排。"""
    return [entry["url"] for entry in describe_sources(gpu_pkg, base_dir)]


def classify_local_cache(gpu_pkg: dict, base_dir: str = PROJECT_ROOT) -> tuple[str, int]:
    """Inspect ``engine_temp/`` for ``gpu_pkg`` without double-counting ``.7z`` and ``.part``.

    Returns ``(state, usable_bytes)`` where state is one of
    ``"complete"``, ``"unverified"``, ``"partial"``, ``"none"``.
    """
    base_dir = base_dir or PROJECT_ROOT
    target_dir = os.path.join(base_dir, "engine_temp")
    target_file = gpu_pkg["file"]
    expected = int(gpu_pkg["size_bytes"])
    src_7z = os.path.join(target_dir, target_file)
    part_path = src_7z + ".part"
    try:
        if os.path.isfile(src_7z):
            size = os.path.getsize(src_7z)
            if size == expected and _is_valid_7z_header(src_7z):
                return "complete", size
            return "unverified", min(size, expected)
    except OSError:
        pass
    try:
        if os.path.isfile(part_path):
            part_size = os.path.getsize(part_path)
            return "partial", min(part_size, expected)
    except OSError:
        pass
    return "none", 0


def estimate_disk_requirement(gpu_pkg: dict, base_dir: str = PROJECT_ROOT) -> tuple[int, int]:
    """统一估算安装所需净空闲字节，仅扣除 ``engine_temp/`` 中当前目标包或断点缓存。

    返回 ``(required_bytes, matched_cache_bytes)``。
    """
    _state, matched_cache = classify_local_cache(gpu_pkg, base_dir)
    gross = int(gpu_pkg["size_bytes"] * DISK_SPACE_FACTOR)
    return max(0, gross - matched_cache), matched_cache


def _is_valid_7z_header(file_path: str) -> bool:
    """校验文件前 6 字节是否为 7z 魔数，防止外部预分配全零文件误跳下载。"""
    try:
        with open(file_path, "rb") as f:
            return f.read(len(_SEVEN_Z_MAGIC)) == _SEVEN_Z_MAGIC
    except OSError:
        return False


def _sanitize_bat_arg(text: str) -> str:
    """清理/转义批处理双引号参数中的控制字符、尾随反斜杠与 cmd 元字符。"""
    no_ctrl = "".join(
        ch if (ord(ch) >= 32 and ord(ch) != 127) else " "
        for ch in (text or "")
    )
    single_line = " ".join(no_ctrl.split())
    # 双引号内部仍会展开 %VAR%，且未闭合引号/尾随反斜杠会破坏引号转义
    cleaned = single_line.replace('"', "").replace("&", " ").replace("|", " ")
    cleaned = cleaned.replace("^", " ").replace("<", " ").replace(">", " ")
    cleaned = " ".join(cleaned.split()).rstrip("\\").strip()
    if not cleaned:
        cleaned = DEFAULT_REF_TEXT
    return cleaned.replace("%", "%%")


# ============================================================
# 多源测速
# ============================================================

def probe_source_metrics(
    url: str,
    expected_bytes: int = 0,
    cancel_cb=None,
) -> tuple[float | None, int | None]:
    """单源测速与可达性检测：返回 ``(throughput_bps, ttfb_ms)``。

    过滤 HTML/JSON/XML 错误页，并在响应携带 Content-Range / Content-Length
    时核验包体一致；测速吞吐自收到响应头起以 ``time.monotonic()`` 精确计时。
    """
    try:
        if cancel_cb is not None and cancel_cb():
            return None, None
        with requests.Session() as session:
            session.trust_env = False  # 直连测速，免疫环境/注册表代理
            _register_http_io(session=session)
            try:
                t_start = time.monotonic()
                with session.get(
                    url,
                    headers={"Range": f"bytes=0-{_PROBE_BYTES - 1}", "Accept-Encoding": "identity"},
                    stream=True,
                    timeout=_PROBE_TIMEOUT,
                    proxies=_NO_PROXY,
                ) as r:
                    _register_http_io(response=r)
                    try:
                        t_body = time.monotonic()
                        ttfb_ms = max(1, int(round((t_body - t_start) * 1000)))
                        if r.status_code not in (200, 206):
                            return None, None
                        ctype = (r.headers.get("Content-Type") or "").lower()
                        if any(bad in ctype for bad in _BAD_BINARY_CTYPES):
                            return None, None
                        if r.status_code == 206 and expected_bytes > 0:
                            cr = r.headers.get("Content-Range", "")
                            total_str = cr.rsplit("/", 1)[-1].strip()
                            if total_str.isdigit() and int(total_str) != expected_bytes:
                                return None, None
                        elif r.status_code == 200 and expected_bytes > 0:
                            cl_str = (r.headers.get("Content-Length") or "").strip()
                            if cl_str.isdigit() and int(cl_str) != expected_bytes:
                                return None, None
                        got = 0
                        for chunk in r.iter_content(chunk_size=65536):
                            if cancel_cb is not None and cancel_cb():
                                return None, None
                            if chunk:
                                got += len(chunk)
                                if got >= _PROBE_BYTES:
                                    break
                    finally:
                        _unregister_http_io(response=r)
                dt = max(time.monotonic() - t_body, 1e-6)
                if got <= 0:
                    return None, None
                return got / dt, ttfb_ms
            finally:
                _unregister_http_io(session=session)
    except Exception:
        return None, None


def benchmark_sources(urls, log_cb, cancel_cb=None, expected_bytes: int = 0):
    """并发（最多 4 路）测速，返回按速度降序的 URL 列表（不可达垫底保留原序）。"""
    log = log_cb or _noop
    if not urls:
        return []
    results: dict[int, tuple[float | None, int | None]] = {}
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=min(4, max(1, len(urls))),
        thread_name_prefix="src-probe",
    )
    try:
        futures = {
            pool.submit(probe_source_metrics, u, expected_bytes, cancel_cb): i
            for i, u in enumerate(urls)
        }
        pending = set(futures.keys())
        while pending:
            if cancel_cb is not None and cancel_cb():
                for fut in pending:
                    fut.cancel()
                log("[提示] 测速已取消，按默认顺序尝试下载。")
                return list(urls)
            done, pending = concurrent.futures.wait(pending, timeout=0.2)
            for fut in done:
                idx = futures[fut]
                try:
                    results[idx] = fut.result()
                except Exception:
                    results[idx] = (None, None)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    scored = [(i, results.get(i, (None, None))) for i in range(len(urls))]
    ok = sorted(
        ((bps, ttfb, i) for i, (bps, ttfb) in scored if bps),
        key=lambda item: (item[0], -(item[1] or 99999)),
        reverse=True,
    )
    dead = [urls[i] for i, (bps, _ttfb) in scored if not bps]
    for rank, (bps, ttfb, i) in enumerate(ok, 1):
        lat = f" · {ttfb}ms" if ttfb is not None else ""
        log(f"[测速] 第{rank}名 ≈{bps / (1024 * 1024):.1f}MB/s{lat}  {urls[i][:88]}")
    for u in dead:
        log(f"[测速] 不可达，暂排除：{u[:88]}")
    if not ok:
        log("[警告] 所有源测速失败（网络波动或无法连通），将按默认顺序尝试直连...")
        return list(urls)
    return [urls[i] for _, _, i in ok] + dead


# ============================================================
# 下载 / 校验
# ============================================================

def compute_sha256(file_path, progress_cb=None, stage_base=0.0, stage_weight=0.0, cancel_cb=None):
    """分块（8MB）计算大文件的 SHA256，支持进度与协作式取消回调。"""
    h = hashlib.sha256()
    file_size = os.path.getsize(file_path)
    read = 0
    with open(file_path, "rb") as f:
        while True:
            if cancel_cb is not None and cancel_cb():
                raise _InstallCancelled("已在文件校验期间取消，下载缓存已保留。")
            chunk = f.read(8 * 1024 * 1024)  # 8MB 块
            if not chunk:
                break
            h.update(chunk)
            read += len(chunk)
            if progress_cb and file_size > 0 and stage_weight > 0:
                pct = stage_base + (read / file_size) * stage_weight
                progress_cb(pct)
    return h.hexdigest()


# Pinned 7zr.exe standalone CLI release (verified against official 7-zip.org download).
SEVEN_Z_VERSION = "26.04"
SEVEN_Z_SHA256 = "256feca8e274e5da655e2a284fabafd9f554365eb164862089dacd4e8276d282"


def _file_sha256_hex(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest().lower()


def _valid_pe_tool(path):
    """7zr.exe 体检：大小 >= 100KB、MZ 魔数头，且严格匹配固定的 SHA-256。"""
    try:
        if os.path.getsize(path) < 100 * 1024:
            return False
        with open(path, "rb") as f:
            if f.read(2) != b"MZ":
                return False
        return _file_sha256_hex(path) == SEVEN_Z_SHA256
    except OSError:
        return False


def _sleep_cancelable(seconds: float, cancel_cb) -> bool:
    """Sleep up to ``seconds`` in short steps; return True if cancelled."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if cancel_cb is not None and cancel_cb():
            return True
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
    return bool(cancel_cb is not None and cancel_cb())


def download_7z_tool(target_path, log_cb=None, cancel_cb=None):
    """下载 7zr.exe 解压工具，执行大小 + MZ 魔数 + 固定 SHA-256 校验与协作取消。

    官方站点对国内网络偶发抖动，重试 3 次；仍失败或 SHA-256 不符时给出明确提示
    并抛 RuntimeError。"""
    log = log_cb or (lambda m: print(m))
    if _valid_pe_tool(target_path):
        return
    if cancel_cb is not None and cancel_cb():
        raise _InstallCancelled("已取消获取解压工具。")
    url = "https://www.7-zip.org/a/7zr.exe"
    log("[状态] 正在获取 7z 解压工具 (约 590KB)...")

    last_err = None
    part_path = target_path + ".part"
    with requests.Session() as session:
        session.trust_env = False
        _register_http_io(session=session)
        try:
            for attempt in range(1, 4):
                if cancel_cb is not None and cancel_cb():
                    remove_quiet(part_path)
                    raise _InstallCancelled("已取消获取解压工具。")
                try:
                    with session.get(url, stream=True, timeout=20, proxies=_NO_PROXY) as r:
                        _register_http_io(response=r)
                        try:
                            r.raise_for_status()
                            with open(part_path, "wb") as f:
                                for chunk in r.iter_content(chunk_size=8192):
                                    if cancel_cb is not None and cancel_cb():
                                        raise _InstallCancelled("已取消获取解压工具。")
                                    if chunk:
                                        f.write(chunk)
                        finally:
                            _unregister_http_io(response=r)
                    # 完整性与供应链安全校验：PE 魔数 + 固定官方 SHA-256
                    if os.path.getsize(part_path) < 100 * 1024:
                        raise OSError(
                            f"下载内容异常（大小 {os.path.getsize(part_path)} 字节），疑似错误页"
                        )
                    with open(part_path, "rb") as pf:
                        if pf.read(2) != b"MZ":
                            raise OSError("下载内容缺少 PE 文件头（MZ），疑似网页/错误页")
                    actual_sha = _file_sha256_hex(part_path)
                    if actual_sha != SEVEN_Z_SHA256:
                        raise OSError(
                            f"7zr.exe SHA-256 校验未通过（预期 {SEVEN_Z_VERSION}: {SEVEN_Z_SHA256[:16]}...，"
                            f"实际: {actual_sha[:16]}...），已拒绝执行未经验证的可执行文件"
                        )
                    replace_with_retry(part_path, target_path)
                    log("[状态] 解压工具获取与 SHA-256 校验通过！")
                    return
                except _InstallCancelled:
                    remove_quiet(part_path)
                    raise
                except Exception as e:
                    last_err = e
                    log(f"[提示] 第 {attempt} 次获取失败: {e}")
                    remove_quiet(part_path)
                    if cancel_cb is not None and cancel_cb():
                        raise _InstallCancelled("已取消获取解压工具。")
                    if attempt < 3 and _sleep_cancelable(2.0, cancel_cb):
                        raise _InstallCancelled("已取消获取解压工具。")
        finally:
            _unregister_http_io(session=session)
    msg = (f"无法自动获取通过 SHA-256 校验的 7z 解压工具（{last_err}）。\n"
           f"请手动下载官方 7-Zip {SEVEN_Z_VERSION} 的 7zr.exe（SHA-256: {SEVEN_Z_SHA256}）"
           f"并保存到 {target_path}，然后重新运行安装。")
    log(f"[错误] {msg}")
    raise RuntimeError(msg)


def _parse_content_range(header: str) -> tuple[int, int, int | None] | None:
    """Parse ``Content-Range: bytes start-end/total`` header."""
    m = re.match(r"^\s*bytes\s+(\d+)-(\d+)/(\d+|\*)\s*$", header or "", re.IGNORECASE)
    if not m:
        return None
    start = int(m.group(1))
    end = int(m.group(2))
    total = int(m.group(3)) if m.group(3).isdigit() else None
    if start > end or (total is not None and end >= total):
        return None
    return start, end, total


def download_file_resumable(url, dest_path, expected_bytes, progress_cb, log_cb,
                            cancel_cb, stage_base=0.0, stage_weight=0.0,
                            allow_reset_part=False):
    """流式下载单源：.part 临时文件 + Range 断点续传 + 每块检查取消。"""
    log = log_cb or (lambda m: print(m))
    part = dest_path + ".part"
    is_7z_target = dest_path.lower().endswith(".7z")
    fobj = None
    try:
        with requests.Session() as session, _tracked_http_io(session=session):
            session.trust_env = False
            try:
                offset = os.path.getsize(part) if os.path.exists(part) else 0
            except OSError:
                offset = 0
            if offset > expected_bytes > 0:
                log("[警告] 本地断点超出文件大小（源内容可能已更新），重置断点从头下载...")
                remove_quiet(part)
                offset = 0
            if expected_bytes > 0 and offset == expected_bytes:
                if is_7z_target and not _is_valid_7z_header(part):
                    log("[警告] 本地已满断点缺少合法 7z 文件头，无法直接转正。")
                    if not allow_reset_part:
                        return False
                    remove_quiet(part)
                    offset = 0
                else:
                    try:
                        replace_with_retry(part, dest_path)
                        return True
                    except OSError:
                        return False

            # --- 服务端 Range 能力探测（仅当已有断点时） ---
            resumed = False
            if offset > 0:
                log(f"[状态] 检测到断点 {offset / (1024 ** 3):.2f}GiB，探测当前源是否支持续传...")
                range_supported = False
                range_rejected = False
                server_total = None
                try:
                    with (
                        session.get(
                            url,
                            headers={"Range": "bytes=0-0", "Accept-Encoding": "identity"},
                            stream=True,
                            timeout=(10, 15),
                            proxies=_NO_PROXY,
                        ) as probe,
                        _tracked_http_io(response=probe),
                    ):
                        probe_ctype = (probe.headers.get("Content-Type") or "").lower()
                        if any(bad in probe_ctype for bad in _BAD_BINARY_CTYPES):
                            log(f"[警告] 源探测返回非二进制类型（{probe_ctype}），保留断点换源...")
                            return False
                        if probe.status_code == 206:
                            content_range = probe.headers.get("Content-Range", "")
                            total = content_range.rsplit("/", 1)[-1].strip()
                            if total.isdigit():
                                server_total = int(total)
                            if server_total is not None and server_total < offset:
                                range_rejected = True
                            else:
                                range_supported = True
                        elif probe.status_code in (200, 416):
                            range_rejected = True
                        else:
                            log(f"[警告] 源探测返回 HTTP {probe.status_code}，保留断点换源...")
                            return False
                except requests.RequestException as e:
                    log(f"[警告] 源探测网络异常（{type(e).__name__}），保留断点换源...")
                    return False

                if range_rejected or not range_supported:
                    if not allow_reset_part:
                        log("[状态] 当前源不支持断点续传，保留断点尝试下一个源...")
                        return False
                    log("[警告] 当前源拒绝旧断点（Range 200/416/短于偏移），将在新响应就绪后从头下载...")
                    offset = 0
                else:
                    resumed = True

            headers = {"Accept-Encoding": "identity"}
            if offset > 0:
                headers["Range"] = f"bytes={offset}-"
            with (
                session.get(
                    url,
                    headers=headers,
                    stream=True,
                    timeout=(15, 30),
                    proxies=_NO_PROXY,
                ) as r,
                _tracked_http_io(response=r),
            ):
                dl_ctype = (r.headers.get("Content-Type") or "").lower()
                if any(bad in dl_ctype for bad in _BAD_BINARY_CTYPES):
                    log(f"[警告] 源返回非二进制响应（{dl_ctype}），换源：{url[:64]}")
                    return False

                if offset > 0 and r.status_code == 206:
                    parsed_cr = _parse_content_range(r.headers.get("Content-Range", ""))
                    if parsed_cr is None or parsed_cr[0] != offset:
                        log("[警告] 源 206 Content-Range 与本地偏移不一致，保留断点换源...")
                        return False
                    if parsed_cr[2] is not None and expected_bytes > 0 and parsed_cr[2] != expected_bytes:
                        log("[警告] 源 206 文件总长与预期不符，保留断点换源...")
                        return False
                elif offset > 0 and r.status_code == 200:
                    if not allow_reset_part:
                        log("[状态] 当前源忽略了 Range，保留断点尝试下一个源...")
                        return False
                    log("[提示] 下载时源忽略了 Range，将在首块校验通过后从头重新下载...")
                    offset = 0
                    resumed = False
                elif offset > 0 and r.status_code == 416:
                    if not allow_reset_part:
                        log("[状态] 当前源返回 HTTP 416，保留断点尝试下一个源...")
                        return False
                    log("[警告] 当前源返回 HTTP 416（断点偏移越界或源文件已更新），清理失效断点...")
                    remove_quiet(part)
                    return False
                elif r.status_code not in (200, 206):
                    log(f"[警告] 源返回 HTTP {r.status_code}，换源：{url[:64]}")
                    return False

                if offset == 0 and expected_bytes > 0 and r.status_code == 200:
                    cl_str = (r.headers.get("Content-Length") or "").strip()
                    if cl_str.isdigit() and int(cl_str) != expected_bytes:
                        log(f"[警告] 源 Content-Length ({cl_str}) 与预期 ({expected_bytes}) 不符，换源...")
                        return False

                # 首块先取到且校验魔数后再建/覆盖 .part：绝不破坏已有断点或留 0 字节文件
                chunk = r.raw.read(_DL_CHUNK, decode_content=False)
                if not chunk:
                    log("[警告] 源未返回任何数据（空响应），换源...")
                    return False
                while is_7z_target and offset == 0 and len(chunk) < len(_SEVEN_Z_MAGIC):
                    if cancel_cb():
                        return False
                    more = r.raw.read(_DL_CHUNK, decode_content=False)
                    if not more:
                        break
                    chunk += more
                if is_7z_target and offset == 0 and not chunk.startswith(_SEVEN_Z_MAGIC):
                    log("[警告] 源返回内容缺少 7z 魔数头（疑似网页/错误页），换源...")
                    return False
                if cancel_cb():
                    return False

                if resumed and offset > 0:
                    off_str = (f"{offset / (1024 ** 3):.2f}GB" if offset >= 1024 ** 3
                               else f"{offset / (1024 ** 2):.0f}MB")
                    log(f"[状态] 断点续传：从 {off_str} 处继续")
                elif offset <= 0:
                    log("[状态] 开始下载...")
                mode = "ab" if offset > 0 else "wb"
                try:
                    fobj = open(part, mode)
                except OSError as disk_exc:
                    raise _DiskWriteError(f"无法打开本地缓存文件：{disk_exc}") from disk_exc
                last_log = time.monotonic()
                last_log_bytes = offset
                while chunk:
                    try:
                        fobj.write(chunk)
                    except OSError as disk_exc:
                        raise _DiskWriteError(f"写入磁盘失败：{disk_exc}") from disk_exc
                    offset += len(chunk)
                    if progress_cb and stage_weight > 0:
                        progress_cb(stage_base
                                    + min(offset / max(expected_bytes, 1), 0.95) * stage_weight)
                    now = time.monotonic()
                    if now - last_log >= 5:
                        mbps = (offset - last_log_bytes) / (now - last_log) / (1024 * 1024)
                        remain = max(expected_bytes - offset, 0) / (1024 ** 3)
                        log(f"[状态] 已下载 {offset / (1024 ** 3):.2f}GB / "
                            f"{expected_bytes / (1024 ** 3):.2f}GB | "
                            f"速度 {mbps:.1f}MB/s | 剩余 {remain:.2f}GB")
                        last_log = now
                        last_log_bytes = offset
                    chunk = r.raw.read(_DL_CHUNK, decode_content=False)
                    if cancel_cb():
                        return False
                try:
                    fobj.flush()
                except OSError as disk_exc:
                    raise _DiskWriteError(f"刷新磁盘缓冲失败：{disk_exc}") from disk_exc
            if expected_bytes > 0 and offset == expected_bytes:
                if fobj is not None:
                    try:
                        fobj.close()
                    except OSError:
                        pass
                    fobj = None
                try:
                    replace_with_retry(part, dest_path)
                except OSError as disk_exc:
                    raise _DiskWriteError(f"重命名临时文件失败：{disk_exc}") from disk_exc
                return True
            if expected_bytes > 0 and offset > expected_bytes:
                log(f"[错误] 接收字节数超出预期（{offset} > {expected_bytes}），丢弃损坏临时文件...")
                if fobj is not None:
                    try:
                        fobj.close()
                    except OSError:
                        pass
                    fobj = None
                remove_quiet(part)
                return False
            log(f"[警告] 该源中途截断（{offset}/{expected_bytes} 字节），尝试下一个源续传...")
            return False
    except _DiskWriteError:
        raise
    except Exception as e:
        log(f"[警告] 源下载异常（{type(e).__name__}），尝试下一个源：{str(e)[:100]}")
        return False
    finally:
        if fobj is not None:
            try:
                fobj.close()
            except OSError:
                pass


# ============================================================
# 安装主流程
# ============================================================

def _progress_for_stages(idx):
    """计算第 idx 阶段（0-based）开始时的累计进度（0-1）"""
    return sum(w for _, w in STAGES[:idx])


def _weight_for_stage(idx):
    """获取第 idx 阶段的权重"""
    return STAGES[idx][1]


def install_engine(
    progress_cb=None,
    log_cb=None,
    state_cb=None,
    gpu_type=None,
    cancel_cb=None,
    base_dir=PROJECT_ROOT,
    preferred_url: str | None = None,
    pre_ranked_urls: list[str] | None = None,
):
    """执行完整安装流程（供 UI/CLI 调用）。

    参数:
        progress_cb:     callback(p: float)  整体进度 0.0~1.0
        log_cb:          callback(msg: str)  状态日志文本
        state_cb:        callback(stage: str) 当前阶段名（STAGES 中的名称）
        gpu_type:        None 时自动探测（detect_gpu_type），失败回落 "general"
        cancel_cb:       callback() -> bool  协作式取消检查
        base_dir:        应用根目录（GPT-SoVITS/、models/、7zr.exe、engine_temp/ 所在处）
        preferred_url:   用户选定优先源 URL；置顶并跳过冗余多源重测
        pre_ranked_urls: UI 已测速排序好的源列表（保持其降序与兜底顺位）
    返回: (success: bool, message: str)
    """
    progress_cb = progress_cb or _noop
    log_cb = log_cb or (lambda m: print(m))
    state_cb = state_cb or _noop
    cancel_cb = cancel_cb or _false

    # 显卡档位：未指定 → 自动探测 → 兜底通用包（兼容面最大）
    if gpu_type is None:
        gpu_type = detect_gpu_type() or "general"
    gpu_pkg = GPU_PACKAGES.get(gpu_type, GPU_PACKAGES["general"])
    target_file = gpu_pkg["file"]

    base_dir = base_dir or PROJECT_ROOT
    try:
        lock_ctx = _acquire_install_lock(base_dir)
        lock_ctx.__enter__()
    except BlockingIOError:
        msg = "另一个安装任务正在操作该目录，请等待其完成后再试（勿同时运行 GUI 与终端安装）。"
        log_cb(f"[错误] {msg}")
        return False, msg

    try:
        log_cb("===================================================")
        log_cb("               MamboTTS 引擎一键安装程序")
        log_cb("===================================================")

        p = engine_paths(base_dir)
        target_dir = os.path.join(base_dir, "engine_temp")
        extract_dir = p["gsv_dir"]
        tool_path = os.path.join(base_dir, "7zr.exe")
        os.makedirs(target_dir, exist_ok=True)

        # ===== 阶段 0: 下载整合包（多源测速或手选优选 + Range 续传 + 失败换源） =====
        state_cb(STAGES[0][0])

        # 磁盘空间预检（与 setup_view 共享 estimate_disk_requirement 唯一数据源）
        try:
            free_bytes = shutil.disk_usage(base_dir).free
            need_bytes, existing_cache = estimate_disk_requirement(gpu_pkg, base_dir)
            if free_bytes < need_bytes:
                return False, (
                    f"磁盘剩余空间不足：还需约 {need_bytes / (1024**3):.1f}GiB"
                    f"（整合包 {gpu_pkg['size_bytes'] / (1024**3):.1f}GiB + 解压产物，已扣除匹配缓存"
                    f" {existing_cache / (1024**3):.1f}GiB），当前仅剩 {free_bytes / (1024**3):.1f}GiB。\n"
                    "请清理磁盘，或把 MamboTTS 移到空间充足的磁盘后重试。"
                )
        except OSError:
            pass  # 极端环境拿不到磁盘信息时不拦截主流程

        if cancel_cb():
            return False, "下载尚未开始。"

        log_cb(f"[状态] 已选择显卡类型: {gpu_pkg['label']}")
        src_7z = os.path.join(target_dir, target_file)
        part_path = src_7z + ".part"
        downloaded_in_run = False

        # 完整缓存检查：除字节数外增加 .7z 魔数校验，防止外部下载工具预分配零文件被误删
        if os.path.exists(src_7z) and os.path.getsize(src_7z) == gpu_pkg["size_bytes"]:
            if not _is_valid_7z_header(src_7z):
                return False, (
                    f"检测到 {target_file} 文件大小已满但 7z 文件头无效（可能外部下载工具仍在"
                    "预分配或尚未写完）。请等待外部下载完成，或手动删除该文件后重试。"
                )
            log_cb("[状态] 检测到完整整合包缓存，跳过下载。")
        else:
            all_urls = build_source_urls(gpu_pkg, base_dir)
            base0 = _progress_for_stages(0)
            w0 = _weight_for_stage(0)
            progress_cb(base0 + 0.02 * w0)
            if preferred_url:
                ordered = [preferred_url]
                for u in (pre_ranked_urls or all_urls):
                    if u not in ordered:
                        ordered.append(u)
                log_cb(f"[状态] 使用选定下载源优先传输：{preferred_url[:88]}")
            elif pre_ranked_urls:
                ordered = list(dict.fromkeys(list(pre_ranked_urls) + all_urls))
                log_cb("[状态] 复用界面测速排名开始下载...")
            else:
                log_cb(
                    f"[状态] 开始下载 {target_file}（{gpu_pkg['size_bytes'] / (1024**3):.2f}GiB），"
                    f"正在对 {len(all_urls)} 个候选源测速..."
                )
                ordered = benchmark_sources(
                    all_urls, log_cb, cancel_cb=cancel_cb, expected_bytes=gpu_pkg["size_bytes"]
                )
            if cancel_cb():
                return False, "下载期间取消（已下载部分保留在 engine_temp，重试自动续传）。"
            progress_cb(base0 + 0.06 * w0)
            downloaded = False
            for attempt, allow_reset in enumerate((False, True), 1):
                if downloaded:
                    break
                if cancel_cb():
                    return False, "下载期间取消（已下载部分保留在 engine_temp，重试自动续传）。"
                if allow_reset and os.path.exists(part_path):
                    stale = os.path.getsize(part_path)
                    log_cb(
                        f"[状态] 第 2 轮容错检查（当前断点 {stale / (1024**3):.2f}GiB）："
                        "若确认源端拒绝偏移则重置断点，网络瞬断则保留继传..."
                    )
                log_cb(
                    f"[状态] 第 {attempt} 轮尝试："
                    f"{'允许校正失效断点' if allow_reset else '优先断点续传'}..."
                )
                for url in ordered:
                    if cancel_cb():
                        return False, "下载期间取消（已下载部分保留在 engine_temp，重试自动续传）。"
                    max_try = 2 if attempt == 1 else 1
                    for retry in range(max_try):
                        try:
                            dl_ok = download_file_resumable(
                                url, src_7z, gpu_pkg["size_bytes"],
                                progress_cb, log_cb, cancel_cb,
                                stage_base=base0, stage_weight=w0,
                                allow_reset_part=allow_reset,
                            )
                        except _DiskWriteError as disk_exc:
                            log_cb(f"[错误] 本地磁盘写入失败，已停止切换源重试：{disk_exc}")
                            return False, (
                                f"本地磁盘写入失败（可能磁盘空间已满、权限不足或设备离线）：{disk_exc}\n"
                                "已保留断点文件，处理完磁盘问题后重试即可继续下载。"
                            )
                        if dl_ok:
                            downloaded = True
                            downloaded_in_run = True
                            break
                        if cancel_cb():
                            return False, "下载期间取消（已下载部分保留在 engine_temp，重试自动续传）。"
                        if retry + 1 < max_try:
                            log_cb("[状态] 短暂重连同一源...")
                            time.sleep(0.5)
                    if downloaded:
                        break
                    log_cb("[状态] 当前源失败，自动切换下一个源...")
                if not downloaded and not os.path.exists(part_path):
                    break
            if not downloaded:
                if cancel_cb():
                    return False, "下载期间取消（已下载部分保留在 engine_temp，重试自动续传）。"
                return False, (
                    "所有下载源均失败，可任选以下方式自救：\n"
                    "1. 检查网络后直接重试安装——已下载部分自动续传，不浪费流量；\n"
                    f"2. 手动下载 {target_file} 放入 {target_dir}；\n"
                    "3. 若有更快镜像，把 URL 写入应用根目录的 mirrors.txt"
                    "（每行一条，{file} 为文件名占位符）。"
                )
        log_cb("[状态] 整合包下载完成。")
        progress_cb(_progress_for_stages(1))  # 阶段0结束

        # ===== 阶段 1: 校验完整性 =====
        state_cb(STAGES[1][0])
        if cancel_cb():
            return False, "已在下载完成后取消。"

        actual_size = os.path.getsize(src_7z)
        if actual_size != gpu_pkg["size_bytes"]:
            log_cb("[错误] 文件大小与记录不符："
                   f"实际 {actual_size / (1024**3):.2f}GiB / 期望 {gpu_pkg['size_bytes'] / (1024**3):.2f}GiB")
            if downloaded_in_run:
                remove_quiet(src_7z)
            return False, "整合包大小校验失败，请重试安装。"

        expected_sha = gpu_pkg.get("sha256", "")
        log_cb("[状态] 正在计算文件 SHA256...")
        actual = compute_sha256(
            src_7z,
            progress_cb=progress_cb,
            stage_base=_progress_for_stages(1),
            stage_weight=_weight_for_stage(1),
            cancel_cb=cancel_cb,
        )
        if expected_sha:
            if actual.lower() != expected_sha.lower():
                log_cb("[错误] 文件校验失败！")
                log_cb(f"  期望: {expected_sha}")
                log_cb(f"  实际: {actual}")
                if downloaded_in_run:
                    log_cb("已自动清理损坏下载缓存，请重新运行安装程序。")
                    remove_quiet(src_7z)
                    return False, "文件校验失败，缓存已清理，请重新运行。"
                return False, (
                    f"现有缓存 {target_file} 的 SHA256 校验未通过（可能外部文件未下完或版本不对），"
                    "已保留原文件未删除，请确认或移除后重试。"
                )
            log_cb("[状态] 文件校验通过。")
        else:
            log_cb(f"[提示] 本次整合包 SHA256: {actual}")
        progress_cb(_progress_for_stages(2))  # 阶段1结束

        # ===== 阶段 2: 解压整合包（先解压至 _staging 目录，成功后原子切换） =====
        state_cb(STAGES[2][0])
        if cancel_cb():
            return False, "已在校验完成后取消，整合包已缓存，重试可跳过下载。"

        if os.path.exists(tool_path) and not _valid_pe_tool(tool_path):
            log_cb("[警告] 检测到已存在的 7zr.exe 校验未通过（大小/PE头/SHA-256），重新获取...")
            remove_quiet(tool_path)
        if not os.path.exists(tool_path):
            download_7z_tool(tool_path, log_cb, cancel_cb=cancel_cb)

        try:
            free_before_extract = shutil.disk_usage(base_dir).free
            extract_need = int(actual_size * max(2.2, DISK_SPACE_FACTOR - 1.0))
            if free_before_extract < extract_need:
                return False, (
                    f"解压所需磁盘空间不足：约需 {extract_need / (1024**3):.1f}GiB，"
                    f"当前仅剩 {free_before_extract / (1024**3):.1f}GiB。\n"
                    "整合包已保存在 engine_temp/，请清理磁盘空间后重试安装（无需重新下载）。"
                )
        except OSError:
            pass

        staging_dir = os.path.join(base_dir, "GPT-SoVITS._staging")
        backup_dir = os.path.join(base_dir, "GPT-SoVITS._old")
        if os.path.exists(staging_dir):
            try:
                shutil.rmtree(staging_dir)
            except OSError as exc:
                return False, f"清理上次失败的解压暂存目录失败：{exc}"
        os.makedirs(staging_dir, exist_ok=True)

        log_cb(f"[状态] 正在使用 7-Zip 解压整合包到暂存区: {staging_dir} ...")
        log_cb("[提示] 解压大文件约需要 1-2 分钟，请不要关闭窗口...")

        extract_base = _progress_for_stages(2)
        extract_weight = _weight_for_stage(2)
        proc_holder: list[subprocess.Popen | None] = [None]
        stop_evt = threading.Event()
        cancel_hit = threading.Event()
        timeout_hit = threading.Event()

        def _kill_extract_proc():
            proc = proc_holder[0]
            if proc is not None:
                _kill_proc_tree(proc)

        def _watch_extract():
            """看护线程：协作式取消 + 解压硬超时，均通过杀 7zr 进程生效。"""
            started = time.monotonic()
            while not stop_evt.wait(0.25):
                if cancel_cb():
                    cancel_hit.set()
                    _kill_extract_proc()
                    return
                if time.monotonic() - started > _EXTRACT_TIMEOUT_SEC:
                    timeout_hit.set()
                    _kill_extract_proc()
                    return

        watcher = threading.Thread(target=_watch_extract, daemon=True, name="extract-watchdog")
        rc = -1
        proc = None
        try:
            creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            proc = subprocess.Popen(
                [tool_path, "x", src_7z, f"-o{staging_dir}", "-y", "-bsp1"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=creation_flags,
            )
            proc_holder[0] = proc
            _register_extract_proc(proc)
            if cancel_cb():
                cancel_hit.set()
                _kill_extract_proc()
            watcher.start()
            assert proc.stdout is not None
            fd = proc.stdout.fileno()
            carry = ""
            while True:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                carry = (carry + data.decode("utf-8", "replace"))[-4096:]
                matches = _PCT_RE.findall(carry.replace("\r", "\n"))
                if matches and extract_weight > 0:
                    pct = min(int(matches[-1]), 99)
                    progress_cb(extract_base + pct / 100.0 * extract_weight)
            proc.stdout.close()
            rc = proc.wait()
        except OSError as e:
            log_cb(f"[错误] 启动 7zr 解压失败: {e}")
            shutil.rmtree(staging_dir, ignore_errors=True)
            return False, "启动解压工具失败，请检查杀毒软件是否拦截了 7zr.exe。"
        except BaseException:
            _kill_extract_proc()
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise
        finally:
            stop_evt.set()
            _unregister_extract_proc(proc)
            if watcher.is_alive():
                watcher.join(timeout=2)
        if cancel_hit.is_set() or cancel_cb():
            shutil.rmtree(staging_dir, ignore_errors=True)
            return False, (
                "解压已取消：原有引擎目录保持完好，临时解压产物已清理（下载缓存已保留）。"
            )
        if timeout_hit.is_set():
            shutil.rmtree(staging_dir, ignore_errors=True)
            return False, "解压超时（30分钟），可能压缩包损坏或磁盘读写异常，请检查后重试。"
        if rc != 0:
            log_cb(f"[错误] 7zr 解压失败（退出码 {rc}），压缩包可能损坏。")
            shutil.rmtree(staging_dir, ignore_errors=True)
            return False, (
                f"解压失败（退出码 {rc}）。可重试安装；若反复失败，"
                "请删除 engine_temp 下的 7z 文件后重新安装。"
            )
        log_cb("[状态] 解压完成。")
        progress_cb(_progress_for_stages(3))  # 阶段2结束

        # ===== 阶段 3: 扁平化校验 + 原子替换旧目录 + 收尾配置 =====
        state_cb(STAGES[3][0])
        if cancel_cb():
            shutil.rmtree(staging_dir, ignore_errors=True)
            return False, (
                "收尾阶段被取消：已清理暂存目录，原有引擎未改动（下载已缓存）。"
            )

        # 安全嵌套扁平化：先将嵌套子目录改名避让，防止同名子目录覆盖自毁
        try:
            root_api = os.path.join(staging_dir, "api.py")
            if not os.path.exists(root_api):
                nested_candidates = [
                    os.path.join(staging_dir, name)
                    for name in os.listdir(staging_dir)
                    if os.path.isdir(os.path.join(staging_dir, name))
                    and os.path.exists(os.path.join(staging_dir, name, "api.py"))
                    and os.path.isdir(os.path.join(staging_dir, name, "runtime"))
                ]
                if len(nested_candidates) == 1:
                    nested_dir = nested_candidates[0]
                    nested_tmp = os.path.join(staging_dir, "__mambo_flatten_src__")
                    os.replace(nested_dir, nested_tmp)
                    log_cb("[状态] 优化文件目录结构...")
                    for item in os.listdir(nested_tmp):
                        dst_item = os.path.join(staging_dir, item)
                        if os.path.exists(dst_item):
                            if os.path.isdir(dst_item):
                                shutil.rmtree(dst_item)
                            else:
                                os.remove(dst_item)
                        shutil.move(os.path.join(nested_tmp, item), dst_item)
                    os.rmdir(nested_tmp)
        except OSError as e:
            log_cb(f"[提示] 目录结构整理失败: {e}")

        if not (
            os.path.isfile(os.path.join(staging_dir, "api.py"))
            and os.path.isfile(os.path.join(staging_dir, "runtime", "python.exe"))
        ):
            shutil.rmtree(staging_dir, ignore_errors=True)
            return False, (
                "解压产物缺少 api.py 或 runtime/python.exe，整合包结构异常。"
                "已保留原有引擎目录未覆盖。"
            )

        # 原子替换 extract_dir：若旧目录存在，先移至 _old，失败回滚
        if os.path.exists(backup_dir):
            try:
                shutil.rmtree(backup_dir)
            except OSError as exc:
                shutil.rmtree(staging_dir, ignore_errors=True)
                return False, f"清理上次遗留的旧版备份失败：{exc}"

        moved_old = False
        try:
            if os.path.exists(extract_dir):
                log_cb("[状态] 正在替换旧版引擎目录...")
                os.replace(extract_dir, backup_dir)
                moved_old = True
            os.replace(staging_dir, extract_dir)
        except OSError as exc:
            if moved_old and not os.path.exists(extract_dir) and os.path.exists(backup_dir):
                try:
                    os.replace(backup_dir, extract_dir)
                except OSError:
                    pass
            shutil.rmtree(staging_dir, ignore_errors=True)
            return False, (
                f"更新引擎目录失败（可能有引擎进程未退出）：{exc}\n"
                "请先关闭 GPT-SoVITS 相关进程或重启后再试。"
            )
        if moved_old:
            try:
                shutil.rmtree(backup_dir)
            except OSError:
                pass

        log_cb("[状态] 正在清理本次安装包缓存...")
        remove_quiet(src_7z)
        remove_quiet(part_path)
        try:
            if os.path.isdir(target_dir) and not os.listdir(target_dir):
                os.rmdir(target_dir)
        except OSError:
            pass

        log_cb("[状态] 正在生成本地引擎启动脚本...")
        sovits_rel = os.path.join("..", "models", SOVITS_MODEL)
        gpt_rel = os.path.join("..", "models", GPT_MODEL)
        ref_rel = os.path.join("..", "models", REF_WAV)
        # Bug B 修复：传入应用根目录下的 models/refer.wav（而非 GPT-SoVITS/models/）
        raw_ref_text = _engine_default_ref_text(p["ref_wav"])
        safe_ref_text = _sanitize_bat_arg(raw_ref_text)
        api_bat_content = (
            "@echo off\n"
            "title GPT-SoVITS Local API Server\n"
            "chcp 65001 >nul\n"
            "cd /d \"%~dp0\"\n"
            "echo Starting GPT-SoVITS API on http://127.0.0.1:9880 ...\n"
            "runtime\\python.exe api.py -a 127.0.0.1 -p 9880 ^\n"
            f"  -s \"{sovits_rel}\" ^\n"
            f"  -g \"{gpt_rel}\" ^\n"
            f"  -dr \"{ref_rel}\" ^\n"
            f"  -dt \"{safe_ref_text}\" ^\n"
            "  -dl zh\n"
            "pause\n"
        )
        try:
            with open(os.path.join(extract_dir, "go-api-mambo.bat"), "w",
                      encoding="utf-8") as f:
                f.write(api_bat_content)
        except OSError as e:
            log_cb(f"[提示] 启动脚本生成失败（不影响程序内启动引擎）: {e}")

        # 安装完成标记：is_engine_installed 用它识别「完整安装」，
        # 避免解压半途中断留下的 api.py+python.exe 被误判为已安装、
        # 之后引擎启动失败又不再进安装流程
        try:
            with open(os.path.join(extract_dir, INSTALL_MARKER), "w", encoding="utf-8") as f:
                f.write(f"installed_at={time.strftime('%Y-%m-%d %H:%M:%S')}\npackage={target_file}\n")
        except OSError:
            pass  # 标记写失败不致命：is_engine_installed 会走 torch 完整性代理

        progress_cb(1.0)  # 全部完成
        log_cb("===================================================")
        log_cb("恭喜！GPT-SoVITS 本地 GPU 推理引擎一键安装成功！")
        log_cb("===================================================")
        log_cb(f"安装路径: {extract_dir}")
        log_cb("接下来请启动 MamboTTS 客户端即可开始配音。")
        return True, "安装成功"

    except _InstallCancelled as e:
        return False, str(e)
    except Exception as e:
        logger.exception("安装过程中发生异常")
        log_cb(f"\n[错误] 安装过程中发生异常: {str(e)}")
        return False, f"安装过程中发生异常: {str(e)}"
    finally:
        lock_ctx.__exit__(None, None, None)
