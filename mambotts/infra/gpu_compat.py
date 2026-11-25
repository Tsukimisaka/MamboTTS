"""Hardware requirements for the engine variants offered by the installer."""

from __future__ import annotations

import re
import subprocess
import sys

from .dxgi import amd_directml_adapter


ROCM = "amd_rocm"
DIRECTML = "amd_directml"

# Windows support matrix for the version of ROCm installed with the engine.
# https://rocm.docs.amd.com/projects/radeon-ryzen/en/docs-7.2.1/docs/compatibility/compatibilityrad/windows/windows_compatibility.html
_ROCM_RADEON = re.compile(
    r"\b(?:RX (?:9070(?: XT)?|9060 XT|7900 XTX|7700)|PRO W7900(?: Dual Slot)?|AI PRO R9700)\s*$",
    re.I,
)


def windows_radeon_adapters() -> tuple[str, ...]:
    if sys.platform != "win32":
        return ()
    command = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        "Get-CimInstance Win32_VideoController -ErrorAction Stop | "
        "Where-Object { $_.PNPDeviceID -match '^PCI\\\\VEN_1002&' } | "
        "Select-Object -ExpandProperty Name"
    )
    try:
        output = subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            encoding="utf-8", errors="replace", stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=6,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    return tuple(line.strip() for line in output.splitlines() if line.strip())


def windows_build_number() -> int:
    return sys.getwindowsversion().platform_version[2] if sys.platform == "win32" else 0


def gpu_incompatibilities(
    nvidia_package: str | None, radeon_adapters: tuple[str, ...], windows_build: int
) -> dict[str, str]:
    """Return the reason for each choice that this machine cannot use."""
    blocked: dict[str, str] = {}
    if radeon_adapters and nvidia_package is None:
        blocked.update(nvidia50="需要 NVIDIA 显卡", general="需要 NVIDIA 显卡")

    if not radeon_adapters:
        blocked.update({ROCM: "需要 AMD Radeon 显卡", DIRECTML: "需要 AMD Radeon 显卡"})
        return blocked

    if windows_build < 22000:
        blocked[ROCM] = "需要 Windows 11"
    elif not any(_ROCM_RADEON.search(name) for name in radeon_adapters):
        blocked[ROCM] = "本机显卡未列入 ROCm 兼容列表"

    if windows_build < 18362:
        blocked[DIRECTML] = "需要 Windows 10 1903 或更新版本"
    elif amd_directml_adapter() is None:
        blocked[DIRECTML] = "需要支持 DirectX 12 的 AMD 显卡及驱动"
    return blocked


def recommended_package(nvidia: str | None, blocked: dict[str, str]) -> str | None:
    if ROCM not in blocked:
        return ROCM
    if nvidia is not None:
        return nvidia
    return DIRECTML if DIRECTML not in blocked else None
