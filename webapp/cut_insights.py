# -*- coding: utf-8 -*-
"""切片版本差异与内容风险审计（只读）。

在现有 EDL 预览基础上补充：
- 版本间的精确片段复用、文本重合、视觉指纹重合；
- 素材来源占比与本机历史上传冲突；
- 可解释的风险等级和发布建议。

该模块不调用 LLM、不启动渲染，也不修改 EDL 或上传队列。平台侧已发表
视频库无法从本地直接访问，因此这里明确输出 ``platform_comparison.available
= False``，不会把“本地未命中”误报成“平台原创通过”。
"""

import json
import os
import re
import sqlite3

from cut_preview import build_cut_preview, _find_output_dir


_CONFIG_RE = re.compile(r'^edl_config_v(\d+)\.json$')


def _normal_text(text):
    return re.sub(r'[^\u4e00-\u9fffa-zA-Z0-9]', '', str(text or ''))


def _ngrams(text, n=3):
    text = _normal_text(text)
    if not text:
        return set()
    if len(text) < n:
        return {text}
    return {text[i:i + n] for i in range(len(text) - n + 1)}


def _set_similarity(a, b):
    """文本包含度：短集合有多少 n-gram 出现在长集合中。"""
    if not a or not b:
        return 0.0
    return round(len(a & b) / min(len(a), len(b)), 4)


def _find_transcribe_dir(src_dir):
    if not src_dir or not os.path.isdir(src_dir):
        return None
    direct = os.path.join(src_dir, 'transcribe')
    if os.path.isdir(direct):
        return direct
    for root, dirs, _files in os.walk(src_dir):
        if 'transcribe' in dirs:
            return os.path.join(root, 'transcribe')
    return None


def _load_transcripts(src_dir):
    """返回 {集号: [ASR 片段]}；缺失或损坏时返回空映射。"""
    transcribe = _find_transcribe_dir(src_dir)
    if not transcribe:
        return {}
    out = {}
    for name in os.listdir(transcribe):
        if not (name.startswith('EP') and name.endswith('.json')):
            continue
        stem = name[2:-5]
        match = re.search(r'第(\d+)集', stem) or re.search(r'(\d+)(?!.*\d)', stem)
        if not match:
            continue
        try:
            with open(os.path.join(transcribe, name), 'r', encoding='utf-8') as f:
                rows = json.load(f)
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(rows, list):
            out[int(match.group(1))] = [r for r in rows if isinstance(r, dict)]
    return out


def _segment_text(segment, transcripts):
    rows = transcripts.get(int(segment.get('episode', 0)), [])
    start = float(segment.get('start', 0) or 0)
    end = start + max(0.0, float(segment.get('duration', 0) or 0))
    texts = []
    for row in rows:
        try:
            row_start = float(row.get('start', 0) or 0)
            row_end = float(row.get('end', row_start) or row_start)
        except (TypeError, ValueError):
            continue
        if row_end > start and row_start < end and row.get('text'):
            texts.append(str(row['text']).strip())
    text = ''.join(texts)
    return text[:180], _ngrams(text)


def _load_edl_sources(out_dir):
    configs = {}
    for name in os.listdir(out_dir):
        match = _CONFIG_RE.match(name)
        if not match:
            continue
        try:
            with open(os.path.join(out_dir, name), 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError, TypeError):
            continue
        configs[int(match.group(1))] = {
            'source_dir': data.get('source_dir') or '',
            'source_pattern': data.get('source_pattern') or '',
            'artifacts': data.get('artifacts') or {},
        }
    return configs


def _path_key(path):
    if not path:
        return ''
    try:
        return os.path.normcase(os.path.realpath(path))
    except OSError:
        return os.path.normcase(os.path.abspath(path))


def _load_visual_fingerprints(db_path):
    """从 upload.db 读取已有成片指纹，返回 {路径键: 序列}。"""
    if not db_path or not os.path.isfile(db_path):
        return {}
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                'SELECT video_path, hash_seq FROM content_fingerprints').fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    out = {}
    for row in rows:
        blob = row['hash_seq'] or b''
        if len(blob) % 8:
            continue
        seq = [int.from_bytes(blob[i:i + 8], 'big')
               for i in range(0, len(blob), 8)]
        if seq:
            out[_path_key(row['video_path'])] = seq
    return out


