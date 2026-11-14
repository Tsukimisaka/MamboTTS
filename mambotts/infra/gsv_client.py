"""Black-Box GPT-SoVITS HTTP Gateway（spec item 9）。

只通过 HTTP 与外部 GPT-SoVITS 运行时（127.0.0.1:9880，云端整合包内 api.py）
通信，绝不 import 引擎内部代码、绝不修改 GPT-SoVITS/ 目录。

关键设计（对应 spec「Key Architectural Invariants」）：
1. Proxy Immunity：所有请求走 requests.Session 且 trust_env=False，并逐请求
   显式传 proxies={"http": None, "https": None}，本地代理工具（Clash 等）
   既劫持不了 localhost 引擎流量，也影响不了测速/下载。
2. VRAM 防抖：_active_models 预初始化为默认 models/ 权重的规范化绝对路径，
   默认音色合成永远不会触发 POST /set_model 热切换。
3. 流式落盘：合成响应按 65536 字节块写入 <save>.<pid>.<tid>.part，
   每块检查 cancel_event，最终 looks_like_wav 校验 + 原子 replace。
"""
from __future__ import annotations

import logging
import math
import os
import threading
from typing import Any

import requests

# PROJECT_ROOT 的单一事实来源是包根 mambotts/__init__.py（spec item 1）
from mambotts import PROJECT_ROOT
from mambotts.domain.models import (
    CUT_PUNC,
    DEFAULT_API_URL,
    DEFAULT_REF_TEXT,
    GPT_MODEL,
    REF_WAV,
    SPEED_MAX,
    SPEED_MIN,
    SOVITS_MODEL,
    SUPPORTED_LANGS,
    VoicePreset,
)
from mambotts.domain.storage_repo import remove_quiet, replace_with_retry
from mambotts.domain.voice_repo import builtin_ref_text

logger = logging.getLogger("mambotts.infra.gsv_client")

# detect_language 只在这些「待判定」语种下生效；显式指定 en/ja 等时不改写
_AUTO_DETECT_LANGS = ("zh", "auto_detect", "")
_TTS_CHUNK_SIZE = 65536          # 合成流式落盘块大小（spec 规定 65536）
_WAV_SNIFF_BYTES = 8192          # WAV 结构嗅探窗口

_NO_PROXY = {"http": None, "https": None}


class _DiskSaveError(Exception):
    """Wrap local file open/write OSError so HTTP RequestException (also OSError) never conflates."""


def _record_engine_error(project_dir: str, message: str) -> None:
    """Persist HTTP/stream diagnostic lines into project ``engine.log``."""
    try:
        from mambotts.infra.engine_mgr import append_engine_log

        append_engine_log(f"[HTTP] {message}", base_dir=project_dir)
    except Exception:
        pass


def _classify_disconnect_reason(project_dir: str) -> str:
    """Inspect recent ``engine.log`` lines to separate BERT-512 overflow from VRAM OOM."""
    tail = ""
    try:
        from mambotts.infra.engine_mgr import engine_log_path

        log_path = engine_log_path(project_dir)
        if os.path.isfile(log_path):
            size = os.path.getsize(log_path)
            with open(log_path, "rb") as fh:
                if size > 8192:
                    fh.seek(size - 8192)
                tail = fh.read().decode("utf-8", errors="replace").lower()
    except OSError:
        tail = ""

    if "get_bert_feature" in tail or "position_embeddings" in tail or "size of tensor b (512)" in tail:
        return (
            "引擎触发文本编码长度上限（BERT 512 token），"
            "请增加标点断句或缩短单段文本长度后重试（详情见 engine.log）。"
        )
    if "out of memory" in tail or "cuda error" in tail:
        return (
            "引擎推理时显存不足（CUDA OOM）或显卡驱动异常，"
            "建议关闭其他占用显存的程序、缩短文案后重试（详情见 engine.log）。"
        )
    return (
        "引擎在合成中途断开了连接（可能是单段文本触发 BERT 长度/时长上限、"
        "显存不足或引擎进程异常）。\n建议增加标点分段并查看 engine.log 定位。"
    )


def _finite_clamp(val: Any, default: float, lo: float, hi: float) -> float:
    """Parse float and clamp finite values into [lo, hi], else return default."""
    try:
        num = float(val)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(num):
        return default
    return max(lo, min(hi, num))


