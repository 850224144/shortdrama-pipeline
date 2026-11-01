# -*- coding: utf-8 -*-
"""A1 镜头边界吸附：scene 检测 + 切点吸附（修复半帧切/转写 start 带前镜头）。

验收：
- _snap_scene_start：容差内吸附最近边界；超容差不吸附；空边界放行；
  向后吸附会超源时长 → 只允许向前或保持原值。
- _scene_boundaries：真实 ffmpeg 硬切素材 → 检测到边界。
- 禁用 scene_snap → 全部保守放行（不吸附）。
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

from processor import PipelineProcessor, CONFIG

FFMPEG = CONFIG['ffmpeg_path']


def _make_hard_cut(tmp, name='src.mp4'):
    """生成含一个硬切（2.0s）的 4s 素材。"""
    src = os.path.join(tmp, name)
    cmd = [
        FFMPEG, '-v', 'error', '-y',
        '-f', 'lavfi', '-i', 'testsrc=duration=2:size=320x240:rate=25',
        '-f', 'lavfi', '-i', 'smptebars=duration=2:size=320x240:rate=25',
        '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0',
        '-pix_fmt', 'yuv420p', src,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-200:])
    return src


class SnapLogicTests(unittest.TestCase):
    """_snap_scene_start 纯逻辑（不调 ffmpeg）。"""

    def setUp(self):
        self.proc = PipelineProcessor(mock.MagicMock(), mock.MagicMock())
        self.proc.logger = mock.MagicMock()
        self._orig = CONFIG.get('scene_snap')
        CONFIG['scene_snap'] = dict(self._orig or {})

    def tearDown(self):
        CONFIG['scene_snap'] = self._orig

    def test_snap_to_nearest_in_tol(self):
        # 2.05s 距 2.0s 边界 0.05 ≤ 0.15 → 吸附
        self.assertEqual(
            self.proc._snap_scene_start(2.05, 10, 100, [2.0, 5.0]), 2.0)

    def test_no_snap_outside_tol(self):
        # 2.5s 距 2.0/5.0 边界 0.5/2.5 > 0.15 → 不吸附
        self.assertEqual(
            self.proc._snap_scene_start(2.5, 10, 100, [2.0, 5.0]), 2.5)

    def test_empty_boundaries_passthrough(self):
        self.assertEqual(
            self.proc._snap_scene_start(2.0, 10, 100, []), 2.0)
        self.assertEqual(
            self.proc._snap_scene_start(2.0, 10, 100, None), 2.0)

    def test_backward_snap_overrun_clamped(self):
        # 边界 95.0 向后吸附 → end=95+90=185 > 100 超界 → 只允许向前/保持
        # （无向前候选 → 保持 2.0）
        self.assertEqual(
            self.proc._snap_scene_start(2.0, 90, 100, [2.0, 95.0]), 2.0)

    def test_backward_snap_within_duration_allowed(self):
        # start=2.0 距 1.0 边界 1.0、距 2.05 边界 0.05 → 向后吸附 2.05
        # （end=2.05+20=22.05 ≤ 100 不超界 → 允许）
        self.assertEqual(
            self.proc._snap_scene_start(2.0, 20, 100, [1.0, 2.05]),
            2.05)

    def test_custom_tol(self):
        # tol=0.5 时 2.5s 距 2.0s 边界 0.5 ≤ 0.5 → 吸附
        self.assertEqual(
            self.proc._snap_scene_start(2.5, 10, 100, [2.0, 5.0], tol=0.5),
            2.0)


class SceneBoundaryTests(unittest.TestCase):
    """_scene_boundaries 真实 ffmpeg。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='scene_')
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.proc = PipelineProcessor(mock.MagicMock(), mock.MagicMock())
        self.proc.logger = mock.MagicMock()
        self._orig = CONFIG.get('scene_snap')
        CONFIG['scene_snap'] = dict(self._orig or {})

    def tearDown(self):
        CONFIG['scene_snap'] = self._orig

    def test_detects_hard_cut(self):
        src = _make_hard_cut(self.tmp)
        bounds = self.proc._scene_boundaries(src)
        self.assertTrue(bounds, '硬切素材应检测到边界')
        # 硬切在 2.0s，检测到 [~2.0]
        self.assertTrue(any(abs(b - 2.0) < 0.2 for b in bounds),
                        f'未检测到 2.0s 硬切边界: {bounds}')

    def test_detection_cached(self):
        src = _make_hard_cut(self.tmp)
        b1 = self.proc._scene_boundaries(src)
        b2 = self.proc._scene_boundaries(src)
        self.assertEqual(b1, b2, '同一源文件二次检测应命中缓存')

    def test_disabled_passthrough(self):
        CONFIG['scene_snap']['enabled'] = False
        src = _make_hard_cut(self.tmp)
        self.assertEqual(self.proc._scene_boundaries(src), [])

    def test_missing_file_passthrough(self):
        self.assertEqual(
            self.proc._scene_boundaries(os.path.join(self.tmp, 'nope.mp4')),
            [])


class SegmentSnapTests(unittest.TestCase):
    """_snap_segment_start 集成（mock scene 边界）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='segsnap_')
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.proc = PipelineProcessor(mock.MagicMock(), mock.MagicMock())
        self.proc.logger = mock.MagicMock()
        self._orig = CONFIG.get('scene_snap')
        CONFIG['scene_snap'] = dict(self._orig or {})
        self.src = os.path.join(self.tmp, 'ep1.mp4')
        with open(self.src, 'wb') as f:
            f.write(b'x' * 100)

    def tearDown(self):
        CONFIG['scene_snap'] = self._orig

    def test_snaps_via_eps_hl(self):
        eps_hl = [{'episode': 1, 'path': self.src, 'dur': 100.0,
                   'cands': []}]
        with mock.patch.object(self.proc, '_scene_boundaries',
                               return_value=[2.0, 5.0]):
            # 2.05 距 2.0 → 吸附
            self.assertEqual(
                self.proc._snap_segment_start(1, 2.05, 10, eps_hl), 2.0)

    def test_no_eps_match_passthrough(self):
        eps_hl = [{'episode': 2, 'path': self.src, 'dur': 100.0, 'cands': []}]
        with mock.patch.object(self.proc, '_scene_boundaries',
                               return_value=[2.0]):
            self.assertEqual(
                self.proc._snap_segment_start(1, 2.05, 10, eps_hl), 2.05)

    def test_no_boundaries_passthrough(self):
        eps_hl = [{'episode': 1, 'path': self.src, 'dur': 100.0, 'cands': []}]
        with mock.patch.object(self.proc, '_scene_boundaries',
                               return_value=[]):
            self.assertEqual(
                self.proc._snap_segment_start(1, 2.05, 10, eps_hl), 2.05)


if __name__ == '__main__':
    unittest.main()
