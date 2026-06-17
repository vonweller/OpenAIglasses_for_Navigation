# -*- coding: utf-8 -*-
"""Shared filesystem locations for the packaged backend."""

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
APP_DIR = PROJECT_ROOT
MODEL_DIR = PROJECT_ROOT / "model"
VOICE_DIR = PROJECT_ROOT / "voice"
MUSIC_DIR = PROJECT_ROOT / "music"
RECORDINGS_DIR = PROJECT_ROOT / "recordings"
STATIC_DIR = PROJECT_ROOT / "static"
TEMPLATES_DIR = PROJECT_ROOT / "templates"
RUNTIME_CONFIG_PATH = PROJECT_ROOT / "runtime_config.json"