def _is_japanese(t: str) -> bool:
    """检测文本是否含日语假名（平假名/片假名，含中点/长音——legacy 语义）。"""
    for char in t or "":
        cp = ord(char)
        if 0x3040 <= cp <= 0x309F or 0x30A0 <= cp <= 0x30FF:
            return True
    return False


def detect_language(text: str, default_lang: str = "zh") -> str:
    """按假名占比识别中文文案中混入的日语（比例式判定，防误伤）。

    只有当假名（排除中点 U+30FB 与长音符号 U+30FC）同时满足
    「最少个数」与「占非空白字符比例」双门槛时才切到日语：
    - 常规文本：kana >= 2 且 kana / 非空白 >= 0.20；
    - 超短文本（<= 4 字，如「私は」）：kana >= 1 且 kana / 非空白 >= 0.50。
    避免类似「阿尼亚・福杰」（仅一个中点符号）被误判为日语。
    """
    if default_lang not in _AUTO_DETECT_LANGS:
        return default_lang
    text = text or ""
    kana_count = 0
    non_ws = 0
    for char in text:
        if char.isspace():
            continue
        non_ws += 1
        cp = ord(char)
        if (0x3040 <= cp <= 0x309F or 0x30A0 <= cp <= 0x30FF) and cp not in (0x30FB, 0x30FC):
            kana_count += 1
    if non_ws <= 0:
        return "zh"
    if (kana_count >= 2 and kana_count / non_ws >= 0.20) or (
        len(text.strip()) <= 4 and kana_count >= 1 and kana_count / non_ws >= 0.50
    ):
        return "ja"
    return "zh"


def looks_like_wav(data: bytes) -> bool:
    """WAV 结构校验：防止把 HTTP 200 的 JSON 错误页存成 .wav。

    按 RIFF chunk 链逐块遍历头 8KB，确认存在 data 块且其声明长度 > 0。
    流式落盘后不可能拿全量做长度自洽校验，只读文件头即可判定：
    JSON 错误页 / 截断假头在此都会被拒绝。"""
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return False
    pos = 12
    limit = min(len(data), _WAV_SNIFF_BYTES)
    while pos + 8 <= limit:
        chunk_id = data[pos:pos + 4]
        try:
            chunk_size = int.from_bytes(data[pos + 4:pos + 8], "little")
        except Exception:
            return False
        if chunk_id == b"data":
            # 音频数据必须真实存在：声明非空即认可（完整性交给上层可选校验）
            return chunk_size > 0
        # 跳过整个 chunk（可能很大，比如 LIST 头）——跳到下一块
        pos += 8 + chunk_size + (chunk_size & 1)
    # 头 8KB 内没找到 data 块：可疑（正常 WAV 不会这样）
    return False


# legacy 别名（旧脚本/旧用例引用 _looks_like_wav）
_looks_like_wav = looks_like_wav


def _check_writable_dir(save_path: str) -> str | None:
    """写文件前预检：父目录必须存在且可写。返回错误信息或 None。"""
    parent_dir = os.path.dirname(save_path) or "."
    if not os.path.exists(parent_dir):
        return f"输出目录不存在：{parent_dir}\n请点击「浏览...」重新选择保存路径。"
    if not os.path.isdir(parent_dir):
        return f"输出路径不是文件夹：{parent_dir}"
    if not os.access(parent_dir, os.W_OK):
        return f"没有写入权限的目录：{parent_dir}\n请选择其他位置，或用管理员身份运行。"
    return None


