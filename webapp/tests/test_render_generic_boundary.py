# -*- coding: utf-8 -*-
"""validate_source 边界容差回归测试。

根因（2026-10-29）：EDL 生成端 round(,1) 四舍五入 + 浮点/ASR 时长微差，
使片段结束点（如 0.2+43.0=43.2s）略超源文件真实时长（43.17s），
validate_source 用严格 `start+duration > total` 误判超界 → 8 版全失败。

修复：加 _BOUNDARY_EPS=0.1 容差（覆盖舍入/浮点/微差），真实超界仍失败。
"""
import sys
import os
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'scripts'))
import render_generic as rg


class TestValidateSourceBoundary(unittest.TestCase):
    def _call(self, start, duration, total=43.17):
        """在 mock 生效期间调用 validate_source"""
        with mock.patch.object(rg.os.path, 'exists', return_value=True), \
             mock.patch.object(rg, 'probe_duration', return_value=total):
            return rg.validate_source(r'X:\源.mp4', start, duration)

    def test_rounding_endpoint_passes(self):
        """结束点 round 后略超真实时长（差<EPS）→ 通过，不抛"""
        total = self._call(0.2, 43.0, total=43.17)
        self.assertAlmostEqual(total, 43.17)

    def test_exact_endpoint_passes(self):
        """结束点恰等于真实时长（浮点差）→ 通过"""
        total = self._call(0.2, 43.0, total=43.2)
        self.assertAlmostEqual(total, 43.2)

    def test_real_exceed_still_fails(self):
        """真实超界（差>EPS）→ 抛 ValueError"""
        with self.assertRaises(ValueError) as cm:
            self._call(0.2, 50.0, total=43.17)
        self.assertIn('超出源文件时长', str(cm.exception))

    def test_negative_start_fails(self):
        with self.assertRaises(ValueError):
            self._call(-1.0, 5.0, total=43.17)

    def test_missing_file_fails(self):
        with mock.patch.object(rg.os.path, 'exists', return_value=False):
            with self.assertRaises(FileNotFoundError):
                rg.validate_source(r'X:\不存在.mp4', 0.0, 5.0)


if __name__ == '__main__':
    unittest.main()
