# -*- coding: utf-8 -*-
"""fail-fast 防御（系统性错误停止）单测：
- _precheck_source_pattern：源头预检①（source_pattern 匹配实际源文件）
- _classify_render_failure：失败归类（source/encode/other），供连续失败②判定
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from processor import PipelineProcessor


def _mk():
    logger = mock.MagicMock()
    sm = mock.MagicMock()
    return PipelineProcessor(logger, sm), logger


class TestPrecheckSourcePattern(unittest.TestCase):
    def _dir_with(self, files):
        d = tempfile.mkdtemp()
        for f in files:
            with open(os.path.join(d, f), 'w', encoding='utf-8') as fh:
                fh.write('')
        return d

    def test_matches_name_dash_ep(self):
        """剧名-集号.mp4 格式：source_pattern {name}-{ep}.mp4 应通过。"""
        p, _ = _mk()
        d = self._dir_with(['前夫你来迟了-1.mp4', '前夫你来迟了-36.mp4'])
        self.assertIsNone(p._precheck_source_pattern(d, '{name}-{ep}.mp4', '前夫你来迟了'))

    def test_rejects_bare_ep(self):
        """BUG 场景复现：{ep}.mp4 匹配不到剧名-集号文件 → 预检拦下（应停止）。"""
        p, _ = _mk()
        d = self._dir_with(['前夫你来迟了-1.mp4', '前夫你来迟了-36.mp4'])
        err = p._precheck_source_pattern(d, '{ep}.mp4', '前夫你来迟了')
        self.assertIsNotNone(err)
        self.assertIn('无法匹配', err)

    def test_rejects_empty_dir(self):
        """空源目录 → 预检拦下。"""
        p, _ = _mk()
        d = self._dir_with([])
        err = p._precheck_source_pattern(d, '{ep}.mp4', 'x')
        self.assertIsNotNone(err)

    def test_matches_nested_subdir(self):
        """嵌套子目录（BaiduPCS-Go 产物）递归扫描能匹配。"""
        p, _ = _mk()
        d = tempfile.mkdtemp()
        sub = os.path.join(d, '前夫你来迟了')
        os.makedirs(sub)
        with open(os.path.join(sub, '前夫你来迟了-36.mp4'), 'w', encoding='utf-8') as fh:
            fh.write('')
        self.assertIsNone(p._precheck_source_pattern(d, '{name}-{ep}.mp4', '前夫你来迟了'))

    def test_matches_episode_suffix(self):
        """第X集.mp4 格式：{name}第{ep}集.mp4 应通过。"""
        p, _ = _mk()
        d = self._dir_with(['剧名第1集.mp4', '剧名第36集.mp4'])
        self.assertIsNone(p._precheck_source_pattern(d, '{name}第{ep}集.mp4', '剧名'))


class TestClassifyRenderFailure(unittest.TestCase):
    def _p(self):
        p, _ = _mk()
        return p

    def test_source_missing(self):
        self.assertEqual(self._p()._classify_render_failure('验证失败: 源文件不存在: X'), 'source')

    def test_encode_fail(self):
        self.assertEqual(self._p()._classify_render_failure('Error: Conversion failed!'), 'encode')

    def test_other_glitch(self):
        self.assertEqual(self._p()._classify_render_failure('random glitch'), 'other')

    def test_exc_filenotfound(self):
        self.assertEqual(self._p()._classify_render_failure('', FileNotFoundError('no')), 'source')

    def test_empty(self):
        self.assertEqual(self._p()._classify_render_failure('', None), 'other')


if __name__ == '__main__':
    unittest.main()
