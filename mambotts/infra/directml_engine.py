"""ONNX Runtime integration for the bundled SoVITS v4 acoustic decoder."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile

import onnxruntime as ort
import torch


_PROVIDER = "DmlExecutionProvider"
_CACHE = Path(__file__).resolve().parent / ".mambo_onnx"
_EXPORT_VERSION = b"mambo-sovits-v4-1"


class DenoisingStep(torch.nn.Module):
    """Export one step while leaving the upstream diffusion loop in place."""

    def __init__(self, estimator):
        super().__init__()
        self.estimator = estimator

    def forward(self, signal, prompt, length, time, step, features):
        return self.estimator(
            signal, prompt, length, time, dt_base_bootstrap=step, text0=features,
        )


def _cache_key(*weights_paths: str) -> str:
    digest = hashlib.sha256(_EXPORT_VERSION)
    for path in weights_paths:
        with open(path, "rb") as weights:
            digest.update(hashlib.file_digest(weights, "sha256").digest())
    return digest.hexdigest()[:24]


def _export(model, sample, graph, inputs, output, axes):
    graph.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=graph.parent) as directory:
        temp_graph = Path(directory) / graph.name
        with torch.no_grad():
            torch.onnx.export(
                model, sample, os.fspath(temp_graph), opset_version=18, dynamo=False,
                input_names=inputs, output_names=[output], dynamic_axes=axes,
                external_data=False,
            )
        os.replace(temp_graph, graph)


def _open_session(graph: Path) -> ort.InferenceSession:
    from _mambo_dxgi import amd_directml_adapter

    adapter = amd_directml_adapter()
    if adapter is None or _PROVIDER not in ort.get_available_providers():
        raise RuntimeError("未找到可用于 DirectML 的 AMD 显卡。")
    options = ort.SessionOptions()
    options.enable_mem_pattern = False
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    session = ort.InferenceSession(
        os.fspath(graph), sess_options=options, providers=[(_PROVIDER, {"device_id": str(adapter)})],
    )
    session.disable_fallback()
    if session.get_providers()[0] != _PROVIDER:
        raise RuntimeError("DirectML 无法加载 SoVITS 推理模型。")
    return session


def _predict(session, names, values):
    inputs = {name: value.detach().contiguous().numpy() for name, value in zip(names, values)}
    return torch.from_numpy(session.run(None, inputs)[0])


def attach_acoustic_model(model, weights_path, base_weights_path=None):
    estimator = model.cfm.estimator
    weights = (weights_path, base_weights_path) if base_weights_path else (weights_path,)
    graph = _CACHE / f"dit-{_cache_key(*weights)}.onnx"
    input_names = ["signal", "prompt", "length", "time", "step", "features"]
    new_graph = not graph.is_file()
    if new_graph:
        frames = 48
        mel = torch.zeros(1, 100, frames)
        example = (mel, mel.clone(), torch.tensor([frames]), torch.zeros(1),
                   torch.ones(1), torch.zeros(1, 512, frames))
        axes = {name: {2: "frames"} for name in ("signal", "prompt", "features")}
        axes["prediction"] = {1: "frames"}
        _export(DenoisingStep(estimator).eval(), example, graph, input_names, "prediction", axes)
    session = _open_session(graph)
    if new_graph:
        _predict(session, input_names, example)

    def forward(signal, prompt, length, time, dt_base_bootstrap=None, text0=None, *,
                infer=False, drop_audio_cond=False, drop_text=False, **_):
        if drop_audio_cond or drop_text:
            raise RuntimeError("DirectML 推理不支持此 SoVITS 引导模式。")
        values = (signal, prompt, length, time, dt_base_bootstrap, text0)
        prediction = _predict(session, input_names, values)
        return (prediction, None, None) if infer else prediction

    estimator.forward = forward


def attach_vocoder(model, weights_path):
    graph = _CACHE / f"vocoder-{_cache_key(weights_path)}.onnx"
    new_graph = not graph.is_file()
    if new_graph:
        example = (torch.zeros(1, 100, 48),)
        _export(model, example, graph, ["mel"], "audio",
                {"mel": {2: "frames"}, "audio": {2: "samples"}})
    session = _open_session(graph)
    if new_graph:
        _predict(session, ("mel",), example)

    def forward(mel):
        return _predict(session, ("mel",), (mel,))

    model.forward = forward
