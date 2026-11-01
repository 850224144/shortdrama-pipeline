# -*- coding: utf-8 -*-
"""A4 响度标准化（EBU R128 -14 LUFS）：build_af 链尾 loudnorm + 真实测量。

验收：
- build_af 默认含 loudnorm=I=-14:TP=-1:LRA=11。
- ad['loudness']['enabled']=False → 无 loudnorm。
- 真实 ffmpeg：4s 素材经 apply_anti_dup → ebur128 测综合响度 ≈ -14 LUFS
  （±1.5 LUFS 容差，单遍 dynamic 模式）。
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

sys_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
sys.path.insert(0, sys_path)
sys.path.insert(0, os.path.join(os.path.dirname(sys_path), 'scripts'))  # render_generic.py

import render_generic as rg

FFMPEG = rg.FFMPEG
FFPROBE = rg.FFPROBE


def _make_src(tmp, name='src.mp4', dur=4):
    """生成 1080x1920 含音轨的小素材（testsrc2 画面 + sine 音频）。"""
    src = os.path.join(tmp, name)
    cmd = [
        FFMPEG, '-v', 'error', '-y',
        '-f', 'lavfi', '-i', f'testsrc2=duration={dur}:size=1080x1920:rate=25',
        '-f', 'lavfi', '-i', f'sine=frequency=440:duration={dur}',
        '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k', src,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=120)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-200:])
    return src


def _measure_lufs(path):
    """ebur128 测量综合响度（LUFS）。取 stderr 中最后（收敛）的 I 值。"""
    cmd = [FFMPEG, '-hide_banner', '-i', path,
           '-af', 'ebur128=peak=true', '-f', 'null', '-']
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=120)
    vals = re.findall(r'I:\s*(-?\d+(?:\.\d+)?)\s*LUFS', r.stderr)
    if not vals:
        raise RuntimeError(f'无法解析响度: {r.stderr[-300:]}')
    return float(vals[-1])


class BuildAfTests(unittest.TestCase):
    def setUp(self):
        self.ad = dict(rg.DEFAULT_ANTI_DUP)

    def test_default_contains_loudnorm(self):
        chain = rg.build_af(self.ad)
        self.assertIn('loudnorm', chain)
        self.assertIn('I=-14.0', chain)

    def test_disabled_no_loudnorm(self):
        ad = dict(self.ad)
        ad['loudness'] = {'enabled': False}
        chain = rg.build_af(ad)
        self.assertNotIn('loudnorm', chain)

    def test_custom_target(self):
        ad = dict(self.ad)
        ad['loudness'] = {'enabled': True, 'target_i': -16.0,
                          'target_tp': -2.0, 'lra': 9.0}
        chain = rg.build_af(ad)
        self.assertIn('I=-16.0', chain)
        self.assertIn('TP=-2.0', chain)
        self.assertIn('LRA=9.0', chain)

    def test_missing_loudness_defaults_on(self):
        ad = dict(self.ad)
        ad.pop('loudness', None)
        self.assertIn('loudnorm', rg.build_af(ad))


class LoudnormRealTests(unittest.TestCase):
    """真实 ffmpeg：apply_anti_dup 后响度达标。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='loudnorm_')
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.src = _make_src(self.tmp)

    def test_output_loudness_approaches_target(self):
        out = os.path.join(self.tmp, 'out.mp4')
        ad = dict(rg.DEFAULT_ANTI_DUP)
        rg.apply_anti_dup(self.src, out, ad, crf=28, preset='ultrafast')
        self.assertTrue(os.path.exists(out), '输出应存在')
        lufs = _measure_lufs(out)
        # 单遍 dynamic 模式容差 ±1.5 LUFS
        self.assertAlmostEqual(lufs, -14.0, delta=1.5,
                               msg=f'响度 {lufs:.2f} LUFS 偏离 -14 超过容差')

    def test_disabled_keeps_original_level(self):
        """关闭 loudnorm：输出响度不应被拉向 -14（源 sine 响度 ≈ -21.7）。"""
        out = os.path.join(self.tmp, 'out_off.mp4')
        ad = dict(rg.DEFAULT_ANTI_DUP)
        ad['loudness'] = {'enabled': False}
        rg.apply_anti_dup(self.src, out, ad, crf=28, preset='ultrafast')
        lufs = _measure_lufs(out)
        # 源 sine 满幅 ≈ -21.7 LUFS；关闭后应远离 -14（不归一化）
        self.assertTrue(abs(lufs - (-14.0)) > 2.0,
                        f'关闭 loudnorm 后响度 {lufs:.2f} LUFS 不应被拉向 -14')


if __name__ == '__main__':
    unittest.main()
