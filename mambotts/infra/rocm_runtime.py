"""Select and exercise the AMD device with the installed ROCm kernels."""

from __future__ import annotations

import torch


def select_device(architecture: str) -> str:
    if not torch.version.hip or not torch.cuda.is_available():
        raise RuntimeError("ROCm 无法使用 AMD 显卡，请检查 AMD 显卡驱动。")
    for index in range(torch.cuda.device_count()):
        properties = torch.cuda.get_device_properties(index)
        if properties.gcnArchName.split(":", 1)[0] == architecture:
            return f"cuda:{index}"
    raise RuntimeError(f"ROCm 未识别到适配 {architecture} 的 AMD 显卡。")


def verify_runtime(architecture: str) -> None:
    import torchaudio
    import torchvision

    device = select_device(architecture)
    with torch.inference_mode():
        operation = torch.nn.Conv1d(4, 4, 3).to(device=device, dtype=torch.float16)
        sample = torch.ones(1, 4, 16, dtype=torch.float16, device=device)
        if not torch.isfinite(operation(sample)).all().item():
            raise RuntimeError("ROCm 半精度卷积校验失败。")
