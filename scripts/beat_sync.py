#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A6 音乐卡点：BGM 节拍检测 + 切点吸附（numpy 能量包络，无 librosa 依赖）。

供 render_generic.py 在 config 提供 bgm 字段时使用（仅引流切片等需要
卡点的场景）。节拍检测失败 → 返回 []（放行原切点，不阻塞渲染）。

用法（模块内函数）：
    beats = detect_beats(bgm_path)          # 节拍秒列表
    t = snap_to_beat(seg_start, beats, tol) # 吸附切点到最近节拍
"""

import os
import subprocess

import numpy as np

FFMPEG = r"D:\ffmpeg-master-latest-win64-gpl-shared\bin\ffmpeg.exe"


def extract_audio_samples(bgm_path, ffmpeg_path=FFMPEG, rate=8000):
    """ffmpeg 提取单声道 8kHz PCM 到内存（float32 -1~1）。失败 → None。"""
    try:
        if not bgm_path or not os.path.exists(bgm_path):
            return None
        cmd = [ffmpeg_path, '-v', 'error', '-i', bgm_path,
               '-ac', '1', '-ar', str(rate), '-f', 's16le', '-']
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        if r.returncode != 0 or not r.stdout:
            return None
        return np.frombuffer(r.stdout, dtype=np.int16).astype(
            np.float32) / 32768.0
    except Exception:
        return None


def detect_beats(bgm_path, ffmpeg_path=FFMPEG, rate=8000, hop=0.05,
                 min_bpm=60, max_bpm=180):
    """能量包络 + 自适应阈值局部峰值 = 节拍。

    hop=0.05s（50ms 窗口）；节拍间隔限制在 min_bpm~max_bpm 对应范围，
    防过密/过疏误检。BGM 过短（<5s）或静音 → []（放行）。
    """
    samples = extract_audio_samples(bgm_path, ffmpeg_path, rate)
    if samples is None or len(samples) < rate * 5:
        return []
    win = max(1, int(rate * hop))
    n_win = len(samples) // win
    if n_win < 20:
        return []
    frames = samples[:n_win * win].reshape(n_win, win)
    energy = np.sqrt(np.mean(frames ** 2, axis=1))
    if float(energy.max()) < 1e-6:
        return []
    # 自适应阈值：均值 + 0.5*std（峰值超过背景噪声一半标准差即算拍）
    thr = float(energy.mean()) + 0.5 * float(energy.std())
    min_gap = 60.0 / max_bpm       # 最快拍间隔（秒）
    max_gap = 60.0 / min_bpm       # 最慢拍间隔
    beats = []
    last = -max_gap
    for i in range(1, n_win - 1):
        if (energy[i] > thr and energy[i] >= energy[i - 1]
                and energy[i] >= energy[i + 1]):
            t = i * hop
            if t - last < min_gap:
                continue
            if t - last > max_gap * 1.5:
                pass  # 允许跳过（BGM 停歇后重新起拍），不强制插入
            beats.append(round(t, 2))
            last = t
    return beats


def snap_to_beat(t, beats, tol=0.2):
    """把切点 t 吸附到最近节拍（容差 tol 秒内）。beats 空 → 原值。"""
    if not beats or t is None:
        return t
    best, bd = None, None
    for b in beats:
        d = abs(b - t)
        if d <= tol and (bd is None or d < bd):
            best, bd = b, d
    return best if best is not None else t
