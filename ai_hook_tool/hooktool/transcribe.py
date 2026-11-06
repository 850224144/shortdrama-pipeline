# -*- coding: utf-8 -*-
"""首集转写：优先复用已有转写文本；无则可选跑 faster-whisper。

不转全集（定稿 D15）。whisper 不可用/失败时返回 PENDING 原因，不抛断流程。
"""
import os

from . import config as cfg, paths

WHISPER_MODEL_PATH = os.environ.get(
    "WHISPER_MODEL_PATH",
    r"D:\models\models\keepitsimple--faster-whisper-large-v3\snapshots\master",
)


def _transcript_cache_path() -> str:
    return os.path.join(paths.DIR_SCRIPT, "transcript_E01.txt")


def read_existing(transcript_path: str | None) -> list[str]:
    if not transcript_path or not os.path.exists(transcript_path):
        return []
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            return [ln.rstrip("\n") for ln in f if ln.strip()]
    except OSError:
        return []


def transcribe_first_episode(episode_path: str) -> tuple[list[str], str | None]:
    """转写首集。返回 (lines, pending_reason)；模型不可用/失败时 lines=[] 并给原因。"""
    cache = _transcript_cache_path()
    if os.path.exists(cache):
        with open(cache, "r", encoding="utf-8") as f:
            return [ln.rstrip("\n") for ln in f if ln.strip()], None

    try:
        from faster_whisper import WhisperModel  # type: ignore
    except Exception as exc:  # 模型/依赖缺失 → PENDING
        return [], f"faster-whisper 不可用：{exc}"

    if not os.path.exists(WHISPER_MODEL_PATH):
        return [], f"whisper 模型路径不存在：{WHISPER_MODEL_PATH}"

    try:
        segments = []
        last_exc = None
        model = None
        for device, compute in (("cuda", "int8"), ("cpu", "int8")):
            try:
                model = WhisperModel(WHISPER_MODEL_PATH, device=device, compute_type=compute)
                segs, _info = model.transcribe(episode_path, language="zh", beam_size=5, vad_filter=True)
                segments = list(segs)
                break
            except Exception as exc:  # cuda 库缺失/推理失败 → 回退 CPU
                last_exc = exc
                model = None
        if model is None:
            raise last_exc  # type: ignore[misc]
        lines = [f"[{seg.start:.2f}-{seg.end:.2f}] {seg.text.strip()}" for seg in segments]
        os.makedirs(paths.DIR_SCRIPT, exist_ok=True)
        with open(cache, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return lines, None
    except Exception as exc:
        return [], f"首集转写失败：{exc}"


def load_or_transcribe(material: dict) -> tuple[list[str], str | None]:
    """已有转写文本直接复用；否则尝试首集 whisper。"""
    lines = read_existing(material.get("transcript"))
    if lines:
        return lines, None
    first = material.get("first_episode")
    if not first:
        return [], "无首集 mp4，无法转写"
    return transcribe_first_episode(first[1])
