"""Find the AMD adapter index used by ONNX Runtime's DirectML provider."""

from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import sys
from uuid import UUID


class _AdapterDescription(C.Structure):
    _fields_ = [
        ("description", W.WCHAR * 128),
        ("vendor_id", W.UINT),
        ("device_id", W.UINT),
        ("subsystem_id", W.UINT),
        ("revision", W.UINT),
        ("video_memory", C.c_size_t),
        ("system_memory", C.c_size_t),
        ("shared_memory", C.c_size_t),
        ("luid_low", W.DWORD),
        ("luid_high", W.LONG),
        ("flags", W.UINT),
    ]


def _method(interface, slot, result, *arguments):
    table = C.cast(interface, C.POINTER(C.POINTER(C.c_void_p))).contents
    return C.WINFUNCTYPE(result, C.c_void_p, *arguments)(table[slot])


def _guid(value: str):
    return (C.c_ubyte * 16).from_buffer_copy(UUID(value).bytes_le)


def amd_directml_adapter() -> int | None:
    """Select a hardware AMD D3D12 adapter, preferring dedicated video memory."""
    if sys.platform != "win32":
        return None
    try:
        create_factory = C.WinDLL("dxgi").CreateDXGIFactory1
        create_device = C.WinDLL("d3d12").D3D12CreateDevice
    except (AttributeError, OSError):
        return None

    create_factory.argtypes = (C.c_void_p, C.POINTER(C.c_void_p))
    create_factory.restype = W.LONG
    create_device.argtypes = (C.c_void_p, W.UINT, C.c_void_p, C.c_void_p)
    create_device.restype = W.LONG
    iid_factory = _guid("770aae78-f26f-4dba-a829-253c83d1b387")
    iid_device = _guid("189819f1-1db6-4b57-be54-1821339b85f7")
    factory = C.c_void_p()
    if create_factory(C.byref(iid_factory), C.byref(factory)) < 0:
        return None

    eligible = []
    enum_adapters = _method(factory, 12, W.LONG, W.UINT, C.POINTER(C.c_void_p))  # EnumAdapters1
    try:
        index = 0
        while True:
            adapter = C.c_void_p()
            if enum_adapters(factory, index, C.byref(adapter)) < 0:
                break
            try:
                desc = _AdapterDescription()
                get_description = _method(adapter, 10, W.LONG, C.POINTER(_AdapterDescription))
                if (get_description(adapter, C.byref(desc)) == 0
                        and desc.vendor_id == 0x1002 and not desc.flags & 2
                        and create_device(adapter, 0xB000, C.byref(iid_device), None) >= 0):
                    eligible.append((desc.video_memory, -index))
            finally:
                _method(adapter, 2, W.ULONG)(adapter)  # Release
            index += 1
    finally:
        _method(factory, 2, W.ULONG)(factory)
    return -max(eligible)[1] if eligible else None
