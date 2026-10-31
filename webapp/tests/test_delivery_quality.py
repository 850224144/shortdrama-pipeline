# -*- coding: utf-8 -*-
"""R6 阶段指标与媒体质量门禁。

验收：
- 任务事件包含阶段耗时（cut_completed/cut_failed/cut_stopped 带 duration_seconds）。
- 无音轨/无视频流/0 字节/时长异常被拒收（_run_render_one 返回 'quality'，不进入交付）。
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import delivery_quality as dq
from processor import PipelineProcessor


def _mk():
    return PipelineProcessor(mock.MagicMock(), mock.MagicMock())


def _stream(codec_type):
    return {'codec_type': codec_type}


def _info(duration, video=True, audio=True):
    streams = []
    if video:
        streams.append(_stream('video'))
    if audio:
        streams.append(_stream('audio'))
    return {'streams': streams, 'format': {'duration': str(duration)}}


class ValidateMediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='dq_')
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmp, ignore_errors=True))

    def _file(self, name='x.mp4', size=100):
        p = os.path.join(self.tmp, name)
        with open(p, 'wb') as f:
            f.write(b'\x00' * size)
        return p

    def test_missing_file_rejected(self):
        r = dq.validate_media(os.path.join(self.tmp, 'nope.mp4'))
        self.assertFalse(r['ok'])
        self.assertIn('不存在', r['reason'])

    def test_zero_byte_rejected(self):
        r = dq.validate_media(self._file(size=0))
        self.assertFalse(r['ok'])
        self.assertIn('0 字节', r['reason'])

    def test_no_video_rejected(self):
        p = self._file()
        with mock.patch.object(dq, 'probe_media',
                               return_value=_info(30, video=False, audio=True)):
            r = dq.validate_media(p)
        self.assertFalse(r['ok'])
        self.assertIn('无视频流', r['reason'])

    def test_no_audio_rejected(self):
        p = self._file()
        with mock.patch.object(dq, 'probe_media',
                               return_value=_info(30, video=True, audio=False)):
            r = dq.validate_media(p)
        self.assertFalse(r['ok'])
        self.assertIn('无音轨', r['reason'])

    def test_zero_duration_rejected(self):
        p = self._file()
        with mock.patch.object(dq, 'probe_media', return_value=_info(0)):
            r = dq.validate_media(p)
        self.assertFalse(r['ok'])
        self.assertIn('时长为 0', r['reason'])

    def test_duration_out_of_window_rejected(self):
        p = self._file()
        with mock.patch.object(dq, 'probe_media', return_value=_info(5)):
            r = dq.validate_media(p, min_duration=10)
        self.assertFalse(r['ok'])
        self.assertIn('时长过短', r['reason'])
        with mock.patch.object(dq, 'probe_media', return_value=_info(500)):
            r = dq.validate_media(p, max_duration=300)
        self.assertFalse(r['ok'])
        self.assertIn('时长过长', r['reason'])

    def test_valid_media_ok(self):
        p = self._file()
        with mock.patch.object(dq, 'probe_media', return_value=_info(43.5)):
            r = dq.validate_media(p, min_duration=10, max_duration=300)
        self.assertTrue(r['ok'], r['reason'])
        self.assertAlmostEqual(r['duration'], 43.5)
        self.assertTrue(r['has_video'])
        self.assertTrue(r['has_audio'])

    def test_probe_failure_conservative_reject(self):
        p = self._file()
        with mock.patch.object(dq, 'probe_media', return_value=None):
            r = dq.validate_media(p)
        self.assertFalse(r['ok'])
        self.assertIn('无法解析', r['reason'])


class MediaGateIntegrationTests(unittest.TestCase):
    """_run_render_one 对质量拒收返回 ('quality')，不进入交付。"""

    def _setup_render(self, d):
        mp4 = os.path.join(d, '测试剧_1_打脸_去重版.mp4')
        open(mp4, 'w').close()
        return mp4

    def test_rejected_media_returns_quality_failure(self):
        p = _mk()
        with tempfile.TemporaryDirectory() as d:
            self._setup_render(d)
            with mock.patch('processor.subprocess.run') as mr, \
                 mock.patch.object(p, '_update_config_artifacts'), \
                 mock.patch.object(p, '_validate_delivery_media',
                                   return_value={'ok': False, 'reason': '0 字节'}):
                mr.return_value = mock.MagicMock(returncode=0, stderr='')
                video, fail_cat = p._run_render_one('测试剧', d, 0, 'cfg.json')
            self.assertIsNone(video)
            self.assertEqual(fail_cat, 'quality')

    def test_accepted_media_returns_video(self):
        p = _mk()
        with tempfile.TemporaryDirectory() as d:
            self._setup_render(d)
            with mock.patch('processor.subprocess.run') as mr, \
                 mock.patch.object(p, '_update_config_artifacts') as m_upd, \
                 mock.patch.object(p, '_validate_delivery_media',
                                   return_value={'ok': True, 'reason': ''}):
                mr.return_value = mock.MagicMock(returncode=0, stderr='')
                video, fail_cat = p._run_render_one('测试剧', d, 0, 'cfg.json')
            self.assertIsNone(fail_cat)
            self.assertIsNotNone(video)
            self.assertEqual(video['filename'], '测试剧_1_打脸_去重版.mp4')


if __name__ == '__main__':
    unittest.main()
