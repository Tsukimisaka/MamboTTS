"""Single-voice registry: the built-in ``mambo:default`` preset.

The multi-character / multi-emotion import system was removed (product
decision: one Mambo voice is enough).  The class keeps the ``scan`` / ``get``
shape so the pipeline and GUI call sites stay unchanged.

``models/refer.txt`` (non-empty) still overrides the compiled-in reference
text so swapping the reference audio only requires touching the two files in
``models/``.
"""

from __future__ import annotations

import os

from .. import PROJECT_ROOT
from .models import DEFAULT_REF_TEXT, GPT_MODEL, REF_WAV, SOVITS_MODEL, VoicePreset

_BUILTIN_ID = "mambo:default"
REQUIRED_MODEL_FILES = (REF_WAV, GPT_MODEL, SOVITS_MODEL)

__all__ = [
    "REQUIRED_MODEL_FILES",
    "VoiceRepository",
    "builtin_ref_text",
    "check_models_ready",
]


def builtin_ref_text(models_dir: str) -> str:
    """Transcript for the built-in default voice.

    ``models/refer.txt`` (non-empty) wins so swapping the reference audio
    only requires touching the two files in models/; fall back to the
    compiled-in DEFAULT_REF_TEXT otherwise. An empty file must NOT be
    used (an empty ref_text makes the engine skip the reference audio
    entirely, silently producing the wrong voice).
    """
    candidate = os.path.join(models_dir, "refer.txt")
    try:
        with open(candidate, "r", encoding="utf-8-sig") as fh:
            text = fh.read().strip()
        if text:
            return text
    except OSError:
        pass
    return DEFAULT_REF_TEXT


def check_models_ready(project_dir: str = PROJECT_ROOT) -> tuple[bool, list[str]]:
    """Return ``(ready, missing)`` for the required voice files in ``models/``."""
    models_dir = os.path.join(os.path.abspath(project_dir), "models")
    missing = [
        name
        for name in REQUIRED_MODEL_FILES
        if not os.path.isfile(os.path.join(models_dir, name))
    ]
    return not missing, missing


class VoiceRepository:
    """Provides the one and only ``mambo:default`` voice preset."""

    def __init__(self, project_dir: str = PROJECT_ROOT):
        self.project_dir = os.path.abspath(project_dir)
        self.models_dir = os.path.abspath(os.path.join(self.project_dir, "models"))
        self._preset = self._build_preset()

    # ------------------------------------------------------------------
    # Preset
    # ------------------------------------------------------------------

    def _build_preset(self) -> VoicePreset:
        return VoicePreset(
            id=_BUILTIN_ID,
            character="mambo",
            emotion="default",
            ref_wav=os.path.abspath(os.path.join(self.models_dir, REF_WAV)),
            ref_text=builtin_ref_text(self.models_dir),
            ref_lang="zh",
            gpt_model=os.path.abspath(os.path.join(self.models_dir, GPT_MODEL)),
            sovits_model=os.path.abspath(os.path.join(self.models_dir, SOVITS_MODEL)),
        )

    def scan(self) -> list[VoicePreset]:
        """Return the built-in preset, refreshing models/refer.txt content."""
        self._preset = self._build_preset()
        return [self._preset]

    def get(self, query: str = "") -> VoicePreset:
        """Resolve any query to the built-in preset, refreshing models/refer.txt."""
        self._preset = self._build_preset()
        return self._preset
