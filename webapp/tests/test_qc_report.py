# -*- coding: utf-8 -*-
"""A7 QC 报告：blackdetect / 响度 / 词中切断。

验收：
- check_black_frames：真实 ffmpeg 黑帧素材 → 检出黑帧段；兼容接口失败 → []。
- measure_loudness：真实 ffmpeg sine → {i, tp}；兼容接口失败 → None。
- find_mid_word_cuts：切点落在转写句中间 → 检出；贴句边界（<0.3s）→ 不检。
- generate_qc：汇总三项，并明确区分 pass / warning / unknown。
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

sys_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
sys.path.insert(0, sys_path)

from qc_report import (check_black_frames, check_black_frames_detailed,
                       measure_loudness, find_mid_word_cuts, generate_qc,
                       MID_WORD_MARGIN, STATUS_PASS, STATUS_UNKNOWN,
                       STATUS_WARNING)

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

    def test_missing_file_is_unknown_not_pass(self):
        result = check_black_frames_detailed(
            os.path.join(self.tmp, 'nope.mp4'), FFMPEG)
        self.assertEqual(result['status'], STATUS_UNKNOWN)
        self.assertIn('不存在', result['error'])

    def test_command_failure_is_unknown(self):
        src = os.path.join(self.tmp, 'input.mp4')
        with open(src, 'wb') as f:
            f.write(b'not-video')
        with mock.patch('qc_report._run_ffmpeg') as run:
            run.return_value = mock.MagicMock(returncode=1, stderr='boom')
            result = check_black_frames_detailed(src, FFMPEG)
        self.assertEqual(result['status'], STATUS_UNKNOWN)
        self.assertIn('boom', result['error'])


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

    def test_missing_transcripts_is_unknown(self):
        segs = [{'episode': 1, 'start': 2.5, 'duration': 10}]
        qc = generate_qc('x.mp4', FFMPEG, segments=segs, transcripts=None)
        self.assertEqual(qc['mid_word_cuts_status'], STATUS_UNKNOWN)
        self.assertFalse(qc['mid_word_cuts'])

    def test_no_cut_is_pass(self):
        segs = [{'episode': 1, 'start': 5.0, 'duration': 10}]
        qc = generate_qc('x.mp4', FFMPEG, segments=segs,
                         transcripts=self.transcripts)
        self.assertEqual(qc['mid_word_cuts_status'], STATUS_PASS)

    def test_cut_is_warning(self):
        segs = [{'episode': 1, 'start': 2.5, 'duration': 10}]
        qc = generate_qc('x.mp4', FFMPEG, segments=segs,
                         transcripts=self.transcripts)
        self.assertEqual(qc['mid_word_cuts_status'], STATUS_WARNING)


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
        self.assertIn(qc['black_status'], (STATUS_PASS, STATUS_WARNING))
        self.assertIn(qc['loudness_status'], (STATUS_PASS, STATUS_WARNING))
        self.assertEqual(qc['mid_word_cuts_status'], STATUS_WARNING)
        self.assertEqual(qc['overall_status'], STATUS_WARNING)

    def test_missing_video_is_unknown_overall(self):
        qc = generate_qc(os.path.join(self.tmp, 'nope.mp4'), FFMPEG)
        self.assertEqual(qc['black_status'], STATUS_UNKNOWN)
        self.assertEqual(qc['loudness_status'], STATUS_UNKNOWN)
        self.assertEqual(qc['mid_word_cuts_status'], STATUS_UNKNOWN)
        self.assertEqual(qc['overall_status'], STATUS_UNKNOWN)


class QcReportStatusTests(unittest.TestCase):
    def test_unknown_is_not_rendered_as_pass(self):
        from processor import PipelineProcessor
        p = PipelineProcessor(mock.MagicMock(), mock.MagicMock())
        unknown = {
            'black_segments': [],
            'black_status': STATUS_UNKNOWN,
            'black_error': 'ffmpeg 不存在',
            'loudness': None,
            'loudness_status': STATUS_UNKNOWN,
            'loudness_error': 'ffmpeg 不存在',
            'mid_word_cuts': [],
            'mid_word_cuts_status': STATUS_UNKNOWN,
            'mid_word_cuts_error': '缺少源素材转写',
            'overall_status': STATUS_UNKNOWN,
        }
        with tempfile.TemporaryDirectory(prefix='qc_report_') as d:
            with mock.patch('qc_report.generate_qc', return_value=unknown):
                p._generate_qc_report(
                    [{'filename': '测试剧_1_打脸_去重版.mp4', 'path': 'x.mp4'}],
                    d, '测试剧')
            report = open(os.path.join(d, '测试剧_QC报告.md'),
                          encoding='utf-8').read()
        self.assertIn('黑帧检测未完成', report)
        self.assertIn('响度测量未完成', report)
        self.assertIn('词中切断检测未完成', report)
        self.assertIn('有未检测项', report)
        self.assertNotIn('无黑帧段', report)


if __name__ == '__main__':
    unittest.main()
