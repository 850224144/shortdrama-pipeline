"""流程配置读取（config/hook_flow.json 外置，不在代码里写死阈值）。"""
from __future__ import annotations

import copy
import json

from .. import paths

_CACHE: dict = {}


def config() -> dict:
    if "value" not in _CACHE:
        with open(paths.config_path("hook_flow.json"), "r", encoding="utf-8") as handle:
            _CACHE["value"] = json.load(handle)
    return copy.deepcopy(_CACHE["value"])


def policy(overrides: dict | None = None) -> dict:
    merged = config()["policy"]
    if overrides:
        merged.update(copy.deepcopy(overrides))
    return merged


def mechanisms() -> list[dict]:
    return config()["mechanisms"]
