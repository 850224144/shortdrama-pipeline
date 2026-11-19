# -*- coding: utf-8 -*-
"""官方封面查找回归测试。

根因（2026-11-19）：_find_official_cover 只认「文件名关键字 / 文件名含剧名 /
数字名且 0.3–2.0MB」三层，漏判两类真实海报：
  - 错嫁后我怀了三宝：海报就是 1.png，3.67MB 被 2.0MB 上限挡掉；
  - 重生后我不当后妈：海报用旧剧名「退婚当天，我夺回铁饭碗 (2).jpg」，
    关键字/剧名/数字名三层全不中。
现改为打分挑选 + 竖版兜底。
"""
import os
import sys
import unittest
import tempfile

from PIL import Image

from pipeline.report_mixin import ReportMixin


class _DummyLogger:
    def __init__(self):
        self.lines = []

    def log(self, *args, **kwargs):
        self.lines.append(args)


def _make_image(path, size, color=(120, 90, 80)):
    Image.new('RGB', size, color).save(path)


class TestFindOfficialCover(unittest.TestCase):
    def setUp(self):
        self.proc = object.__new__(ReportMixin)
        self.proc.logger = _DummyLogger()
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def _dir(self):
        return self._tmp.name

    def test_numeric_poster_over_2mb_is_found(self):
        """数字名海报不受 2MB 体积上限限制（错嫁 1.png 3.67MB）。"""
        path = os.path.join(self._dir(), '1.png')
        _make_image(path, (1328, 1776))
        self.assertEqual(self.proc._find_official_cover(self._dir(), '错嫁后我怀了三宝'), path)

    def test_old_title_portrait_poster_is_found(self):
        """旧剧名竖版海报（不含关键字/现剧名/数字名）靠竖版兜底命中。"""
        d = self._dir()
        old_title = os.path.join(d, '退婚当天，我夺回铁饭碗 (2).jpg')
        _make_image(old_title, (1440, 2560))
        for i, name in enumerate(['女主.png', '女反.png', '男主.png', '男反.png']):
            _make_image(os.path.join(d, name), (2560, 1440), (60 + i * 20, 80, 100))
        self.assertEqual(self.proc._find_official_cover(d, '重生后我不当后妈'), old_title)

    def test_keyword_beats_portrait_fallback(self):
        """关键字封面优先于竖版大图。"""
        d = self._dir()
        key = os.path.join(d, '视频号封面.jpg')
        _make_image(key, (900, 1200))
        big = os.path.join(d, 'big_pic.png')
        _make_image(big, (1440, 2560))
        self.assertEqual(self.proc._find_official_cover(d, '任意剧名'), key)

    def test_drama_name_match(self):
        """文件名含剧名优先于纯竖版图。"""
        d = self._dir()
        named = os.path.join(d, '南洋归夫难续旧缘-主视觉.jpg')
        _make_image(named, (1000, 1400))
        other = os.path.join(d, 'x.png')
        _make_image(other, (2000, 3000))
        self.assertEqual(self.proc._find_official_cover(d, '南洋归夫难续旧缘'), named)

    def test_only_landscape_stills_returns_none(self):
        """只有横版剧照/立绘时判无官方封面，不硬凑。"""
        d = self._dir()
        for i, name in enumerate(['a.png', 'b.png', 'c.png']):
            _make_image(os.path.join(d, name), (2560, 1440), (40 + i * 30, 60, 80))
        self.assertIsNone(self.proc._find_official_cover(d, '某剧'))

    def test_episode_screenshot_not_chosen(self):
        """剧集截图（带「集」）不会被当成封面。"""
        d = self._dir()
        _make_image(os.path.join(d, '第12集.png'), (1080, 1920))
        poster = os.path.join(d, '海报.jpg')
        _make_image(poster, (1000, 1400))
        self.assertEqual(self.proc._find_official_cover(d, '某剧'), poster)

    def test_empty_dir_returns_none(self):
        self.assertIsNone(self.proc._find_official_cover(self._dir(), '某剧'))

    def test_missing_dir_returns_none(self):
        self.assertIsNone(self.proc._find_official_cover(
            os.path.join(self._dir(), '不存在'), '某剧'))


if __name__ == '__main__':
    unittest.main()
