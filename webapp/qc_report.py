# -*- coding: utf-8 -*-
"""A7 QC 报告：成片质量检查（blackdetect / 响度 / 词中切断）。

纯函数模块，供 processor 在渲染后交付前调用。所有检查失败保守返回空值
（不阻塞交付——QC 是审计报告，不是门禁）。ffmpeg 路径由调用方注入。
"""

import os
import re
import subprocess

# 词中切断判定：切点距句起点/终点都 > MID_WORD_MARGIN 秒 → 落在词中间
MID_WORD_MARGIN = 0.3
# 黑帧判定：连续黑帧 ≥ BLACK_DURATION 秒、像素阈值 ≤ BLACK_PIX_TH
BLACK_DURATION = 1.0
BLACK_PIX_TH = 0.10


def _run_ffmpeg(cmd, timeout=180):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=timeout)


def check_black_frames(video_path, ffmpeg_path, duration=BLACK_DURATION,
                       pix_th=BLACK_PIX_TH):
    """blackdetect 检测黑帧段。返回 [{start, end}]；失败 → []。"""
    try:
        if not video_path or not os.path.exists(video_path):
            return []
        cmd = [
            ffmpeg_path, '-hide_banner', '-i', video_path,
            '-vf', f'blackdetect=d={duration}:pix_th={pix_th}',
            '-an', '-f', 'null', '-',
        ]
        r = _run_ffmpeg(cmd)
        segments = []
        for line in (r.stderr or '').splitlines():
            ms = re.search(r'black_start:([\d.]+)', line)
            me = re.search(r'black_end:([\d.]+)', line)
            if ms and me:
                segments.append({'start': float(ms.group(1)),
                                 'end': float(me.group(1))})
        return segments
    except Exception:
        return []


def measure_loudness(video_path, ffmpeg_path):
    """ebur128 测综合响度/真峰值。返回 {i, tp}；失败 → None。"""
    try:
        if not video_path or not os.path.exists(video_path):
            return None
        cmd = [ffmpeg_path, '-hide_banner', '-i', video_path,
               '-af', 'ebur128=peak=true', '-f', 'null', '-']
        r = _run_ffmpeg(cmd)
        vals = re.findall(r'I:\s*(-?\d+(?:\.\d+)?)\s*LUFS', r.stderr)
        tps = re.findall(r'TPK:\s*(-?\d+(?:\.\d+)?)\s*dBFS', r.stderr)
        if not vals:
            return None
        return {'i': float(vals[-1]),
                'tp': float(tps[-1]) if tps else None}
    except Exception:
        return None


def find_mid_word_cuts(segments, transcripts, margin=MID_WORD_MARGIN):
    """切点是否落在转写句中间（半句切）。

    segments: [{episode, start, duration}]——每个 start 是切点（第一段的
    start 是片头，也检查：片头从词中起同样算半句切）。
    transcripts: {episode: [{start, end, text}]}（源素材转写，秒）。

    返回 [{episode, t, text, margin}]；转写缺失 → []（跳过）。
    """
    cuts = []
    if not segments or not transcripts:
        return cuts
    for seg in segments:
        ep = seg.get('episode')
        t = seg.get('start')
        if ep is None or t is None:
            continue
        for s in transcripts.get(ep, []):
            st = float(s.get('start', 0))
            en = float(s.get('end', st))
            if st + margin < t < en - margin:
                cuts.append({
                    'episode': ep,
                    't': round(t, 1),
                    'text': str(s.get('text', ''))[:20],
                    'margin': round(min(t - st, en - t), 1),
                })
                break
    return cuts


def generate_qc(video_path, ffmpeg_path, segments=None, transcripts=None):
    """单版本 QC 汇总。返回 dict；任何单项失败为 None/[]（审计不阻塞）。"""
    return {
        'black_segments': check_black_frames(video_path, ffmpeg_path),
        'loudness': measure_loudness(video_path, ffmpeg_path),
        'mid_word_cuts': find_mid_word_cuts(segments, transcripts),
    }
