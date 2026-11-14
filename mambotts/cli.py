"""Headless command-line interface (spec item 15).

Subcommands: ``synth`` / ``batch`` /
``engine status|start|stop|install`` with ``--json`` output where useful.

Exit codes: 0 = ok, 1 = error, 2 = usage, 3 = engine offline.

This module never imports PySide6 — it only touches stdlib plus
``mambotts.domain.*``, ``mambotts.pipeline.*`` and ``mambotts.infra.*``
(engine / installer modules are imported lazily inside their handlers).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import re
import shutil
import sys
import threading
import time
import wave

from mambotts import APP_VERSION
from mambotts.domain.models import (
    API_PORT,
    DEFAULT_API_URL,
    SPEED_MAX,
    SPEED_MIN,
    SynthesisJobResult,
    has_pronounceable_char,
)
from mambotts.domain.naming import is_reserved_windows_name, sanitize_stem
from mambotts.domain.storage_repo import atomic_write_json, remove_quiet, replace_with_retry
from mambotts.domain.voice_repo import VoiceRepository
from mambotts.infra import reconfigure_stdio_utf8
from mambotts.infra.gsv_client import GSVHttpClient
from mambotts.pipeline.scheduler import SynthesisPipeline

__all__ = ["run_cli"]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_ENGINE_OFFLINE = 3

_ENGINE_START_WAIT_SEC = 60.0


class _UsageError(Exception):
    """Raised for argument-parse / usage failures (exit code 2)."""


class _ArgumentParser(argparse.ArgumentParser):
    """argparse parser that raises instead of calling ``sys.exit`` on errors."""

    def error(self, message: str):  # noqa: D401 - argparse hook signature
        raise _UsageError(message)


def _make_clients(api_url: str | None = None):
    """Build ``(voice_repo, gsv_client, supervisor)``.

    Patch point for tests: monkeypatch ``mambotts.cli._make_clients``.
    """
    from mambotts.infra.engine_mgr import EngineSupervisor

    return (
        VoiceRepository(),
        GSVHttpClient(api_url=api_url or DEFAULT_API_URL),
        EngineSupervisor(),
    )


def _log(message: str) -> None:
    # stderr keeps stdout clean so --json output stays machine-parseable.
    print(message, file=sys.stderr, flush=True)


def _normalize_api_url_arg(api_url: str | None) -> str | None:
    if not api_url:
        return None
    from mambotts.infra.engine_mgr import validate_http_api_url

    normalized = validate_http_api_url(api_url)
    if normalized is None:
        raise _UsageError(f"--api-url 格式无效，需为 http(s)://host:port（当前：{api_url}）")
    return normalized


def _validate_synth_args(args, *, check_wav_out: bool = False) -> None:
    if not math.isfinite(args.speed) or args.speed < SPEED_MIN or args.speed > SPEED_MAX:
        raise _UsageError(
            f"--speed 必须在 {SPEED_MIN}~{SPEED_MAX} 之间，当前为 {args.speed}"
        )
    if getattr(args, "api_url", None):
        args.api_url = _normalize_api_url_arg(args.api_url)
    if check_wav_out:
        if not has_pronounceable_char(args.text or ""):
            raise _UsageError("待合成文本为空或缺少可朗读字符。")
        out_raw = (args.out or "").strip()
        base = os.path.basename(out_raw)
        stem_part, ext = os.path.splitext(base)
        if not out_raw or not base or not stem_part or ext.lower() != ".wav":
            raise _UsageError(f"-o/--out 必须为有效的 .wav 文件路径，当前为 {args.out!r}")
        if any(ch in '<>:"/\\|?*' or ord(ch) < 32 for ch in base):
            raise _UsageError(f"-o/--out 文件名包含非法字符：{base!r}")
        if is_reserved_windows_name(base):
            raise _UsageError(f"-o/--out 文件名不能使用系统保留名称：{stem_part}")
        if os.path.isdir(out_raw):
            raise _UsageError(f"-o/--out 指向一个已存在的目录：{out_raw}")
        parent = os.path.dirname(os.path.abspath(out_raw))
        if parent and os.path.exists(parent) and not os.path.isdir(parent):
            raise _UsageError(f"-o/--out 的上级路径不是目录：{parent}")


def _ensure_engine_online(gsv_client, auto_start: bool = False,
                          wait_seconds: float = _ENGINE_START_WAIT_SEC,
                          api_url: str | None = None) -> bool:
    """Return True once the engine answers ``/control``; optionally start it."""
    if gsv_client.is_api_running():
        return True
    if not auto_start:
        return False
    _log("本地引擎离线，正在以分离守护模式启动 ...")
    from mambotts.infra.engine_mgr import EngineSupervisor

    target_url = api_url or getattr(gsv_client, "api_url", None) or DEFAULT_API_URL
    supervisor = EngineSupervisor()
    ok, message = supervisor.start(api_url=target_url, bind_job_object=False)
    _log(message)
    if not ok:
        return gsv_client.is_api_running()
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    while time.monotonic() < deadline:
        if gsv_client.is_api_running():
            return True
        if getattr(supervisor, "_proc", None) is not None and not supervisor.is_running():
            _log("引擎子进程在启动完成前意外退出，请查看 engine.log 排查原因。")
            return False
        time.sleep(0.5)
    return gsv_client.is_api_running()


def _offline_payload(message: str) -> dict:
    return dataclasses.asdict(SynthesisJobResult(success=False, message=message))


def _print_job_result(result) -> None:
    print(f"结果: {result.message}")
    if result.wav_path:
        print(f"音频: {result.wav_path}")
    if result.srt_path:
        print(f"字幕: {result.srt_path}")
    print(f"时长: {result.duration_sec:.3f} 秒, 分句: {len(result.clauses)}")
    for clause in result.clauses:
        cache_flag = " [缓存]" if clause.from_cache else ""
        print(
            f"  #{clause.index + 1:03d} {clause.start_ms}-{clause.end_ms} ms"
            f"{cache_flag} {clause.text}"
        )


# ---------------------------------------------------------------------------
# synth
# ---------------------------------------------------------------------------

def _cmd_synth(args) -> int:
    _validate_synth_args(args, check_wav_out=True)
    voice_repo, gsv_client, supervisor = _make_clients(args.api_url)
    if not _ensure_engine_online(
        gsv_client, auto_start=args.auto_start_engine, api_url=args.api_url
    ):
        message = (
            f"本地引擎未运行 ({args.api_url or DEFAULT_API_URL})。"
            "请先执行: python -m mambotts engine start (或加 --auto-start-engine)。"
        )
        if args.json:
            print(json.dumps(_offline_payload(message), ensure_ascii=False, indent=2))
        else:
            print(message, file=sys.stderr)
        return EXIT_ENGINE_OFFLINE

    pipeline = SynthesisPipeline(voice_repo, gsv_client, supervisor=supervisor)
    result = pipeline.run_job(
        args.text,
        args.out,
        speed=args.speed,
        api_url=args.api_url,
        export_srt=args.srt,
        trim_silence=not args.no_trim,
        normalize_audio=not args.no_normalize,
        use_cache=not args.no_cache,
    )
    if args.json:
        print(json.dumps(dataclasses.asdict(result), ensure_ascii=False, indent=2))
    else:
        _print_job_result(result)
    return EXIT_OK if result.success else EXIT_ERROR


# ---------------------------------------------------------------------------
# batch
# ---------------------------------------------------------------------------

def _copy_clause_file(src_path: str, dst_path: str) -> None:
    part_path = f"{dst_path}.{os.getpid()}.{threading.get_ident()}.part"
    try:
        shutil.copyfile(src_path, part_path)
        replace_with_retry(part_path, dst_path)
    except BaseException:
        remove_quiet(part_path)
        raise


def _extract_wav_slice(master_wav: str, dst_path: str, start_ms: int, end_ms: int) -> None:
    """Slice ``[start_ms, end_ms]`` from assembled WAV into ``dst_path`` atomically."""
    part_path = f"{dst_path}.{os.getpid()}.{threading.get_ident()}.part"
    try:
        with wave.open(master_wav, "rb") as src_wf:
            params = src_wf.getparams()
            rate = params.framerate or 1
            total_frames = params.nframes
            s_frame = max(0, min(total_frames, int(round(start_ms * rate / 1000.0))))
            e_frame = max(s_frame, min(total_frames, int(round(end_ms * rate / 1000.0))))
            src_wf.setpos(s_frame)
            frames = src_wf.readframes(e_frame - s_frame)
        with wave.open(part_path, "wb") as dst_wf:
            dst_wf.setnchannels(params.nchannels)
            dst_wf.setsampwidth(params.sampwidth)
            dst_wf.setframerate(rate)
            dst_wf.writeframes(frames)
        replace_with_retry(part_path, dst_path)
    except BaseException:
        remove_quiet(part_path)
        raise


def _cmd_batch(args) -> int:
    _validate_synth_args(args, check_wav_out=False)
    input_path = os.path.abspath(args.input)
    if not os.path.isfile(input_path):
        print(f"输入文件不存在: {input_path}", file=sys.stderr)
        return EXIT_ERROR
    try:
        with open(input_path, "r", encoding="utf-8-sig", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        print(f"读取输入文件失败: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if not text.strip() or not has_pronounceable_char(text):
        print("输入文件为空或缺少可朗读字符，没有可合成的内容。", file=sys.stderr)
        return EXIT_ERROR

    stem = sanitize_stem(os.path.splitext(os.path.basename(input_path))[0])
    out_dir = os.path.abspath(args.out_dir) if args.out_dir else (
        os.path.dirname(input_path) or os.getcwd()
    )
    if os.path.exists(out_dir) and not os.path.isdir(out_dir):
        print(f"输出目录路径被文件占用: {out_dir}", file=sys.stderr)
        return EXIT_ERROR
    save_path = os.path.join(out_dir, f"{stem}.wav")

    voice_repo, gsv_client, supervisor = _make_clients(args.api_url)
    if not _ensure_engine_online(
        gsv_client, auto_start=args.auto_start_engine, api_url=args.api_url
    ):
        message = (
            f"本地引擎未运行 ({args.api_url or DEFAULT_API_URL})，batch 终止。"
            "请先执行: python -m mambotts engine start (或加 --auto-start-engine)。"
        )
        if args.json:
            print(json.dumps(_offline_payload(message), ensure_ascii=False, indent=2))
        else:
            print(message, file=sys.stderr)
        return EXIT_ENGINE_OFFLINE
    os.makedirs(out_dir, exist_ok=True)

    pipeline = SynthesisPipeline(voice_repo, gsv_client, supervisor=supervisor)
    result = pipeline.run_job(
        text,
        save_path,
        speed=args.speed,
        api_url=args.api_url,
        export_srt=args.srt,
        trim_silence=not args.no_trim,
        normalize_audio=not args.no_normalize,
        use_cache=not args.no_cache,
    )
    if not result.success:
        if args.json:
            print(json.dumps(dataclasses.asdict(result), ensure_ascii=False, indent=2))
        else:
            print(f"合成失败: {result.message}", file=sys.stderr)
        return EXIT_ERROR

    from mambotts.infra.installer import compute_sha256

    clause_entries = []
    for clause in result.clauses:
        target = os.path.join(out_dir, f"{stem}_{clause.index + 1:03d}.wav")
        try:
            if clause.wav_path and os.path.isfile(clause.wav_path):
                if os.path.abspath(clause.wav_path) != os.path.abspath(target):
                    _copy_clause_file(clause.wav_path, target)
            elif result.wav_path and os.path.isfile(result.wav_path):
                _extract_wav_slice(result.wav_path, target, clause.start_ms, clause.end_ms)
        except (OSError, wave.Error) as exc:
            print(f"写出分句音频失败 ({target}): {exc}", file=sys.stderr)
            return EXIT_ERROR
        sha256 = ""
        if os.path.isfile(target):
            sha256 = compute_sha256(target)
        clause_entries.append(
            {
                "index": clause.index,
                "text": clause.text,
                "wav_path": os.path.abspath(target),
                "sha256": sha256,
                "duration_sec": round(clause.duration_ms / 1000.0, 3),
                "start_ms": clause.start_ms,
                "end_ms": clause.end_ms,
            }
        )

    manifest = {
        "source_file": input_path,
        "wav_path": os.path.abspath(result.wav_path or save_path),
        "srt_path": os.path.abspath(result.srt_path) if result.srt_path else "",
        "duration_sec": result.duration_sec,
        "clauses": clause_entries,
    }
    manifest_path = (
        os.path.join(out_dir, "manifest.json")
        if not args.manifest or args.manifest == "__AUTO__"
        else os.path.abspath(args.manifest)
    )
    os.makedirs(os.path.dirname(manifest_path) or ".", exist_ok=True)
    atomic_write_json(manifest_path, manifest)

    if args.json:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    else:
        _print_job_result(result)
        print(f"清单: {manifest_path}")
        print(f"分句文件: {len(clause_entries)} 个 -> {out_dir}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------

def _cmd_engine_status(args) -> int:
    from mambotts.infra.engine_mgr import is_engine_installed

    args.api_url = _normalize_api_url_arg(args.api_url)
    _voice_repo, gsv_client, _supervisor = _make_clients(args.api_url)
    api_url = args.api_url or DEFAULT_API_URL
    installed = is_engine_installed()
    running = gsv_client.is_api_running()
    payload = {"installed": bool(installed), "running": bool(running), "api_url": api_url}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        state = "在线" if running else "离线"
        readiness = "已安装" if installed else "未安装"
        print(f"引擎{state} ({api_url}); 引擎包{readiness}。")
    return EXIT_OK if running else EXIT_ENGINE_OFFLINE


def _cmd_engine_start(args) -> int:
    from mambotts.infra.engine_mgr import EngineSupervisor

    args.api_url = _normalize_api_url_arg(args.api_url)
    api_url = args.api_url or DEFAULT_API_URL
    supervisor = EngineSupervisor()
    ok, message = supervisor.start(api_url=api_url, bind_job_object=False)
    print(message, flush=True)
    if not ok:
        return EXIT_ERROR
    # Detached daemon: report readiness without blocking the CLI forever.
    probe_client = GSVHttpClient(api_url=api_url)
    deadline = time.monotonic() + _ENGINE_START_WAIT_SEC
    while time.monotonic() < deadline:
        if probe_client.is_api_running():
            print(f"引擎已上线: {api_url}", flush=True)
            return EXIT_OK
        if getattr(supervisor, "_proc", None) is not None and not supervisor.is_running():
            print(
                "引擎子进程在加载期间意外退出，请查看 engine.log 定位具体原因。",
                file=sys.stderr,
                flush=True,
            )
            return EXIT_ERROR
        time.sleep(1.0)
    print(
        f"引擎进程已启动，但未在 {_ENGINE_START_WAIT_SEC:.0f} 秒内上线；"
        "请查看 engine.log 或稍后用 engine status 检查。",
        file=sys.stderr,
        flush=True,
    )
    return EXIT_ERROR


_ENGINE_CMD_RE = re.compile(r'(?:^|[\s"\'\\/])api(?:_v2)?\.py(?=$|[\s"\'])', re.IGNORECASE)


def _cmdline_is_engine(cmdline: str) -> bool:
    """Return True only when a process commandline matches local GPT-SoVITS api.py."""
    lower = (cmdline or "").lower()
    if _ENGINE_CMD_RE.search(lower):
        return True
    return ("gpt-sovits" in lower or "gpt_sovits" in lower) and "python" in lower


def _pid_is_engine_process(pid: int) -> bool:
    """Verify ``pid`` looks like a GPT-SoVITS / api.py process before killing it."""
    if sys.platform != "win32":
        return True
    exe_path = ""
    try:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if handle:
            try:
                buf_len = ctypes.c_ulong(32768)
                buf = ctypes.create_unicode_buffer(buf_len.value)
                if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(buf_len)):
                    exe_path = buf.value or ""
            finally:
                k32.CloseHandle(handle)
    except Exception:
        exe_path = ""

    exe_lower = exe_path.lower()
    if "gpt-sovits" in exe_lower:
        return True
    exe_base = os.path.basename(exe_lower)
    if exe_base and exe_base not in ("python.exe", "pythonw.exe", "python3.exe"):
        return False

    import subprocess as _sp

    flags = _sp.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        ps = _sp.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine",
            ],
            capture_output=True,
            timeout=5,
            creationflags=flags,
        )
        stdout_bytes = ps.stdout if isinstance(ps.stdout, (bytes, bytearray)) else b""
        cmdline = stdout_bytes.decode("utf-8", errors="ignore")
        return _cmdline_is_engine(cmdline)
    except (OSError, _sp.SubprocessError):
        return False


def _kill_engine_by_port(port: int = API_PORT) -> bool:
    """按监听端口精确定位本机引擎进程树并终止（先核验确属 GPT-SoVITS / api.py）。"""
    import subprocess as _sp

    pids: set[int] = set()
    flags = _sp.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        raw = _sp.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            timeout=10,
            creationflags=flags,
        ).stdout or b""
        # netstat on zh-CN Windows emits GBK headers; digits/keywords are
        # ASCII so ignore-decode is sufficient and never raises.
        out = raw.decode("utf-8", errors="ignore")
    except (OSError, _sp.SubprocessError):
        return False
    local_addr_re = re.compile(
        rf"^(?:127\.\d+\.\d+\.\d+|0\.0\.0\.0|\[::(?:1)?\]):{int(port)}$"
    )
    for line in out.splitlines():
        parts = line.split()
        if (
            len(parts) >= 5
            and parts[0].upper() == "TCP"
            and local_addr_re.match(parts[1])
            and parts[3].upper() == "LISTENING"
            and parts[4].isdigit()
        ):
            pids.add(int(parts[4]))
    killed = False
    for pid in pids:
        if not _pid_is_engine_process(pid):
            _log(f"跳过端口 {port} 上的非引擎进程 PID={pid}（避免误杀无关服务）。")
            continue
        try:
            _sp.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                creationflags=flags,
            )
            killed = True
        except (OSError, _sp.SubprocessError):
            pass
    return killed


def _cmd_engine_stop(args) -> int:
    from mambotts.infra.engine_mgr import (
        EngineSupervisor,
        _host_port_from_url,
        is_local_api_url,
    )

    args.api_url = _normalize_api_url_arg(getattr(args, "api_url", None))
    api_url = args.api_url or DEFAULT_API_URL
    if not is_local_api_url(api_url):
        raise _UsageError(f"--api-url 仅支持停止本机引擎，不能指向远端地址：{api_url}")
    _host, port = _host_port_from_url(api_url)
    supervisor = EngineSupervisor()
    supervisor.stop()
    if _kill_engine_by_port(port):
        print(f"已停止本地引擎进程（端口 {port}，含其他实例拉起的引擎）。", flush=True)
    else:
        print(f"本地引擎当前未在运行（端口 {port}）。", flush=True)
    return EXIT_OK


def _cmd_engine_install(args) -> int:
    from mambotts.infra.installer import install_engine, terminate_active_install_processes

    gpu_type = None if args.gpu == "auto" else args.gpu
    cancel_evt = threading.Event()
    try:
        ok, message = install_engine(
            gpu_type=gpu_type,
            log_cb=lambda msg: print(msg, flush=True),
            cancel_cb=cancel_evt.is_set,
        )
    except KeyboardInterrupt:
        cancel_evt.set()
        terminate_active_install_processes()
        raise
    print(message, flush=True)
    return EXIT_OK if ok else EXIT_ERROR


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _add_common_synth_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--speed", type=float, default=1.0, help=f"语速 {SPEED_MIN}~{SPEED_MAX}")
    parser.add_argument("--srt", action="store_true", help="同步导出 .srt 字幕")
    parser.add_argument("--no-trim", action="store_true", help="禁用静音修剪")
    parser.add_argument("--no-normalize", action="store_true", help="禁用峰值归一化")
    parser.add_argument("--no-cache", action="store_true", help="禁用分句缓存复用")
    parser.add_argument("--api-url", default=None, help="引擎 API 地址（默认 127.0.0.1:9880）")
    parser.add_argument("--auto-start-engine", action="store_true",
                        help="引擎离线时自动启动本地引擎并等待其上线")


def _build_parser() -> _ArgumentParser:
    parser = _ArgumentParser(
        prog="mambotts",
        description="MamboTTS v2 — 本地 AI 语音配音工作站（无头 CLI 模式）",
    )
    parser.add_argument("--version", action="version", version=f"mambotts {APP_VERSION}")
    parser.epilog = ("退出码: 0=成功, 1=运行错误, 2=参数错误, 3=引擎离线。"
                     "不带子命令运行将打开图形界面。")
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.required = True

    # -- synth ----------------------------------------------------------
    p_synth = sub.add_parser("synth", help="合成单篇脚本文本", description="合成单篇脚本文本")
    p_synth.add_argument("text", help="要合成的脚本文本（用中英文标点控制自然停顿）")
    p_synth.add_argument("-o", "--out", required=True, help="输出 WAV 路径")
    _add_common_synth_args(p_synth)
    p_synth.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    p_synth.set_defaults(func=_cmd_synth)

    # -- batch ----------------------------------------------------------
    p_batch = sub.add_parser("batch", help="批量合成脚本文件", description="批量合成脚本文件")
    p_batch.add_argument("input", help="输入脚本文本文件 (UTF-8)")
    p_batch.add_argument("--out-dir", default=None, help="输出目录（默认与输入文件同目录）")
    p_batch.add_argument("--manifest", nargs="?", const="__AUTO__", default=None,
                         help="清单 JSON 路径（可省略路径，默认 <out-dir>/manifest.json）")
    _add_common_synth_args(p_batch)
    p_batch.add_argument("--json", action="store_true", help="以 JSON 输出清单")
    p_batch.set_defaults(func=_cmd_batch)

    # -- engine ---------------------------------------------------------
    p_engine = sub.add_parser("engine", help="本地引擎生命周期管理", description="本地引擎生命周期管理")
    engine_sub = p_engine.add_subparsers(dest="engine_command",
                                         metavar="{status,start,stop,install}")
    engine_sub.required = True

    p_status = engine_sub.add_parser("status", help="查询引擎安装与在线状态")
    p_status.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_status.add_argument("--api-url", default=None)
    p_status.set_defaults(func=_cmd_engine_status)

    p_start = engine_sub.add_parser("start", help="以分离守护模式启动引擎")
    p_start.add_argument("--api-url", default=None)
    p_start.set_defaults(func=_cmd_engine_start)

    p_stop = engine_sub.add_parser("stop", help="停止托管的引擎进程")
    p_stop.add_argument("--api-url", default=None)
    p_stop.set_defaults(func=_cmd_engine_stop)

    p_install = engine_sub.add_parser("install", help="下载并安装 GPT-SoVITS 引擎包")
    p_install.add_argument("--gpu", choices=["auto", "nvidia50", "general"], default="auto",
                           help="GPU 包类型（auto 为自动探测）")
    p_install.set_defaults(func=_cmd_engine_install)

    return parser


def run_cli(argv: list[str]) -> int:
    """Entry point for the headless CLI; returns the process exit code."""
    # Force UTF-8 with safe fallback so --json and pipes emit valid UTF-8 on legacy consoles.
    reconfigure_stdio_utf8()
    parser = _build_parser()
    try:
        args = parser.parse_args(list(argv))
    except _UsageError as exc:
        print(f"参数错误: {exc}", file=sys.stderr)
        print("使用 --help 查看帮助。", file=sys.stderr)
        return EXIT_USAGE
    try:
        return int(args.func(args))
    except _UsageError as exc:
        print(f"参数错误: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("已中断。", file=sys.stderr)
        return EXIT_ERROR
