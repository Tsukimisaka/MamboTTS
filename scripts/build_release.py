# -*- coding: utf-8 -*-
"""build_release.py - Build the self-contained portable release zip.

Usage:  python scripts/build_release.py [--out DIR]

What it does (per EGM release checklist):
1. Stage the app tree with robocopy /XJ (never follow the GPT-SoVITS/models
   junctions), excluding dev-only artifacts.
2. Copy the REAL model files from the junction target (deref) into staging.
3. Verify staging has zero junctions and models byte-sizes are exact.
4. Verify the bundled runtime imports (PySide6+requests) and the automated test
   suite passes under runtime\\python.exe (portable release gate).
5. Compress to MamboTTS-v2.0.0-portable.zip and assert the zip contains no
   venv / GPT-SoVITS / config.json / history.json / __pycache__.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
# refer.txt must ship with refer.wav: the builtin default voice reads its
# reference transcript from models/refer.txt -- without it the release
# falls back to DEFAULT_REF_TEXT and the reference audio/transcript pair
# no longer matches (swallowed syllables, broken timbre).
MODELS = ["manbo-e10.ckpt", "manbo_e8_s168.pth", "refer.wav", "refer.txt"]
# Byte-exact tables rot the moment a model is retrained (they already did
# once). The real invariants are: (1) staged copy == repo source byte for
# byte, and (2) weights are real model files, not junction stubs / LFS
# pointers -- enforced via the floor below. sizes: ckpt 155MB, pth 75MB,
# wav 536KB, txt tiny.
MIN_MODEL_BYTES = {
    "manbo-e10.ckpt": 100_000_000,
    "manbo_e8_s168.pth": 50_000_000,
    "refer.wav": 100_000,
    "refer.txt": 1,
}
EXCLUDE_DIRS = {
    ".venv",
    "GPT-SoVITS",
    "GPT-SoVITS._staging",
    "GPT-SoVITS._old",
    "engine_temp",
    "tests",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".git",
    ".github",
    "output",
    "scripts",
    "release",
    "voices",
    ".commandcode",
    ".archify",
    "taste",
    ".ui_backup_*",
    "ui_screenshots_*",
}
# Root-level dev artifacts that must never leak into the portable zip:
# test outputs and the engine download checkpoint list. NOTE: mirrors.txt
# and verified 7zr.exe ship on purpose (portable offline extraction / custom mirrors).
EXCLUDE_FILES = {
    "config.json", "history.json", "*.log", "*.log.*", "*.part", "*.tmp",
    ".venv_install.lock", "crash.log", "engine.log", ".gitignore", ".gitattributes",
    "manbo.list", "曼波_*.mp3", "MamboTTS 界面*.md",
}
ZIP_EXCLUDE_HINTS = (
    "venv",
    "pycache",
    "config.json",
    "history.json",
    "crash.log",
    "engine.log",
    "gpt-sovits/",
    "gpt-sovits._staging/",
    "gpt-sovits._old/",
    "mambotts/tests/",
    "mambotts/scripts/",
    "mambotts/release/",
    ".git/",
    ".github/",
    "screenshots",
    "ui_backup",
    ".commandcode",
    ".archify",
)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd))
    return subprocess.run(cmd, **kw)


def stage(base_out: Path) -> Path:
    stage_dir = base_out / "MamboTTS"
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True)

    # robocopy return codes < 8 are success-ish; handle manually
    cmd = ["robocopy", str(PROJECT), str(stage_dir), "/E", "/XJ", "/NFL", "/NDL", "/NJH", "/NJS", "/NP"]
    for d in EXCLUDE_DIRS:
        cmd += ["/XD", d]
    for f in EXCLUDE_FILES:
        cmd += ["/XF", f]
    rc = subprocess.run(cmd).returncode
    if rc >= 8:
        raise SystemExit(f"robocopy failed rc={rc}")
    print(f"staged -> {stage_dir}")
    return stage_dir


def deref_models(stage_dir: Path) -> None:
    models_dst = stage_dir / "models"
    if models_dst.exists() and not any(p.is_symlink() for p in [models_dst]):
        # robocopy /XJ already skipped it; copy real files now
        models_dst.mkdir(exist_ok=True)
    else:
        shutil.rmtree(models_dst, ignore_errors=True)
        models_dst.mkdir(exist_ok=True)
    for name in MODELS:
        src = PROJECT / "models" / name
        assert src.exists(), f"missing model source: {src}"
        shutil.copy2(src, models_dst / name)
        staged = (models_dst / name).stat().st_size
        source = src.stat().st_size
        assert staged == source, f"{name}: staged {staged} != source {source} bytes"
        floor = MIN_MODEL_BYTES[name]
        assert staged >= floor, f"{name}: {staged} bytes < floor {floor} (stub/pointer?)"
        assert not (models_dst / name).is_symlink(), f"{name} still a link!"
    print("models dereferenced + byte-verified (staged == source, floors OK)")


def verify_no_links(stage_dir: Path) -> None:
    import hashlib

    for root, dirs, files in os.walk(stage_dir):
        for entry in dirs + files:
            p = Path(root) / entry
            if p.is_symlink():
                raise SystemExit(f"SYMLINK/JUNCTION LEAKED INTO STAGE: {p}")
    if str(PROJECT) not in sys.path:
        sys.path.insert(0, str(PROJECT))
    from mambotts.infra.installer import SEVEN_Z_SHA256

    tool = stage_dir / "7zr.exe"
    assert tool.is_file(), f"bundled 7zr.exe missing in staging: {tool}"
    actual = hashlib.sha256(tool.read_bytes()).hexdigest().lower()
    assert actual == SEVEN_Z_SHA256, f"7zr.exe SHA-256 mismatch: {actual} != {SEVEN_Z_SHA256}"
    app_icon = stage_dir / "mambotts" / "ui" / "assets" / "app_icon.png"
    assert app_icon.is_file(), f"app icon missing in staging: {app_icon}"
    print("no junctions in staging + bundled 7zr.exe SHA-256 + app_icon OK")


def test_gate(stage_dir: Path) -> None:
    py = stage_dir / "runtime" / "python.exe"
    assert py.exists(), "bundled runtime missing from staging"
    gate_env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    rc = subprocess.run(
        [str(py), "-B", "-c", "import PySide6.QtWidgets, PySide6.QtSvg, PySide6.QtMultimedia, requests"],
        cwd=str(stage_dir),
        env=gate_env,
    )
    assert rc.returncode == 0, "bundled runtime import probe failed"
    # Gate: run the PROJECT test suite (tests/ stays out of the zip) with
    # the STAGED runtime python -- proving the shipped tree itself works.
    rc = subprocess.run(
        [str(py), "-B", "-m", "unittest", "discover", "-s", str(PROJECT / "tests")],
        cwd=str(PROJECT),
        env=gate_env,
        capture_output=True,
        text=True,
    )
    tail = (rc.stdout + rc.stderr).strip().splitlines()[-2:]
    print("gate:", *tail, sep="\n  ")
    assert rc.returncode == 0 and "OK" in " ".join(tail), "portable test gate failed"


def compress(stage_dir: Path, out: Path) -> Path:
    zip_path = out / "MamboTTS-v2.0.0-portable.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, dirs, files in os.walk(stage_dir):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                fp = Path(root) / f
                if fp.suffix.lower() in {".pyc", ".pyo"}:
                    continue
                zf.write(fp, fp.relative_to(stage_dir.parent))
    # assert zip hygiene
    with zipfile.ZipFile(zip_path) as check_zf:
        bad = [n for n in check_zf.namelist()
               if any(h in n.lower() for h in ZIP_EXCLUDE_HINTS)]
    assert not bad, f"zip hygiene violated: {bad[:5]}"
    print(f"zip -> {zip_path} ({zip_path.stat().st_size/1024/1024:.0f} MB), hygiene OK")
    return zip_path


def trim_runtime(stage_dir: Path) -> None:
    """Strip unused Qt modules from the staged runtime (scripts/trim_runtime.py)."""
    script = PROJECT / "scripts" / "trim_runtime.py"
    rc = subprocess.run([sys.executable, str(script), str(stage_dir)]).returncode
    assert rc == 0, "trim_runtime failed"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(PROJECT / "release"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(exist_ok=True)

    print("=== 1/6 staging ===")
    stage_dir = stage(out)
    print("=== 2/6 models deref ===")
    deref_models(stage_dir)
    print("=== 3/6 junction check ===")
    verify_no_links(stage_dir)
    print("=== 4/6 trim bundled runtime ===")
    trim_runtime(stage_dir)
    print("=== 5/6 portable test gate ===")
    test_gate(stage_dir)
    print("=== 6/6 compress ===")
    zip_path = compress(stage_dir, out)
    shutil.rmtree(stage_dir, ignore_errors=True)
    print(f"\nDONE: {zip_path}")
    print("Pre-ship smoke (clean VM): unzip -> double-click MamboTTS.bat -> engine install -> synth.")


if __name__ == "__main__":
    main()