def _load_upload_records(db_path, drama_name):
    if not db_path or not os.path.isfile(db_path):
        return []
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                '''SELECT id, drama_name, version, account, status, title,
                          publish_time, video_path
                   FROM upload_records WHERE drama_name=?
                     AND status IN ('pending','uploading','completed')
                   ORDER BY version, id''', (drama_name,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def _visual_pair(a, b, fingerprints):
    sa = fingerprints.get(_path_key(a))
    sb = fingerprints.get(_path_key(b))
    if not sa or not sb:
        return None
    try:
        from content_fingerprint import similarity
        return round(float(similarity(sa, sb)), 4)
    except Exception:
        return None


def _risk_for_version(version, visual_max, dialogue_max,
                      exact_reuse, upload_records):
    reasons = []
    source_ratio = 1.0 if version.get('segment_count') else 0.0
    if source_ratio >= 0.9:
        reasons.append(
            '画面与原声均直接来自同一部源剧，未检测到新旁白、新音轨或授权补充素材')
    if exact_reuse:
        reasons.append(f'与其它版本精确复用 {exact_reuse} 个素材片段')
    if visual_max is not None and visual_max >= 0.75:
        reasons.append(f'与本剧其它版本视觉相似度最高 {visual_max:.2%}')
    elif visual_max is not None and visual_max >= 0.5:
        reasons.append(f'与本剧其它版本存在疑似视觉重合 {visual_max:.2%}')
    if dialogue_max >= 0.5:
        reasons.append(f'与其它版本对白文本重合度最高 {dialogue_max:.1%}')
    same_drama_accounts = sorted({r.get('account') for r in upload_records
                                  if r.get('account')})
    if same_drama_accounts:
        reasons.append('本机已有该剧上传记录，涉及账号：' +
                       '、'.join(same_drama_accounts))

    if source_ratio >= 0.9 or exact_reuse or (visual_max or 0) >= 0.75:
        level = 'high'
    elif (visual_max or 0) >= 0.5 or dialogue_max >= 0.5 or same_drama_accounts:
        level = 'medium'
    else:
        level = 'low'
    return {
        'level': level,
        'source_ratio': round(source_ratio, 4),
        'reasons': reasons,
        'bulk_publish_allowed': False if level == 'high' else None,
    }


def build_cut_insights(output_root, drama_name, source_root=None, db_path=None):
    """构建 Web 可直接渲染的版本差异和内容风险视图。"""
    preview = build_cut_preview(
        output_root, drama_name, source_root=source_root)
    if preview is None:
        return None

    found = _find_output_dir(output_root, drama_name)
    if found is None:
        return None
    _account, out_dir = found
    edl_sources = _load_edl_sources(out_dir)
    first_source = next((v.get('source_dir') for v in edl_sources.values()
                         if v.get('source_dir')), '')
    transcripts = _load_transcripts(first_source)
    fingerprints = _load_visual_fingerprints(db_path)
    upload_records = _load_upload_records(db_path, drama_name)

    versions = preview.get('versions') or []
    all_keys = []
    for version in versions:
        keys = []
        for segment in version.get('segments') or []:
            keys.append((int(segment['episode']),
                         round(float(segment['start']), 1)))
        all_keys.extend(keys)
        version['_segment_keys'] = keys

    key_counts = {}
    for key in all_keys:
        key_counts[key] = key_counts.get(key, 0) + 1

    for version in versions:
        config = edl_sources.get(int(version.get('version', 0)), {})
        video_path = (config.get('artifacts') or {}).get('video_path')
        dialogue_parts = []
        dialogue_grams = set()
        for segment in version.get('segments') or []:
            text, grams = _segment_text(segment, transcripts)
            segment['dialogue_excerpt'] = text
            if text:
                dialogue_parts.append(text)
            dialogue_grams.update(grams)
        version['_video_path'] = video_path
        version['_dialogue_grams'] = dialogue_grams
        version['dialogue_excerpt'] = ''.join(dialogue_parts)[:220]
        version['exact_reuse_count'] = sum(
            1 for key in version['_segment_keys'] if key_counts.get(key, 0) > 1)
        version['source_episodes'] = sorted({s['episode'] for s in
                                             version.get('segments') or []})
        version['source_seconds'] = round(sum(
            float(s.get('duration', 0) or 0)
            for s in version.get('segments') or []), 1)

    pair_rows = []
    for i in range(len(versions)):
        for j in range(i + 1, len(versions)):
            a, b = versions[i], versions[j]
            visual = _visual_pair(a.get('_video_path'), b.get('_video_path'),
                                  fingerprints)
            dialogue = _set_similarity(a['_dialogue_grams'], b['_dialogue_grams'])
            shared = len(set(a['_segment_keys']) & set(b['_segment_keys']))
            pair_rows.append({
                'version_a': a.get('version'),
                'version_b': b.get('version'),
                'visual_similarity': visual,
                'dialogue_similarity': dialogue,
                'shared_segments': shared,
            })

    for version in versions:
        own = int(version.get('version', 0))
        related = [p for p in pair_rows if p['version_a'] == own or
                   p['version_b'] == own]
        visual_values = [p['visual_similarity'] for p in related
                         if p['visual_similarity'] is not None]
        dialogue_values = [p['dialogue_similarity'] for p in related]
        version['visual_max'] = max(visual_values) if visual_values else None
        version['dialogue_max'] = max(dialogue_values) if dialogue_values else 0.0
        version['risk'] = _risk_for_version(
            version, version['visual_max'], version['dialogue_max'],
            version['exact_reuse_count'], upload_records)
        version.pop('_segment_keys', None)
        version.pop('_dialogue_grams', None)
        version.pop('_video_path', None)

    visual_known = [p['visual_similarity'] for p in pair_rows
                    if p['visual_similarity'] is not None]
    high = sum(1 for v in versions if v['risk']['level'] == 'high')
    medium = sum(1 for v in versions if v['risk']['level'] == 'medium')
    low = sum(1 for v in versions if v['risk']['level'] == 'low')
    overall_level = 'high' if high else ('medium' if medium else 'low')
    return {
        'drama_name': preview.get('drama_name'),
        'account': preview.get('account'),
        'version_count': preview.get('version_count', 0),
        'rendered_count': preview.get('rendered_count', 0),
        'planned_count': preview.get('planned_count', 0),
        'summary': {
            'segments_total': len(all_keys),
            'segments_unique': len(set(all_keys)),
            'exact_reuse_segments': len(all_keys) - len(set(all_keys)),
            'visual_pairs_available': len(visual_known),
            'visual_max': max(visual_known) if visual_known else None,
            'risk_high': high,
            'risk_medium': medium,
            'risk_low': low,
        },
        'content_risk': {
            'level': overall_level,
            'source_only': True if versions else False,
            'bulk_publish_allowed': False if overall_level == 'high' else None,
            'note': ('本机只能核验生成成片、文本和本地上传记录；无法读取'
                     '视频号平台已发表视频库，平台侧仍可能需要人工逐条验证。'),
        },
        'platform_comparison': {
            'available': False,
            'reason': '平台未提供已发表内容比对接口',
        },
        'local_upload_records': upload_records,
        'pairs': pair_rows,
        'versions': versions,
    }
