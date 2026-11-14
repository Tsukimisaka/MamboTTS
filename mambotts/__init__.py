"""MamboTTS v2 — 本地 AI 配音工作站（GPT-SoVITS 黑盒运行时前端）。

Clean-slate v2 package layout (strict unidirectional dependency graph):

    ui / cli -> pipeline -> infra -> domain

``domain/`` is pure Python stdlib and must never import PySide6 or any
third-party package.
"""

import os

APP_VERSION = "v2.0.0"
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