class GSVHttpClient:
    """GPT-SoVITS 本地 API（127.0.0.1:9880）的 HTTP 网关。

    对引擎的唯一契约（与整合包内 api.py 核对过）：
    - GET  /control    → 任意可解析 JSON 即视为引擎在线；
    - POST /set_model  → {"gpt_model_path", "sovits_model_path"} 热切换权重；
    - POST /           → 旧版字段 refer_wav_path / prompt_text / prompt_language /
                         text / text_language / speed / cut_punc / top_k / top_p /
                         temperature，响应体为流式 WAV。
    """

    def __init__(self, api_url: str = DEFAULT_API_URL, project_dir: str = PROJECT_ROOT):
        self.api_url = api_url
        self.project_dir = project_dir
        # 强制直连、绕过系统代理：trust_env=False + 逐请求显式 proxies 双保险
        self._session = requests.Session()
        self._session.trust_env = False

        models_dir = os.path.join(project_dir, "models")
        self._default_gpt = os.path.abspath(os.path.join(models_dir, GPT_MODEL))
        self._default_sovits = os.path.abspath(os.path.join(models_dir, SOVITS_MODEL))
        self._default_ref_audio = os.path.abspath(os.path.join(models_dir, REF_WAV))
        # 预初始化为默认 models/ 权重：默认音色合成不会触发 /set_model（防 VRAM 尖峰）
        self._active_models: tuple[str, str] = (self._default_gpt, self._default_sovits)

    def reset_active_models(self) -> None:
        """Reset tracked active weights so a restarted engine reloads cleanly."""
        self._active_models = (self._default_gpt, self._default_sovits)

    def invalidate_active_models(self) -> None:
        """Clear cached model assumptions (forces /set_model on remote/switched URL)."""
        self._active_models = None  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # 基础探测
    # ------------------------------------------------------------------

    def is_api_running(self, timeout: float = 1.5, api_url: str | None = None) -> bool:
        """检查本地 GPT-SoVITS API 服务是否在线。

        判定依据：对 GET /control 的任何响应——正常 JSON、参数校验 422、
        路由 404——响应体都是 JSON（无参数时甚至就是字面量 null）。因此
        「能拿到可解析的 JSON」即可确认端口上是引擎；返回 HTML/明文
        （反代、其他本地服务占端口）判为离线，避免后续合成全部报怪错。"""
        base_url = (api_url or self.api_url).rstrip("/")
        try:
            response = self._session.get(f"{base_url}/control", timeout=timeout,
                                         proxies=_NO_PROXY)
        except requests.RequestException:
            return False
        try:
            response.json()
        except ValueError:
            return False
        return True

    def switch_model_if_needed(self, gpt_path: str, sovits_path: str,
                               api_url: str | None = None) -> tuple[bool, str]:
        """按需 POST /set_model 热切换权重。

        规范化（os.path.abspath）后的 (gpt, sovits) 与 _active_models 相同
        则直接跳过（默认音色零开销）；切换成功（HTTP 200）才更新
        _active_models 缓存。空路径回落到默认 models/ 权重。"""
        abs_gpt = os.path.abspath(gpt_path) if gpt_path else self._default_gpt
        abs_sovits = os.path.abspath(sovits_path) if sovits_path else self._default_sovits
        target = (abs_gpt, abs_sovits)
        if target == self._active_models:
            return True, "模型未变化，跳过切换。"

        base_url = (api_url or self.api_url).rstrip("/")
        payload = {"gpt_model_path": abs_gpt, "sovits_model_path": abs_sovits}
        try:
            response = self._session.post(f"{base_url}/set_model", json=payload,
                                          timeout=(10, 60), proxies=_NO_PROXY)
        except requests.RequestException as e:
            logger.warning("POST /set_model 请求失败: %s", e)
            return False, f"切换模型请求失败：{type(e).__name__}: {e}"
        if response.status_code == 200:
            self._active_models = target
            logger.info("已切换引擎模型: gpt=%s sovits=%s", abs_gpt, abs_sovits)
            return True, "模型切换成功。"
        body = (response.text or "")[:300]
        logger.error("POST /set_model 失败（HTTP %s）: %s", response.status_code, body)
        return False, f"切换模型失败（状态码 {response.status_code}）：\n{body}"

    # ------------------------------------------------------------------
    # 合成
    # ------------------------------------------------------------------

    def synthesize_clause(self, text: str, save_path: str, preset: VoicePreset,
                          speed: float = 1.0, text_lang: str = "zh",
                          api_url: str | None = None, cut_punc: str = CUT_PUNC,
                          temperature: float = 0.8, top_p: float = 0.8,
                          top_k: int = 15,
                          cancel_event: threading.Event | None = None,
                          check_online: bool = True) -> tuple[bool, str]:
        """合成单个短句并流式落盘到 save_path。返回 (success, message)。

        流程：目录可写预检 → 语种校验（zh 文案自动按假名占比纠偏）→
        在线校验 → /set_model 按需切换 → 流式写 .part（每块检查取消）→
        looks_like_wav 校验 → 原子 replace（PermissionError 退避重试）。"""
        if not save_path:
            return False, "保存路径为空，请选择有效的输出路径。"
        err = _check_writable_dir(save_path)
        if err:
            return False, err
        if cancel_event is not None and cancel_event.is_set():
            return False, "合成已取消。"

        # 自动语种纠偏：中文模式下检测到足够假名占比才切日语（比例式判定）
        if text_lang in _AUTO_DETECT_LANGS:
            text_lang = detect_language(text, text_lang or "zh")
        if text_lang not in SUPPORTED_LANGS:
            return False, (f"不支持的文本语种: {text_lang}\n"
                           "支持的语种: zh/en/ja/ko/yue 等")

        if preset is None:
            preset = VoicePreset(
                id="mambo:default",
                character="mambo",
                emotion="default",
                ref_wav=self._default_ref_audio,
                ref_text=builtin_ref_text(
                    os.path.dirname(self._default_ref_audio)
                ),
                ref_lang="zh",
                gpt_model="",
                sovits_model="",
            )

        # 参考音频语种校验（参考文本混入假名时同样纠偏）
        prompt_lang = preset.ref_lang or "zh"
        if prompt_lang in _AUTO_DETECT_LANGS and _is_japanese(preset.ref_text):
            prompt_lang = "ja"
        if prompt_lang not in SUPPORTED_LANGS:
            prompt_lang = "zh"

        base_url = (api_url or self.api_url).rstrip("/")
        if check_online and not self.is_api_running(api_url=base_url):
            logger.warning("GPT-SoVITS API 处于离线状态，无法合成。")
            return False, ("本地 GPT-SoVITS 引擎尚未就绪，无法合成配音。\n"
                           "请先启动引擎（或勾选自动启动），待引擎状态显示在线后再试。")

        speed = _finite_clamp(speed, 1.0, SPEED_MIN, SPEED_MAX)
        top_p = _finite_clamp(top_p, 0.8, 0.05, 1.0)
        temperature = _finite_clamp(temperature, 0.8, 0.05, 2.0)
        try:
            top_k = max(1, min(100, int(top_k)))
        except (TypeError, ValueError):
            top_k = 15

        # 引擎模型权重：preset 未指定时回落默认 models/，规范化后再比对，
        # 与 _active_models 一致即不发 /set_model（防 VRAM 尖峰）
        ok, msg = self.switch_model_if_needed(
            preset.gpt_model, preset.sovits_model, api_url=base_url)
        if not ok:
            return False, msg

        # api.py（整合包旧版字段）请求体；POST 而非 GET：避免长文案 URL
        # 编码后超长触发 h11 414/400
        params = {
            "text": text,
            "text_language": text_lang,
            "speed": speed,
            "cut_punc": cut_punc,
            "top_k": top_k,
            "top_p": top_p,
            "temperature": temperature,
        }
        if preset.ref_wav and preset.ref_text:
            params["refer_wav_path"] = preset.ref_wav
            params["prompt_text"] = preset.ref_text
            params["prompt_language"] = prompt_lang

        # 读超长按文本量伸缩：约每字 1 秒，下限 120s，上限 600s
        read_timeout = min(600.0, max(120.0, len(text) * 1.0))
        part_path = f"{save_path}.{os.getpid()}.{threading.get_ident()}.part"

        logger.info("正在请求 GPT-SoVITS API 合成文本: %s...", text[:20])
        if cancel_event is not None and cancel_event.is_set():
            return False, "合成已取消。"
        promoted = False
        try:
            try:
                with self._session.post(f"{base_url}/", json=params, stream=True,
                                        timeout=(10, read_timeout),
                                        proxies=_NO_PROXY) as response:
                    if response.status_code != 200:
                        err_body = ""
                        try:
                            err_body = response.text[:300]
                        except Exception:
                            pass
                        logger.error("API 响应失败，状态码: %s, 内容: %s",
                                     response.status_code, err_body)
                        _record_engine_error(
                            self.project_dir,
                            f"API 响应失败 HTTP {response.status_code}: {err_body or '(empty)'}",
                        )
                        return False, (f"API 返回错误（状态码 {response.status_code}）：\n"
                                       f"{err_body or '(无响应内容)'}")
                    canceled = False
                    try:
                        fobj = open(part_path, "wb")
                    except OSError as e:
                        raise _DiskSaveError(e) from e
                    try:
                        for chunk in response.iter_content(chunk_size=_TTS_CHUNK_SIZE):
                            if cancel_event is not None and cancel_event.is_set():
                                canceled = True
                                break
                            if chunk:
                                try:
                                    fobj.write(chunk)
                                except OSError as e:
                                    raise _DiskSaveError(e) from e
                        try:
                            fobj.flush()
                        except OSError as e:
                            raise _DiskSaveError(e) from e
                    finally:
                        try:
                            fobj.close()
                        except OSError:
                            pass
                    if canceled:
                        logger.info("合成已取消，已清理临时文件: %s", part_path)
                        return False, "合成已取消。"
            except requests.exceptions.ConnectionError:
                return False, (f"无法连接到 API 服务：{base_url}\n"
                               "引擎可能正在退出，请等待引擎状态显示在线后重试。")
            except requests.exceptions.ChunkedEncodingError:
                logger.error("API 响应中途断开（ChunkedEncodingError）。")
                _record_engine_error(self.project_dir, "ChunkedEncodingError: API 响应中途断开")
                return False, _classify_disconnect_reason(self.project_dir)
            except requests.exceptions.Timeout:
                _record_engine_error(self.project_dir, f"Timeout: API 请求超时 ({int(read_timeout)}s)")
                return False, (f"API 请求超时（{int(read_timeout)}秒），文案过长或显卡负载过高，"
                               "可分段合成后拼接。")
            except requests.exceptions.RequestException as e:
                logger.error("API HTTP 通信异常：%s", e)
                _record_engine_error(self.project_dir, f"{type(e).__name__}: {e}")
                return False, (f"引擎 HTTP 通信异常：{e}\n"
                               "可查看 engine.log 定位原因，或尝试重启引擎。")
            except _DiskSaveError as e:
                return False, f"保存文件失败：{e}\n路径：{save_path}"

            # 写完再校验 WAV 结构：流式模式下无法提前看 content
            try:
                with open(part_path, "rb") as f:
                    head = f.read(_WAV_SNIFF_BYTES)
            except OSError:
                head = b""
            if not looks_like_wav(head):
                snippet = f"(文件大小 {os.path.getsize(part_path)} 字节)" \
                    if os.path.exists(part_path) else "(空文件)"
                logger.error("API 返回 200 但内容不是有效 WAV：%s", snippet)
                _record_engine_error(self.project_dir, f"无效 WAV 响应 {snippet}")
                return False, ("引擎返回了无效音频数据（状态码 200）。\n"
                               "请查看 engine.log 排查模型/参考音频配置。")
            try:
                replace_with_retry(part_path, save_path)
            except OSError as e:
                return False, (f"文件被占用或无写入权限：{save_path}\n"
                               f"请关闭正在使用该文件的程序后重试。({e})")
            promoted = True
            logger.info("语音合成成功，已保存至: %s", save_path)
            return True, save_path
        finally:
            if not promoted:
                remove_quiet(part_path)

    # ------------------------------------------------------------------
    # legacy 兼容包装
    # ------------------------------------------------------------------

    def generate_speech(self, text: str, save_path: str, ref_audio: str = "",
                        prompt_text: str = "", text_lang: str = "zh",
                        prompt_lang: str = "zh", speed: float = 1.0,
                        api_url: str | None = None) -> tuple[bool, str]:
        """v1 风格的合成入口（兼容旧调用签名）。

        把零散的 ref_audio / prompt_text 参数组装成临时 VoicePreset 后
        委托 synthesize_clause：未传参考音频时回落默认 models/refer.wav +
        DEFAULT_REF_TEXT，未传提示文本时同样回落默认转写。"""
        preset = VoicePreset(
            id="mambo:default", character="mambo", emotion="default",
            ref_wav=ref_audio or self._default_ref_audio,
            ref_text=prompt_text or DEFAULT_REF_TEXT,
            ref_lang=prompt_lang or "zh",
            gpt_model="", sovits_model="",
        )
        return self.synthesize_clause(text, save_path, preset, speed=speed,
                                      text_lang=text_lang, api_url=api_url)


# legacy 别名：v1 的 TTSClient 即 v2 的 GSVHttpClient
TTSClient = GSVHttpClient
