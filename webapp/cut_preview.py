# -*- coding: utf-8 -*-
"""只读切片方案预览。

从 edl_config_v*.json 汇总版本、片段与产物状态；若源素材已有
highlight_scores.json，则按时间窗口重叠补充候选理由。该模块不写文件，
不触发渲染或上传。
"""

import json
import os
import re


_CONFIG_RE = re.compile(r'^edl_config_v(\d+)\.json$')


def _validate_drama_name(drama_name):
    name = str(drama_name or '').strip()
    if (not name or name in ('.', '..') or len(name) > 200
            or os.path.basename(name) != name
            or any(ch in name for ch in ('/', '\\', ':', '*', '?', '"', '<', '>', '|'))):
        raise ValueError('剧名非法')
    return name


def _find_output_dir(output_root, drama_name):
    """在 output/<账号>/<剧名> 下查找唯一目录。"""
    root = os.path.realpath(output_root)
    matches = []
    if not os.path.isdir(root):
        return None
    for account in os.listdir(root):
        account_dir = os.path.join(root, account)
        if not os.path.isdir(account_dir):
            continue
        candidate = os.path.join(account_dir, drama_name)
        if os.path.isdir(candidate):
            matches.append((account, os.path.realpath(candidate)))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0])
    return matches[0]


def _find_transcribe_dir(source_root, drama_name):
    """兼容下载目录多套一层同名文件夹的素材结构。"""
    if not source_root:
        return None
    base = os.path.join(source_root, drama_name)
    if not os.path.isdir(base):
        return None
    for root, dirs, _files in os.walk(base):
        if 'transcribe' in dirs:
            return os.path.join(root, 'transcribe')
    return None


def _load_highlight_candidates(source_root, drama_name):
    transcribe_dir = _find_transcribe_dir(source_root, drama_name)
    if not transcribe_dir:
        return {}
    cache_path = os.path.join(transcribe_dir, 'highlight_scores.json')
    try:
        with open(cache_path, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, item in raw.items():
        try:
            episode = int(key)
        except (TypeError, ValueError):
            continue
        if not isinstance(item, dict):
            continue
        cands = item.get('cands')
        if not isinstance(cands, list):
            cands = [item] if 'score' in item else []
        out[episode] = [c for c in cands if isinstance(c, dict)]
    return out


def _match_candidate(segment, candidates):
    """返回与该片段重叠最多的候选；无重叠返回 None。"""
    try:
        seg_start = float(segment.get('start', 0))
        seg_end = seg_start + float(segment.get('duration', 0))
    except (TypeError, ValueError):
        return None
    best = None
    best_overlap = 0.0
    for cand in candidates:
        try:
            cand_start = float(cand.get('start', 0))
            if 'end' in cand:
                cand_end = float(cand['end'])
            else:
                cand_end = cand_start + float(cand.get('dur', 0))
        except (TypeError, ValueError):
            continue
        overlap = max(0.0, min(seg_end, cand_end) - max(seg_start, cand_start))
        if overlap > best_overlap:
            best = cand
            best_overlap = overlap
    return best if best_overlap > 0 else None


def _expected_video_path(out_dir, version, hook):
    suffix = f'_{version}_{hook}_去重版.mp4'
    try:
        matches = sorted(
            name for name in os.listdir(out_dir)
            if name.endswith(suffix) and os.path.isfile(os.path.join(out_dir, name))
        )
    except OSError:
        return None
    return os.path.join(out_dir, matches[0]) if matches else None


def _safe_existing_file(path, root):
    """只接受 output/<账号>/<剧名> 目录内的真实文件。"""
    if not path:
        return None
    try:
        root_path = os.path.realpath(root)
        file_path = os.path.realpath(path)
        if os.path.commonpath([root_path, file_path]) != root_path:
            return None
    except (OSError, ValueError):
        return None
    return file_path if os.path.isfile(file_path) else None


def build_cut_preview(output_root, drama_name, source_root=None):
    """构建只读方案预览。目录不存在时返回 None。"""
    name = _validate_drama_name(drama_name)
    found = _find_output_dir(output_root, name)
    if found is None:
        return None
    account, out_dir = found
    highlights = _load_highlight_candidates(source_root, name)
    configurations = []
    for filename in os.listdir(out_dir):
        match = _CONFIG_RE.match(filename)
        if match:
            configurations.append((int(match.group(1)), filename))
    configurations.sort()

    versions = []
    for version, filename in configurations:
        config_path = os.path.join(out_dir, filename)
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
        except (OSError, ValueError, TypeError):
            continue
        videos = config.get('videos') or []
        video_config = videos[0] if videos and isinstance(videos[0], dict) else {}
        hook = str(video_config.get('hook_keyword') or '')
        segments = []
        planned_duration = 0.0
        for index, segment in enumerate(video_config.get('segments') or [], 1):
            if not isinstance(segment, dict):
                continue
            try:
                episode = int(segment.get('episode'))
                start = float(segment.get('start', 0))
                duration = float(segment.get('duration', 0))
            except (TypeError, ValueError):
                continue
            match = _match_candidate(segment, highlights.get(episode, []))
            planned_duration += max(0.0, duration)
            segments.append({
                'index': index,
                'episode': episode,
                'start': round(start, 2),
                'end': round(start + max(0.0, duration), 2),
                'duration': round(max(0.0, duration), 2),
                'hook_type': str((match or {}).get('hook_type') or ''),
                'reason': str((match or {}).get('reason') or ''),
                'score': (match or {}).get('score'),
                'candidate_source': str((match or {}).get('src') or ''),
            })

        artifacts = config.get('artifacts') or {}
        video_path = _safe_existing_file(artifacts.get('video_path'), out_dir)
        if not video_path:
            video_path = _expected_video_path(out_dir, version, hook)
        cover_path = _safe_existing_file(artifacts.get('cover_path'), out_dir)
        versions.append({
            'version': version,
            'hook': hook,
            'title': str(video_config.get('title') or ''),
            'description': str(video_config.get('description') or ''),
            'hashtags': video_config.get('hashtags') or [],
            'planned_duration': round(planned_duration, 1),
            'segment_count': len(segments),
            'segments': segments,
            'status': 'rendered' if video_path else 'planned',
            'video_exists': bool(video_path),
            'video_name': os.path.basename(video_path) if video_path else '',
            'cover_exists': bool(cover_path),
            'config_file': filename,
        })

    return {
        'drama_name': name,
        'account': account,
        'version_count': len(versions),
        'rendered_count': sum(1 for v in versions if v['video_exists']),
        'planned_count': sum(1 for v in versions if not v['video_exists']),
        'versions': versions,
    }
