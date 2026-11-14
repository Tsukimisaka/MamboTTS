"""ensure_deps.py — 依赖安装的"多源测速择优"引导器（GUI 无关，可独立测试）。

职责：确保 .venv 里能 import requirements.txt 声明的依赖（当前为 PySide6 + requests）。
由 MamboTTS.bat 在创建 .venv 之后调用：python -m mambotts.infra.ensure_deps

设计动机（实战教训）：
1. 硬编码"国内用户用阿里云镜像"不可靠——实测同一时刻官方 PyPI 可能比国内镜像快 25 倍，
   网络状况随地区/时段波动，唯一可靠的策略是"实时测速，谁快用谁"。
2. 国内 Clash/FlClash 等代理工具会注入 GHOST_PROXY 环境变量并开启 Windows 注册表
   系统代理（ProxyEnable=1 → 127.0.0.1:7890）。pip 底层 requests 会从注册表捡代理，
   导致对国内镜像的长连接被本地代理掐死（表现为下载卡死零进度）。
   → 测速一律 trust_env=False 直连；安装国内镜像时显式 --proxy ""；
     仅当最终回退到官方 PyPI 时才放行系统代理（海外用户走自己的代理反而更快）。

流程：
1. 幂等：当前解释器已能 import 全部依赖 → 直接退出 0。
2. 并发测速 5 个候选源（Range 拉取真实 wheel 前 1MB 计时）。
3. 按速度降序逐个尝试安装；国内镜像带 --proxy ""，官方源不带（保留用户系统代理）。
4. 任一源成功即退出 0；全部失败退出 1 并给出排查指引。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mambotts.infra import reconfigure_stdio_utf8, safe_print

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"

# 探针包：体积大、win_amd64 wheel 唯一，能真实代表大文件下载体验
PROBE_PACKAGE = "pyside6-addons"
PROBE_WHEEL_TAG = "win_amd64"
PROBE_BYTES = 1 * 1024 * 1024
PROBE_TIMEOUT = 12.0

MIRRORS: dict[str, str] = {
    "aliyun": "https://mirrors.aliyun.com/pypi/simple",
    "tuna": "https://pypi.tuna.tsinghua.edu.cn/simple",
    "tencent": "https://mirrors.cloud.tencent.com/pypi/simple",
    "huawei": "https://repo.huaweicloud.com/repository/pypi/simple",
    "pypi": "https://pypi.org/simple",
}

REQUIRED_MODULES = ("PySide6", "requests")


def deps_already_installed() -> bool:
    """幂等检查：当前解释器已能导入全部依赖则视为就绪。"""
    import importlib

    for mod in REQUIRED_MODULES:
        try:
            importlib.import_module(mod)
        except ImportError:
            return False
    return True


def _open_direct(url: str, headers: dict | None = None, timeout: float = 12):
    """Open ``url`` with the stdlib only, proxies fully disabled.

    stdlib matters: at first-install time ``requests`` is not installed yet
    (it is one of the packages being installed), so the probe must not
    depend on it.
    """
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "pip/24.0 (MamboTTS-ensure-deps)", **(headers or {})})
    # build_opener with a no-proxy handler beats both env vars and registry
    proxy_disabled = urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(proxy_disabled)
    return opener.open(req, timeout=timeout)


def _resolve_wheel_url(mirror: str) -> str | None:
    """从源的 simple 索引页解析探针包的 win_amd64 wheel 直链。"""
    with _open_direct(f"{mirror}/{PROBE_PACKAGE}/", timeout=PROBE_TIMEOUT) as r:
        status = getattr(r, "status", 200)
        if isinstance(status, int) and status >= 400:
            raise RuntimeError(f"HTTP {status}")
        html_text = r.read().decode("utf-8", errors="replace")
    links = re.findall(r'href="([^"]+)"', html_text)
    hits = [u for u in links if PROBE_WHEEL_TAG in u]
    if not hits:
        return None
    url = hits[-1].split("#")[0]
    if url.startswith("/"):
        # 形如 /packages/... 的绝对路径 → 拼站点根（simple 之前的部分）
        origin = mirror.split("/simple")[0] if "/simple" in mirror else mirror
        url = origin + url
    elif url.startswith("../"):
        from urllib.parse import urljoin
        url = urljoin(f"{mirror.rstrip('/')}/{PROBE_PACKAGE}/", url)
    elif not url.startswith("http"):
        url = f"{mirror}/{url}"
    return url


def _probe_speed(mirror: str) -> tuple[float | None, str | None]:
    """对单个源做 1MB Range 拉流测速，返回 (MB/s, 错误信息)。"""
    try:
        url = _resolve_wheel_url(mirror)
        if not url:
            return None, "index has no matching win_amd64 wheel"
        t0 = time.time()
        with _open_direct(url, headers={"Range": f"bytes=0-{PROBE_BYTES - 1}"},
                          timeout=PROBE_TIMEOUT) as r:
            got = 0
            while got < PROBE_BYTES:
                chunk = r.read(65536)
                if not chunk:
                    break
                got += len(chunk)
        dt = time.time() - t0
        if got <= 0 or dt <= 0:
            return None, "empty probe response"
        return got / PROBE_BYTES / dt, None
    except Exception as e:  # 网络错误千奇百怪，统一降级
        return None, f"{type(e).__name__}: {str(e)[:80]}"


def benchmark_mirrors() -> list[tuple[str, float]]:
    """并发测速全部候选源，返回按速度降序的 [(名称, MB/s)]（失败的排在最后）。"""
    results: list[tuple[str, float | None, str | None]] = []
    with ThreadPoolExecutor(max_workers=len(MIRRORS)) as ex:
        for name, (bps, err) in zip(MIRRORS, ex.map(_probe_speed, MIRRORS.values())):
            results.append((name, bps, err))
    for name, bps, err in results:
        if bps:
            safe_print(f"[测速] {name:8s} {bps:6.2f} MB/s")
        else:
            safe_print(f"[测速] {name:8s} 不可达（{err}）")
    ok = [(n, b) for n, b, _ in results if b]
    ok.sort(key=lambda x: -x[1])
    return ok


def install_from(mirror_name: str, mirror_url: str, log) -> bool:
    """从指定源安装 requirements.txt；返回是否成功。"""
    pip_args = [
        "install",
        "-r", str(REQUIREMENTS),
        "-i", mirror_url,
        "--timeout", "15",
        "--retries", "2",
        "--disable-pip-version-check",
    ]
    env = os.environ.copy()
    if mirror_name != "pypi":
        # pip 24+ treats `--proxy=""` as falsy and still loads HTTP_PROXY / Windows registry
        # proxies via PipSession(trust_env=True). Strip proxy env AND stub urllib.request.getproxies
        # in the child pip process so domestic mirrors always connect directly.
        for key in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy",
            "PIP_PROXY",
        ):
            env.pop(key, None)
        env["NO_PROXY"] = "*"
        env["no_proxy"] = "*"
        env["PIP_NO_PROXY"] = "*"
        bootstrap = (
            "import runpy, sys, urllib.request; "
            "urllib.request.getproxies = lambda: {}; "
            "sys.argv = ['pip'] + sys.argv[1:]; "
            "runpy.run_module('pip', run_name='__main__')"
        )
        cmd = [sys.executable, "-c", bootstrap, *pip_args]
    else:
        # 官方 PyPI 保留系统代理配置，兼容海外或 TUN 用户。
        cmd = [sys.executable, "-m", "pip", *pip_args]
    log(f"正在从 {mirror_name} 安装依赖（首次约 2-5 分钟，请勿关闭窗口）...")
    proc = subprocess.run(cmd, env=env)
    return proc.returncode == 0


def main() -> int:
    reconfigure_stdio_utf8()
    if deps_already_installed():
        safe_print("[ensure-deps] 依赖已就绪，跳过安装。")
        return 0
    if not REQUIREMENTS.exists():
        safe_print(f"[ensure-deps] 错误：找不到 {REQUIREMENTS}")
        return 1

    safe_print("[ensure-deps] 正在并发测速候选镜像源...")
    ranked = benchmark_mirrors()
    if not ranked:
        safe_print("[ensure-deps] 所有源测速失败，按默认顺序直接尝试...")
        ranked = [(name, 0.0) for name in MIRRORS]

    for name, bps in ranked:
        url = MIRRORS[name]
        speed = f"（测速 {bps:.2f} MB/s）" if bps else ""
        safe_print(f"\n[ensure-deps] === 尝试源: {name}{speed} ===")
        if install_from(name, url, safe_print):
            safe_print(f"[ensure-deps] 安装成功（源: {name}）。")
            return 0
        if name == "pypi":
            safe_print(
                "[ensure-deps] 提示：官方源安装失败时，本机系统代理（Clash 等）"
                "可能是元凶——测速走直连，pip 却会走系统代理。"
                "可尝试关闭系统代理后重试。"
            )
        safe_print(f"[ensure-deps] 源 {name} 安装失败，切换下一个源...")

    safe_print("[ensure-deps] 错误：所有镜像源均安装失败。请检查网络后重新运行。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
