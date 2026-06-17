#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Prepare model assets for first-time setup.

This script keeps the on-disk layout explicit:
- model/yolo-seg.pt
- model/yoloe-11l-seg.pt
- model/shoppingbest5.pt
- model/trafficlight.pt
- model/hand_landmarker.task
- ./mobileclip_blt.ts

It first tries to copy files from a ModelScope snapshot of the bundled model repo.
If a file is still missing, it falls back to a direct download for mobileclip_blt.ts.
"""

from __future__ import annotations

import os
import shutil
import sys
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
MODEL_DIR = APP_DIR / "model"
MODEL_REPO = os.getenv("AIGLASS_MODEL_REPO", "archifancy/AIGlasses_for_navigation")
SNAPSHOT_ROOT = os.getenv("AIGLASS_MODEL_SNAPSHOT", "").strip()
MODEL_FILES = [
    "yolo-seg.pt",
    "yoloe-11l-seg.pt",
    "shoppingbest5.pt",
    "trafficlight.pt",
    "hand_landmarker.task",
]
MOBILECLIP_NAME = "mobileclip_blt.ts"
MOBILECLIP_URL = "https://github.com/ultralytics/assets/releases/download/v8.4.0/mobileclip_blt.ts"
MOBILECLIP_MIN_BYTES = 500 * 1024 * 1024


def log(msg: str) -> None:
    print(msg, flush=True)


def ensure_dirs() -> None:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)


def load_modelscope_snapshot() -> Path | None:
    if SNAPSHOT_ROOT:
        root = Path(SNAPSHOT_ROOT)
        if root.exists():
            return root
    try:
        from modelscope.hub.snapshot_download import snapshot_download
    except Exception as exc:
        log(f"[models] ModelScope 未安装或不可用: {exc}")
        return None

    try:
        log(f"[models] 正在从 ModelScope 拉取仓库: {MODEL_REPO}")
        snapshot_dir = snapshot_download(MODEL_REPO)
        root = Path(snapshot_dir)
        log(f"[models] ModelScope 快照目录: {root}")
        return root
    except Exception as exc:
        log(f"[models] ModelScope 下载失败: {exc}")
        return None


def copy_from_snapshot(snapshot_root: Path, filename: str, dest: Path) -> bool:
    matches = []
    try:
        matches = [p for p in snapshot_root.rglob(filename) if p.is_file()]
    except Exception:
        matches = []
    if not matches:
        return False

    src = max(matches, key=lambda p: p.stat().st_size)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size == src.stat().st_size:
        log(f"[models] 已存在 {dest.relative_to(APP_DIR)}")
        return True
    shutil.copy2(src, dest)
    log(f"[models] 已复制 {src.name} -> {dest.relative_to(APP_DIR)}")
    return True


def download_file(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"[models] 下载 {dest.name} ...")
    with urllib.request.urlopen(url) as response, open(dest, "wb") as f:
        shutil.copyfileobj(response, f, length=1024 * 1024)


def prepare_mobileclip(dest: Path) -> bool:
    if dest.exists() and dest.stat().st_size >= MOBILECLIP_MIN_BYTES:
        log(f"[models] 已存在 {dest.name} ({dest.stat().st_size // (1024 * 1024)} MB)")
        return True

    try:
        download_file(MOBILECLIP_URL, dest)
        size_mb = dest.stat().st_size / (1024 * 1024)
        log(f"[models] 已下载 {dest.name} ({size_mb:.1f} MB)")
        return True
    except Exception as exc:
        log(f"[models] 下载 {dest.name} 失败: {exc}")
        return False


def main() -> int:
    ensure_dirs()
    snapshot_root = load_modelscope_snapshot()

    missing = []
    for filename in MODEL_FILES:
        dest = MODEL_DIR / filename
        if dest.exists() and dest.stat().st_size > 0:
            log(f"[models] 已存在 model/{filename}")
            continue
        if snapshot_root and copy_from_snapshot(snapshot_root, filename, dest):
            continue
        missing.append(dest)

    mobileclip_ok = False
    mobileclip_dest = APP_DIR / MOBILECLIP_NAME
    if snapshot_root and copy_from_snapshot(snapshot_root, MOBILECLIP_NAME, mobileclip_dest):
        mobileclip_ok = True
    else:
        mobileclip_ok = prepare_mobileclip(mobileclip_dest)

    log("")
    log("[models] 结果:")
    for filename in MODEL_FILES:
        dest = MODEL_DIR / filename
        log(f"  - {dest.relative_to(APP_DIR)}: {'OK' if dest.exists() and dest.stat().st_size > 0 else 'MISSING'}")
    log(f"  - {MOBILECLIP_NAME}: {'OK' if mobileclip_ok else 'MISSING'}")

    if missing or not mobileclip_ok:
        log("")
        log("[models] 仍有缺失文件时，可手动放到以下位置:")
        for dest in missing:
            log(f"  - {dest.relative_to(APP_DIR)}")
        log(f"  - {MOBILECLIP_NAME}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
