# -*- coding: utf-8 -*-
"""首集转写：优先复用已有转写文本；无则可选跑 faster-whisper。

不转全集（定稿 D15）。whisper 不可用/失败时返回 PENDING 原因，不抛断流程。

缓存隔离（2026-11-17 修）：
此前所有剧共用 ``workdir/03_脚本分镜/transcript_E01.txt``，换一部剧且素材目录
没有自带转写时会直接命中上一部剧的缓存，把别剧对白当本剧剧情事实（素材串戏，
且下游钩子/文案全跟着错）。现在缓存按「剧名 + 集号 + 源文件指纹」命名，并在
首行写归属头（drama / episode / source / fp）；读取时逐项校验归属，不匹配一律
不采用——宁可重转一次，也不复用别剧对白。旧的无归属 ``transcript_E01.txt``
不再读取（无法证明它属于哪部剧），保留在目录里不删。
"""
import hashlib
import os
import re

from . import paths
from .normalize import normalize_name, parse_episode_number

WHISPER_MODEL_PATH = os.environ.get(
    "WHISPER_MODEL_PATH",
    r"D:\models\models\keepitsimple--faster-whisper-large-v3\snapshots\master",
)

# 缓存首行的归属头（存在即证明该缓存可归属到某部剧的某一集某个源文件）
CACHE_HEADER_PREFIX = "# ai_hook-transcript"
_FP_CHUNK = 1024 * 1024  # 指纹只取首尾各 1MB，避免大文件全量哈希
_SLUG_MAX = 40


def _source_fingerprint(path: str) -> str:
    """源文件指纹：大小 + 首尾各 1MB 的 SHA1 前 12 位。

    不用 mtime——同一部剧被复制/移动到别的目录后 mtime 会变，指纹应跟随内容
    保持稳定（否则每次搬目录都要重转一遍，成本高）；但换版本（重下/重切、
    文件大小或头尾字节变化）会得到不同指纹，缓存自动失效。
    读取失败返回空串（调用方据此判 PENDING）。
    """
    try:
        size = os.path.getsize(path)
        h = hashlib.sha1(str(size).encode("utf-8"))
        with open(path, "rb") as f:
            h.update(f.read(_FP_CHUNK))
            if size > _FP_CHUNK:
                f.seek(max(_FP_CHUNK, size - _FP_CHUNK))
                h.update(f.read(_FP_CHUNK))
        return h.hexdigest()[:12]
    except OSError:
        return ""


def _drama_slug(drama_name: str) -> str:
    """剧名 → 文件名安全片段（只留中文/字母/数字，超长截断）。"""
    slug = normalize_name(drama_name) or "unknown"
    return slug[:_SLUG_MAX]


def cache_path_for(drama_name: str, ep_num: int, episode_path: str,
                   fingerprint: str | None = None) -> str:
    """按「剧名 + 集号 + 源指纹」构造转写缓存路径（含剧名即天然按剧隔离）。"""
    fp = fingerprint if fingerprint is not None else _source_fingerprint(episode_path)
    ep = int(ep_num) if ep_num is not None else 1
    name = f"transcript_{_drama_slug(drama_name)}_E{ep:02d}_{fp or 'nofp'}.txt"
    return os.path.join(paths.DIR_SCRIPT, name)


def _header_line(drama_name: str, ep_num: int, episode_path: str, fp: str) -> str:
    episode = f"E{int(ep_num):02d}" if ep_num is not None else "E??"
    return (f"{CACHE_HEADER_PREFIX} drama={drama_name} episode={episode} "
            f"source={os.path.abspath(episode_path)} fp={fp}")


def _parse_header(line: str) -> dict:
    """解析归属头 → {'drama','episode','source','fp'}；非本格式返回 {}。"""
    if not line.startswith(CACHE_HEADER_PREFIX):
        return {}
    fields = {}
    for part in line[len(CACHE_HEADER_PREFIX):].split():
        if "=" in part:
            k, v = part.split("=", 1)
            fields[k] = v
    return fields


def _read_cached_lines(cache_path: str, drama_name: str, ep_num: int,
                       fp: str) -> list[str] | None:
    """读缓存并校验归属；归属不符/无归属头/读失败 → None（不得采用）。

    逐项校验：剧名（归一化后相等）、集号、源指纹。任一不符即视为「别剧/别版本
    的转写」，返回 None 让调用方重新转写。
    """
    if not cache_path or not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            lines = [ln.rstrip("\n") for ln in f]
    except OSError:
        return None
    if not lines:
        return None
    header = _parse_header(lines[0])
    if not header:
        return None  # 旧的无归属缓存：无法证明属于本剧，不采用
    if normalize_name(header.get("drama", "")) != normalize_name(drama_name):
        return None
    want_ep = f"E{int(ep_num):02d}" if ep_num is not None else None
    if want_ep and header.get("episode") != want_ep:
        return None
    if header.get("fp") != fp:
        return None
    return [ln for ln in lines[1:] if ln.strip()]


def read_existing(transcript_path: str | None) -> list[str]:
    """读取素材目录自带的转写/字幕文件（由 material 扫描并按首集绑定）。"""
    if not transcript_path or not os.path.exists(transcript_path):
        return []
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            return [ln.rstrip("\n") for ln in f if ln.strip()]
    except OSError:
        return []


def transcribe_first_episode(episode_path: str, drama_name: str | None = None,
                             ep_num: int | None = None) -> tuple[list[str], str | None]:
    """转写首集。返回 (lines, pending_reason)；模型不可用/失败时 lines=[] 并给原因。

    缓存按剧名 + 集号 + 源指纹隔离（见模块 docstring），命中条件三项全等。
    drama_name 缺省时取首集所在目录名（素材目录约定为剧名）；ep_num 缺省时从
    文件名解析，解析不到按 1 处理（指纹仍能防止跨文件串用）。
    """
    if not episode_path or not os.path.exists(episode_path):
        return [], f"首集文件不存在：{episode_path}"
    drama = drama_name or os.path.basename(
        os.path.dirname(os.path.abspath(episode_path)))
    if ep_num is None:
        ep_num = parse_episode_number(os.path.basename(episode_path)) or 1
    fp = _source_fingerprint(episode_path)
    if not fp:
        return [], f"无法读取首集文件（指纹计算失败）：{episode_path}"

    cache = cache_path_for(drama, ep_num, episode_path, fingerprint=fp)
    cached = _read_cached_lines(cache, drama, ep_num, fp)
    if cached is not None:
        return cached, None

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
            f.write(_header_line(drama, ep_num, episode_path, fp) + "\n")
            f.write("\n".join(lines))
        return lines, None
    except Exception as exc:
        return [], f"首集转写失败：{exc}"


def load_or_transcribe(material: dict) -> tuple[list[str], str | None]:
    """已有转写文本直接复用；否则尝试首集 whisper（缓存按剧/集/指纹隔离）。"""
    lines = read_existing(material.get("transcript"))
    if lines:
        return lines, None
    first = material.get("first_episode")
    if not first:
        return [], "无首集 mp4，无法转写"
    if isinstance(first, (tuple, list)) and len(first) >= 2:
        ep_num, path = first[0], first[1]
    else:  # 兼容旧调用（直接给路径）
        ep_num, path = None, first
    return transcribe_first_episode(
        str(path), drama_name=material.get("drama_name"), ep_num=ep_num)
