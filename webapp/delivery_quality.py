# -*- coding: utf-8 -*-
"""媒体质量门禁（R6）：渲染产物交付前的结构化校验。

在成片进入交付/上传前检查：文件存在、非 0 字节、含视频流、含音轨、时长在
预期区间。任一不满足即拒收（阻止残缺文件进入交付）。

纯函数 + 显式依赖注入（ffprobe 路径），便于单元测试与复用。
"""

import json
import os
import subprocess


def probe_media(file_path, ffprobe_path):
    """用 ffprobe 读取媒体流信息，返回 dict 或 None（不可解析/失败）。"""
    if not file_path or not os.path.isfile(file_path):
        return None
    if not ffprobe_path or not os.path.isfile(ffprobe_path):
        return None
    cmd = [ffprobe_path, '-v', 'error', '-print_format', 'json',
           '-show_streams', '-show_format', file_path]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads((proc.stdout or b'').decode('utf-8', 'replace'))
    except (ValueError, TypeError):
        return None


def validate_media(file_path, ffprobe_path=None, min_duration=None,
                   max_duration=None):
    """校验单个媒体文件。返回 {ok, reason, size, duration, has_video, has_audio}。

    - 无音轨/无视频流/0 字节/时长异常（0、低于下限、超过上限）→ ok=False。
    - ffprobe 缺失或解析失败 → ok=False（无法证明文件完好，保守拒收）。
    """
    base = {
        'ok': True, 'reason': '', 'size': 0, 'duration': None,
        'has_video': False, 'has_audio': False,
    }
    if not file_path or not os.path.isfile(file_path):
        base.update(ok=False, reason='文件不存在')
        return base

    size = os.path.getsize(file_path)
    base['size'] = size
    if size == 0:
        base.update(ok=False, reason='0 字节')
        return base

    info = probe_media(file_path, ffprobe_path)
    if info is None:
        base.update(ok=False, reason='无法解析媒体流（ffprobe 失败/缺失）')
        return base

    streams = info.get('streams') or []
    base['has_video'] = any(s.get('codec_type') == 'video' for s in streams)
    base['has_audio'] = any(s.get('codec_type') == 'audio' for s in streams)

    try:
        duration = float((info.get('format') or {}).get('duration') or 0)
    except (TypeError, ValueError):
        duration = 0.0
    base['duration'] = duration

    if not base['has_video']:
        base.update(ok=False, reason='无视频流')
        return base
    if not base['has_audio']:
        base.update(ok=False, reason='无音轨')
        return base
    if duration <= 0:
        base.update(ok=False, reason='时长为 0')
        return base
    if min_duration is not None and duration < min_duration:
        base.update(ok=False, reason=f'时长过短（{duration:.1f}s < {min_duration}s）')
        return base
    if max_duration is not None and duration > max_duration:
        base.update(ok=False, reason=f'时长过长（{duration:.1f}s > {max_duration}s）')
        return base
    return base
