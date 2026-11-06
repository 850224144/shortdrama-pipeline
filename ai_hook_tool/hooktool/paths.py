# -*- coding: utf-8 -*-
"""路径与二进制解析。"""
import os

MODULE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(MODULE_ROOT, "config")
WORKDIR = os.path.join(MODULE_ROOT, "workdir")

# 工作区槽位目录（对齐定稿 §6）
DIR_FACES = os.path.join(WORKDIR, "02_素材库", "faces")
DIR_SCENES = os.path.join(WORKDIR, "02_素材库", "scenes")
DIR_HOOK_ANIMAL = os.path.join(WORKDIR, "02_素材库", "hooks_animal")
DIR_TEMPLATES = os.path.join(WORKDIR, "02_素材库", "templates")
DIR_SCRIPT = os.path.join(WORKDIR, "03_脚本分镜")
DIR_AI_PRE = os.path.join(WORKDIR, "04_ai_generated", "前核15s")
DIR_AI_HOOK = os.path.join(WORKDIR, "04_ai_generated", "后钩子15s")
DIR_SEGMENTS = os.path.join(WORKDIR, "04_ai_generated", "segments")
DIR_OUTPUT = os.path.join(WORKDIR, "05_成片")
DIR_GATE = os.path.join(WORKDIR, "06_合规校验")

WORKDIR_DIRS = [
    DIR_FACES, DIR_SCENES, DIR_HOOK_ANIMAL, DIR_TEMPLATES,
    DIR_SCRIPT, DIR_AI_PRE, DIR_AI_HOOK, DIR_SEGMENTS, DIR_OUTPUT, DIR_GATE,
]


def ensure_workdir():
    """创建工作区槽位目录（幂等）。"""
    for d in WORKDIR_DIRS:
        os.makedirs(d, exist_ok=True)


def config_path(name: str) -> str:
    return os.path.join(CONFIG_DIR, name)
