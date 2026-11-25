"""Hardware requirements for the engine variants offered by the installer."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import subprocess
import sys

from .dxgi import amd_directml_adapter


ROCM = "amd_rocm"
DIRECTML = "amd_directml"

# ROCm 10.1 Windows support and Windows Driver Store version for Adrenalin 26.10.41.05.
# https://rocm.docs.amd.com/en/docs-10.1.0/about/release-notes.html#amd-hardware-support
_ROCM_WINDOWS_BUILD = 26200  # Windows 11 25H2
_ROCM_DRIVER_VERSION = (32, 0, 31041, 5005)
_ROCM_ARCHITECTURES = {
    "gfx1100": ("RX 7900 XTX", "RX 7900 XT", "RX 7900 GRE", "PRO W7900", "PRO W7900 DUAL SLOT",
                "PRO W7800", "PRO W7800 48GB"),
    "gfx1101": ("RX 7800 XT", "RX 7700 XT", "RX 7700", "PRO W7700"),
    "gfx1102": ("RX 7600",),
    "gfx1200": ("RX 9060 XT LP", "RX 9060 XT", "RX 9060", "RX 9050", "RX 9050 (4GB)"),
    "gfx1201": ("RX 9070 XT", "RX 9070 GRE", "RX 9070", "AI PRO R9700S", "AI PRO R9700",
                "AI PRO R9600D", "AI PRO R9600"),
}
_ROCM_MODELS = {model: arch for arch, models in _ROCM_ARCHITECTURES.items() for model in models}


@dataclass(frozen=True)
class RadeonAdapter:
    name: str
    driver_version: str

    @property
    def rocm_architecture(self) -> str | None:
        name = " ".join(self.name.upper().split())
        model = re.search(r"\b(?:RX|AI PRO|PRO) .+$", name)
        return _ROCM_MODELS.get(model.group()) if model else None

    @property
    def has_rocm_driver(self) -> bool:
        parts = self.driver_version.split(".")
        return len(parts) == 4 and all(part.isdecimal() for part in parts) and (
            tuple(map(int, parts)) >= _ROCM_DRIVER_VERSION
        )


def windows_radeon_adapters() -> tuple[RadeonAdapter, ...]:
    if sys.platform != "win32":
        return ()
    command = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        "Get-CimInstance Win32_VideoController -ErrorAction Stop | "
        "Where-Object { $_.PNPDeviceID -match '^PCI\\\\VEN_1002&' } | "
        "Select-Object Name, DriverVersion | ConvertTo-Json -Compress"
    )
    try:
        output = subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            encoding="utf-8", errors="replace", stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=6,
        )
        adapters = json.loads(output) if output.strip() else []
    except (OSError, subprocess.SubprocessError, ValueError):
        return ()
    if isinstance(adapters, dict):
        adapters = [adapters]
    if not isinstance(adapters, list):
        return ()
    return tuple(RadeonAdapter(item["Name"], item.get("DriverVersion") or "")
                 for item in adapters if item.get("Name"))


def windows_build_number() -> int:
    if sys.platform != "win32":
        return 0
    import winreg

    # The kernel32.dll file version can lag behind the OS build on Windows 11.
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as key:
            build, _ = winreg.QueryValueEx(key, "CurrentBuildNumber")
        return int(build)
    except (OSError, TypeError, ValueError):
        return 0


def gpu_incompatibilities(
    nvidia_package: str | None, radeon_adapters: tuple[RadeonAdapter, ...], windows_build: int
) -> dict[str, str]:
    """Return the reason for each choice that this machine cannot use."""
    blocked: dict[str, str] = {}
    if radeon_adapters and nvidia_package is None:
        blocked.update(nvidia50="需要 NVIDIA 显卡", general="需要 NVIDIA 显卡")

    if not radeon_adapters:
        blocked.update({ROCM: "需要 AMD Radeon 显卡", DIRECTML: "需要 AMD Radeon 显卡"})
        return blocked

    supported = [adapter for adapter in radeon_adapters if adapter.rocm_architecture]
    if windows_build < _ROCM_WINDOWS_BUILD:
        blocked[ROCM] = "需要 Windows 11 25H2 或更新版本"
    elif not supported:
        blocked[ROCM] = "本机显卡未列入 ROCm 兼容列表"
    elif not any(adapter.has_rocm_driver for adapter in supported):
        blocked[ROCM] = "需要更新 AMD 显卡驱动"

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
