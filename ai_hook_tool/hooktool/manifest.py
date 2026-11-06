# -*- coding: utf-8 -*-
"""分镜/产物 manifest 读写（两段式，后期 API 化可平移）。"""
import json
import os

from . import paths

MANIFEST_PATH = os.path.join(paths.DIR_SCRIPT, "manifest.json")

SEG_PRE = "前核15s"
SEG_HOOK = "后钩子15s"


def empty_manifest(drama_name: str, genre: str, episode_label: str, source_episode: str) -> dict:
    return {
        "drama_name": drama_name,
        "genre": genre,
        "episode_label": episode_label,
        "source_episode": source_episode,
        "refs": {},
        "segments": [
            {"segment": SEG_PRE, "shots": []},
            {"segment": SEG_HOOK, "shots": []},
        ],
        "clips": [],
        "status": "draft",
        "warns": [],
    }


def load(path: str = MANIFEST_PATH) -> dict | None:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save(manifest: dict, path: str = MANIFEST_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def segment(manifest: dict, name: str) -> dict | None:
    for seg in manifest["segments"]:
        if seg["segment"] == name:
            return seg
    return None
