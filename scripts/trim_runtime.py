# -*- coding: utf-8 -*-
"""trim_runtime.py - Strip unused Qt modules from the bundled PySide6.

The app uses ONLY the Widgets stack + Multimedia + Svg. Everything else
(3D, QML/Quick, WebEngine, Charts, Pdf, Sensors, ...) is dead weight.

Kept DLL whitelist (Qt6*): Core, Gui, Widgets, Network, Multimedia,
MultimediaWidgets, OpenGL, OpenGLWidgets, Svg, SvgWidgets, Concurrent, Xml,
Dlls from the FFmpeg backend (avcodec/avformat/avutil/swresample/swscale),
opengl32sw (software GL fallback) and the VC runtime family.

Run AFTER staging, BEFORE compress:  python scripts/trim_runtime.py [STAGE_DIR]
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

_DEFAULT_STAGE = Path(__file__).resolve().parents[1] / "release" / "MamboTTS"

KEEP_DLLS = {
    "Qt6Core.dll", "Qt6Gui.dll", "Qt6Widgets.dll", "Qt6Network.dll",
    "Qt6Multimedia.dll", "Qt6MultimediaWidgets.dll",
    "Qt6OpenGL.dll", "Qt6OpenGLWidgets.dll",
    "Qt6Svg.dll", "Qt6SvgWidgets.dll",
    "Qt6Concurrent.dll", "Qt6Xml.dll", "Qt6Test.dll",
    "avcodec-61.dll", "avformat-61.dll", "avutil-59.dll",
    "swresample-5.dll", "swscale-8.dll",
    "opengl32sw.dll", "pyside6.abi3.dll",
    "concrt140.dll", "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
    "msvcp140_codecvt_ids.dll", "vcamp140.dll", "vccorlib140.dll",
    "vcomp140.dll", "vcruntime140.dll", "vcruntime140_1.dll",
}
KEEP_PLUGINS = {
    "platforms", "imageformats", "multimedia", "iconengines", "styles",
    "tls", "networkinformation", "platforminputcontexts", "generic",
}
KEEP_TRANSLATIONS = {"qtbase_zh_CN.qm", "qtbase_zh_TW.qm"}
DELETE_ROOT_ENTRIES = {"qml", "metatypes", "Qt", "exe", "lib", "include", "Sources"}


def human(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


def main() -> None:
    stage = Path(sys.argv[1]) if len(sys.argv) > 1 else _DEFAULT_STAGE
    pyside = stage / "runtime" / "site-packages" / "PySide6"
    assert pyside.exists(), f"staging missing: {pyside}"
    saved = 0

    # 1) root Qt6*.dll whitelist
    for name in os.listdir(pyside):
        fp = pyside / name
        if name.endswith(".dll") and name.startswith("Qt6") and name not in KEEP_DLLS:
            saved += fp.stat().st_size
            fp.unlink()
    print(f"root DLLs trimmed: {human(saved)}")

    # 2) pyside6qml binding (only needed by PySide6.QtQml)
    qml_binding = pyside / "pyside6qml.abi3.dll"
    if qml_binding.exists():
        saved += qml_binding.stat().st_size
        qml_binding.unlink()

    # 3) whole directories
    for entry in ("qml", "metatypes", "Qt", "lib", "include", "Sources", "exe"):
        fp = pyside / entry
        if fp.exists():
            size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(fp) for f in fs)
            shutil.rmtree(fp, ignore_errors=True)
            saved += size
    print(f"qml/metatypes/etc trimmed")

    # 4) translations: keep only qtbase_zh_*.qm
    tr = pyside / "translations"
    if tr.exists():
        size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(tr) for f in fs)
        for root, dirs, files in os.walk(tr, topdown=False):
            for f in files:
                if f not in KEEP_TRANSLATIONS:
                    os.remove(os.path.join(root, f))
            for d in dirs:
                p = Path(root) / d
                if not any(p.iterdir()):
                    p.rmdir()
        kept_size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(tr) for f in fs)
        saved += size - kept_size
    print(f"translations trimmed (kept qtbase_zh_CN/TW)")

    # 5) resources: keep only icudtl.dat (drop 85+MB of WebEngine paks)
    res = pyside / "resources"
    if res.exists():
        for f in os.listdir(res):
            if f != "icudtl.dat":
                fp = res / f
                if fp.is_file():
                    saved += fp.stat().st_size
                    fp.unlink()
                elif fp.is_dir():
                    shutil.rmtree(fp, ignore_errors=True)
    print(f"resources trimmed (kept icudtl.dat)")

    # 6) plugins whitelist
    pl = pyside / "plugins"
    if pl.exists():
        for d in os.listdir(pl):
            fp = pl / d
            if fp.is_dir() and d not in KEEP_PLUGINS:
                size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(fp) for f in fs)
                shutil.rmtree(fp, ignore_errors=True)
                saved += size
    print(f"plugins trimmed")

    # 7) top-level junk in the app root (exact names + pattern backups/screenshots)
    junk_dirs = {
        ".commandcode",
        ".ui_backup_round2_20261106",
        ".ui_backup_tag_20261106",
        "taste",
        "ui_screenshots_20261106",
        "ui_screenshots_round2_20261106",
        "ui_screenshots_tag_20261106",
        ".archify",
    }
    for entry in os.listdir(stage):
        if entry.startswith((".ui_backup_", "ui_screenshots_")):
            junk_dirs.add(entry)
    for junk in sorted(junk_dirs):
        fp = stage / junk
        if fp.exists():
            shutil.rmtree(fp, ignore_errors=True)
            print(f"app-root junk removed: {junk}")
    for f in ("MamboTTS 界面第二轮评估与优化方向文档.md",
              "MamboTTS 界面高级感优化方向文档.md",
              ".gitignore"):
        fp = stage / f
        if fp.exists():
            fp.unlink()

    print(f"TOTAL SAVED: {human(saved)}")


if __name__ == "__main__":
    sys.exit(main())
