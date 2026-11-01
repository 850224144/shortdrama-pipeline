# -*- coding: utf-8 -*-
"""A7 QC 报告：blackdetect / 响度 / 词中切断。

验收：
- check_black_frames：真实 ffmpeg 黑帧素材 → 检出黑帧段；失败 → []。
- measure_loudness：真实 ffmpeg sine → {i, tp}；失败 → None。
- find_mid_word_cuts：切点落在转写句中间 → 检出；贴句边界（<0.3s）→ 不检；
  转写缺失 → []。
- generate_qc：汇总三项。
"""

import os
import shutil
import subprocess
import tempfile
import unittest

sys_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
sys.path.insert(0, sys_path)

from qc_report import (check_black_frames, measure_loudness,
                       find_mid_word_cuts, generate_qc, MID_WORD_MARGIN)

FFMPEG = r'D:\ffmpeg-master-latest-win64-gpl-shared\bin\ffmpeg.exe'


def _make_black_clip(tmp, name='black_clip.mp4'):
    """1.5s 黑 + 2.5s 画面（硬切）→ 检出 1.5s 黑帧段。"""
    src = os.path.join(tmp, name)
    cmd = [
        FFMPEG, '-v', 'error', '-y',
        '-f', 'lavfi', '-i', 'color=black:duration=1.5:size=320x240:rate=25',
        '-f', 'lavfi', '-i', 'testsrc=duration=2.5:size=320x240:rate=25',
        '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0',
        '-pix_fmt', 'yuv420p', src,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-200:])
    return src


def _make_sine_clip(tmp, name='sine_clip.mp4', dur=4):
    src = os.path.join(tmp, name)
    cmd = [
        FFMPEG, '-v', 'error', '-y',
        '-f', 'lavfi', '-i', f'testsrc=duration={dur}:size=320x240:rate=25',
        '-f', 'lavfi', '-i', f'sine=frequency=440:duration={dur}',
        '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k', src,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-200:])
    return src


class BlackFrameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='qc_black_')
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_detects_black_segment(self):
        src = _make_black_clip(self.tmp)
        segs = check_black_frames(src, FFMPEG)
        self.assertTrue(segs, '1.5s 黑帧应被检出')
        self.assertTrue(any(s['end'] - s['start'] >= 1.4 for s in segs),
                        f'黑帧段应≥1.4s: {segs}')

    def test_missing_file_returns_empty(self):
        self.assertEqual(
            check_black_frames(os.path.join(self.tmp, 'nope.mp4'), FFMPEG),
            [])


class LoudnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='qc_loud_')
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_measures_lufs_tp(self):
        src = _make_sine_clip(self.tmp)
        ln = measure_loudness(src, FFMPEG)
        self.assertIsNotNone(ln)
        self.assertIsNotNone(ln['i'])
        self.assertLess(ln['i'], 0)  # 响度为负 LUFS

    def test_missing_file_returns_none(self):
        self.assertIsNone(
            measure_loudness(os.path.join(self.tmp, 'nope.mp4'), FFMPEG))


class MidWordCutTests(unittest.TestCase):
    def setUp(self):
        self.transcripts = {
            1: [
                {'start': 0.0, 'end': 5.0, 'text': '第一句'},
                {'start': 5.0, 'end': 10.0, 'text': '第二句'},
            ],
        }

    def test_detects_cut_in_sentence(self):
        segs = [{'episode': 1, 'start': 2.5, 'duration': 10}]
        cuts = find_mid_word_cuts(segs, self.transcripts)
        self.assertEqual(len(cuts), 1)
        self.assertEqual(cuts[0]['episode'], 1)
        self.assertIn('第一句', cuts[0]['text'])

    def test_no_cut_near_boundary(self):
        # 5.2 距句1 起点 5.0 仅 0.2 < 0.3 → 贴边界不算词中
        segs = [{'episode': 1, 'start': 5.2, 'duration': 10}]
        self.assertEqual(find_mid_word_cuts(segs, self.transcripts), [])

    def test_no_cut_at_start(self):
        # 0.1 距句0 起点 0.0 仅 0.1 < 0.3 → 贴句起点不算词中
        segs = [{'episode': 1, 'start': 0.1, 'duration': 10}]
        self.assertEqual(find_mid_word_cuts(segs, self.transcripts), [])

    def test_no_transcripts_skips(self):
        segs = [{'episode': 1, 'start': 2.5, 'duration': 10}]
        self.assertEqual(find_mid_word_cuts(segs, None), [])
        self.assertEqual(find_mid_word_cuts(segs, {}), [])

    def test_no_segments_skips(self):
        self.assertEqual(find_mid_word_cuts(None, self.transcripts), [])

    def test_margin_constant(self):
        self.assertEqual(MID_WORD_MARGIN, 0.3)


class GenerateQcTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='qc_gen_')
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_generate_aggregates(self):
        src = _make_sine_clip(self.tmp)
        qc = generate_qc(src, FFMPEG, segments=[{'episode': 1, 'start': 1.0,
                                                 'duration': 2}],
                         transcripts={1: [{'start': 0.0, 'end': 3.0,
                                           'text': 'x'}]})
        self.assertIn('black_segments', qc)
        self.assertIn('loudness', qc)
        self.assertIn('mid_word_cuts', qc)
        self.assertIsNotNone(qc['loudness'])


if __name__ == '__main__':
    unittest.main()
