# -*- coding: utf-8 -*-
"""配置加载：阈值、词表、路径全部外置到 config/，不写死代码。"""
import json
import os

from . import paths

_CACHE = {}


def _load(name: str) -> dict:
    if name not in _CACHE:
        with open(paths.config_path(name), "r", encoding="utf-8") as f:
            _CACHE[name] = json.load(f)
    return _CACHE[name]


def settings() -> dict:
    return _load("settings.json")


def compliance() -> dict:
    return _load("compliance.json")


def genres() -> dict:
    return _load("genres.json")


def keywords() -> dict:
    return _load("keywords.json")


def get_setting(key: str, default=None):
    return settings().get(key, default)


def ffmpeg_bin() -> str:
    return settings().get("ffmpeg")


def ffprobe_bin() -> str:
    return settings().get("ffprobe")


def bin_exists(path: str) -> bool:
    return bool(path) and os.path.exists(path)
