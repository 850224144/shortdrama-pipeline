# -*- coding: utf-8 -*-
"""A6 音乐卡点：BGM 节拍检测（numpy 能量包络）+ 切点吸附。

验收：
- snap_to_beat：容差内吸附最近节拍；超容差不吸附；空 beats 放行。
- detect_beats：真实 120BPM 脉冲 WAV → 检出 ~0.5s 间隔节拍。
- detect_beats 静音/过短 → []（放行）。
- extract_audio_samples：sine → 非 None 且样本数合理。
"""

import os
import shutil
import subprocess
import tempfile
import unittest
import wave

import numpy as np

sys_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys_path_parent = os.path.join(os.path.dirname(sys_path), 'scripts')
import sys
sys.path.insert(0, sys_path)
sys.path.insert(0, sys_path_parent)

from beat_sync import detect_beats, snap_to_beat, extract_audio_samples

FFMPEG = r'D:\ffmpeg-master-latest-win64-gpl-shared\bin\ffmpeg.exe'
RATE = 8000


def _make_beat_wav(path, bpm=120, dur=10.0):
    """生成 bpm 节奏的短脉冲 WAV（20ms 指数衰减，每拍一次）。"""
    beat_interval = 60.0 / bpm
    n = int(RATE * dur)
    samples = np.zeros(n, dtype=np.float32)
    pulse_len = int(0.02 * RATE)
    t = 0.0
    while t < dur - 0.05:
        i0 = int(t * RATE)
        if i0 + pulse_len < n:
            tt = np.arange(pulse_len) / RATE
            samples[i0:i0 + pulse_len] = np.sin(
                2 * np.pi * 1000 * tt) * np.exp(-tt * 200)
        t += beat_interval
    # 归一化 + 写 16-bit WAV
    samples = samples / (np.max(np.abs(samples)) + 1e-9)
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((samples * 32767).astype(np.int16).tobytes())
    return path


class SnapToBeatTests(unittest.TestCase):
    def test_snap_in_tol(self):
        self.assertEqual(snap_to_beat(1.05, [1.0, 2.0], tol=0.2), 1.0)

    def test_no_snap_outside_tol(self):
        self.assertEqual(snap_to_beat(1.5, [1.0, 2.0], tol=0.2), 1.5)

    def test_empty_beats_passthrough(self):
        self.assertEqual(snap_to_beat(1.5, [], tol=0.2), 1.5)
        self.assertEqual(snap_to_beat(1.5, None, tol=0.2), 1.5)

    def test_custom_tol(self):
        # tol=0.5：1.6 距 1.0 为 0.6（>tol），距 2.0 为 0.4（≤tol）→ 吸附 2.0
        self.assertEqual(snap_to_beat(1.6, [1.0, 2.0], tol=0.5), 2.0)


class DetectBeatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='beat_')
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_detects_120bpm(self):
        wav = _make_beat_wav(os.path.join(self.tmp, 'bgm.wav'), bpm=120)
        beats = detect_beats(wav, ffmpeg_path=FFMPEG)
        self.assertGreaterEqual(len(beats), 15,
                                f'120BPM/10s 应检出≥15拍: {len(beats)}')
        # 节拍间隔 ≈ 0.5s（±0.15s 容差）
        gaps = [b2 - b1 for b1, b2 in zip(beats, beats[1:])]
        self.assertTrue(all(0.35 <= g <= 0.65 for g in gaps),
                        f'节拍间隔应≈0.5s: {gaps[:8]}')

    def test_missing_file_returns_empty(self):
        self.assertEqual(
            detect_beats(os.path.join(self.tmp, 'nope.wav'),
                         ffmpeg_path=FFMPEG), [])

    def test_silence_returns_empty(self):
        wav = os.path.join(self.tmp, 'silence.wav')
        with wave.open(wav, 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(b'\x00\x00' * RATE * 6)
        self.assertEqual(detect_beats(wav, ffmpeg_path=FFMPEG), [])


class ExtractSamplesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='beatext_')
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_extracts_sine(self):
        wav = _make_beat_wav(os.path.join(self.tmp, 'bgm.wav'))
        samples = extract_audio_samples(wav, FFMPEG)
        self.assertIsNotNone(samples)
        self.assertGreater(len(samples), RATE * 5)

    def test_missing_returns_none(self):
        self.assertIsNone(
            extract_audio_samples(os.path.join(self.tmp, 'nope.wav'), FFMPEG))


if __name__ == '__main__':
    unittest.main()
