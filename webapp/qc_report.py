# -*- coding: utf-8 -*-
"""A7 QC 报告：成片质量检查（blackdetect / 响度 / 词中切断）。

纯函数模块，供 processor 在渲染后交付前调用。QC 是审计报告，不是交付
门禁；但“检测失败”和“确认通过”必须区分，不能把未知结果展示为通过。
ffmpeg 路径由调用方注入。
"""

import os
import re
import subprocess

# 词中切断判定：切点距句起点/终点都 > MID_WORD_MARGIN 秒 → 落在词中间
MID_WORD_MARGIN = 0.3
# 黑帧判定：连续黑帧 ≥ BLACK_DURATION 秒、像素阈值 ≤ BLACK_PIX_TH
BLACK_DURATION = 1.0
BLACK_PIX_TH = 0.10
# 响度目标与允许偏差（与 processor 的 QC 报告口径一致）
LOUDNESS_TARGET = -14.0
LOUDNESS_TOLERANCE = 2.0

STATUS_PASS = 'pass'
STATUS_WARNING = 'warning'
STATUS_UNKNOWN = 'unknown'


def _run_ffmpeg(cmd, timeout=180):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=timeout)


def _stderr_tail(result, limit=300):
    return str(getattr(result, 'stderr', '') or '').strip()[-limit:]


def check_black_frames_detailed(video_path, ffmpeg_path,
                                duration=BLACK_DURATION,
                                pix_th=BLACK_PIX_TH):
    """blackdetect 检测黑帧段，并返回可区分的检测状态。

    返回 ``{status, segments, error}``：
    - pass: 命令成功且未发现黑帧段
    - warning: 检出黑帧段
    - unknown: 文件/ffmpeg 缺失、命令失败或输出无法解析
    """
    result = {'status': STATUS_UNKNOWN, 'segments': [], 'error': ''}
    try:
        if not video_path or not os.path.exists(video_path):
            result['error'] = '视频文件不存在'
            return result
        if not ffmpeg_path or not os.path.exists(ffmpeg_path):
            result['error'] = 'ffmpeg 不存在'
            return result
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
        result['segments'] = segments
        if getattr(r, 'returncode', 1) != 0 and not segments:
            result['error'] = _stderr_tail(r) or 'blackdetect 命令失败'
            return result
        result['status'] = STATUS_WARNING if segments else STATUS_PASS
        return result
    except Exception as exc:
        result['error'] = str(exc) or exc.__class__.__name__
        return result


def check_black_frames(video_path, ffmpeg_path, duration=BLACK_DURATION,
                       pix_th=BLACK_PIX_TH):
    """兼容接口：只返回黑帧段列表；需要区分未知状态请用 detailed 接口。"""
    return check_black_frames_detailed(
        video_path, ffmpeg_path, duration=duration, pix_th=pix_th,
    )['segments']


def measure_loudness_detailed(video_path, ffmpeg_path):
    """ebur128 测量综合响度/真峰值并评价偏差。

    返回 ``{status, loudness, error}``。status=warning 表示测量成功但偏离
    -14 LUFS 超过容差；unknown 表示测量失败。
    """
    result = {'status': STATUS_UNKNOWN, 'loudness': None, 'error': ''}
    try:
        if not video_path or not os.path.exists(video_path):
            result['error'] = '视频文件不存在'
            return result
        if not ffmpeg_path or not os.path.exists(ffmpeg_path):
            result['error'] = 'ffmpeg 不存在'
            return result
        cmd = [ffmpeg_path, '-hide_banner', '-i', video_path,
               '-af', 'ebur128=peak=true', '-f', 'null', '-']
        r = _run_ffmpeg(cmd)
        vals = re.findall(r'I:\s*(-?\d+(?:\.\d+)?)\s*LUFS', r.stderr)
        tps = re.findall(r'TPK:\s*(-?\d+(?:\.\d+)?)\s*dBFS', r.stderr)
        if not vals:
            result['error'] = _stderr_tail(r) or '未解析到响度'
            return result
        loudness = {'i': float(vals[-1]),
                    'tp': float(tps[-1]) if tps else None}
        result['loudness'] = loudness
        deviation = abs(loudness['i'] - LOUDNESS_TARGET)
        result['status'] = (STATUS_PASS if deviation <= LOUDNESS_TOLERANCE
                            else STATUS_WARNING)
        return result
    except Exception as exc:
        result['error'] = str(exc) or exc.__class__.__name__
        return result


def measure_loudness(video_path, ffmpeg_path):
    """兼容接口：只返回 {i, tp}；失败 → None。"""
    return measure_loudness_detailed(video_path, ffmpeg_path)['loudness']


def find_mid_word_cuts(segments, transcripts, margin=MID_WORD_MARGIN):
    """切点是否落在转写句中间（半句切）。

    segments: [{episode, start, duration}]——每个 start 是切点（第一段的
    start 是片头，也检查：片头从词中起同样算半句切）。
    transcripts: {episode: [{start, end, text}]}（源素材转写，秒）。

    返回 [{episode, t, text, margin}]。转写缺失时返回空列表；需要区分
    “无切断”和“未检测”请用 detailed 接口。
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


def check_mid_word_cuts_detailed(segments, transcripts,
                                 margin=MID_WORD_MARGIN):
    """检查词中切断，并区分通过、异常与缺少输入。"""
    result = {'status': STATUS_UNKNOWN, 'cuts': [], 'error': ''}
    if not segments:
        result['error'] = '缺少 EDL 片段'
        return result
    if not transcripts:
        result['error'] = '缺少源素材转写'
        return result

    try:
        cuts = find_mid_word_cuts(segments, transcripts, margin=margin)
        checked_episodes = {
            seg.get('episode') for seg in segments
            if seg.get('episode') in transcripts
        }
        # 有切点但没有任何可核对的集数，仍是未知，不能当成通过。
        if not checked_episodes and not cuts:
            result['error'] = '转写未覆盖 EDL 片段集数'
            return result
        result['cuts'] = cuts
        result['status'] = STATUS_WARNING if cuts else STATUS_PASS
        return result
    except Exception as exc:
        result['error'] = str(exc) or exc.__class__.__name__
        return result


def generate_qc(video_path, ffmpeg_path, segments=None, transcripts=None):
    """单版本 QC 汇总。

    保留原字段以兼容报告/调用方，同时新增每项 status/error 与 overall_status。
    QC 仍不阻塞交付，但报告可明确显示未检测，而不是误报为通过。
    """
    black = check_black_frames_detailed(video_path, ffmpeg_path)
    loudness = measure_loudness_detailed(video_path, ffmpeg_path)
    mid_word = check_mid_word_cuts_detailed(segments, transcripts)
    statuses = [black['status'], loudness['status'], mid_word['status']]
    if STATUS_WARNING in statuses:
        overall_status = STATUS_WARNING
    elif STATUS_UNKNOWN in statuses:
        overall_status = STATUS_UNKNOWN
    else:
        overall_status = STATUS_PASS
    return {
        'black_segments': black['segments'],
        'black_status': black['status'],
        'black_error': black['error'],
        'loudness': loudness['loudness'],
        'loudness_status': loudness['status'],
        'loudness_error': loudness['error'],
        'mid_word_cuts': mid_word['cuts'],
        'mid_word_cuts_status': mid_word['status'],
        'mid_word_cuts_error': mid_word['error'],
        'overall_status': overall_status,
    }
