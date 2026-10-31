# -*- coding: utf-8 -*-
"""R8 端到端冒烟：小素材经 render_generic 完整跑通 + R6 质量门禁核验。

真实小素材（ffmpeg 生成的 2 集 1080x1920 短片）→ 手动 EDL 配置 → render_generic
渲染 → 1080x1920 成片 → delivery_quality.validate_media 确认可交付。

默认跳过（需 RELIABILITY_SMOKE=1 且本机 ffmpeg/ffprobe 可用），避免拖慢常规回归。
运行方式：
    RELIABILITY_SMOKE=1 python -m unittest tests.test_render_smoke -v
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from delivery_quality import validate_media

FFMPEG = r'D:\ffmpeg-master-latest-win64-gpl-shared\bin\ffmpeg.exe'
FFPROBE = r'D:\ffmpeg-master-latest-win64-gpl-shared\bin\ffprobe.exe'
WEBAPP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RENDER = os.path.join(WEBAPP, '..', 'scripts', 'render_generic.py')


def _ffmpeg_available():
    return os.path.isfile(FFMPEG) and os.path.isfile(FFPROBE)


@unittest.skipUnless(os.environ.get('RELIABILITY_SMOKE') == '1' and _ffmpeg_available(),
                     '需要 RELIABILITY_SMOKE=1 且本机 ffmpeg/ffprobe 可用')
class RenderSmokeTests(unittest.TestCase):
    def test_small_material_full_render_passes_gate(self):
        with tempfile.TemporaryDirectory(prefix='render_smoke_') as d:
            src_dir = os.path.join(d, '素材')
            out_dir = os.path.join(d, 'output')
            os.makedirs(src_dir)
            os.makedirs(out_dir)
            drama = '冒烟测试剧'

            # 2 集真实小素材（各 34s，1080x1920，含音轨）
            for ep in (1, 2):
                r = subprocess.run(
                    [FFMPEG, '-y',
                     '-f', 'lavfi', '-i', 'testsrc=size=1080x1920:rate=25:duration=34',
                     '-f', 'lavfi', '-i', 'sine=frequency=440:duration=34',
                     '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '30',
                     '-c:a', 'aac', '-b:a', '128k', '-shortest',
                     os.path.join(src_dir, f'{drama} - 第{ep}集.mp4')],
                    capture_output=True)
                self.assertEqual(r.returncode, 0, '生成素材失败')

            # 手动 EDL 配置：两段各 32s → 拼接 64s → 抗查重 1.05x ≈ 61s（≥60s 可交付）
            cfg = {
                'drama_name': drama,
                'source_dir': src_dir,
                'source_pattern': '{name} - 第{ep}集.mp4',
                'output_dir': out_dir,
                'artifacts': {'video_path': os.path.join(out_dir, f'{drama}_1_测试_去重版.mp4')},
                'videos': [{
                    'id': 1, 'hook_keyword': '测试', 'title': 't',
                    'description': 'd', 'hashtags': ['测试'],
                    'segments': [
                        {'episode': 1, 'start': 0.2, 'duration': 32.0},
                        {'episode': 2, 'start': 0.3, 'duration': 32.0},
                    ],
                }],
            }
            cfg_path = os.path.join(d, 'edl_config_v1.json')
            with open(cfg_path, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False)

            # 完整渲染
            r = subprocess.run([sys.executable, RENDER, cfg_path],
                               capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=300)
            self.assertEqual(r.returncode, 0, r.stdout[-500:] + r.stderr[-500:])

            deliverable = os.path.join(out_dir, f'{drama}_1_测试_去重版.mp4')
            self.assertTrue(os.path.isfile(deliverable), '成片未生成')

            # R6 质量门禁核验真实成片
            q = validate_media(deliverable, ffprobe_path=FFPROBE,
                               min_duration=60, max_duration=300)
            self.assertTrue(q['ok'], f'成片未通过质量门禁: {q}')
            self.assertEqual(q['size'], os.path.getsize(deliverable))
            self.assertGreaterEqual(q['duration'], 60)

            # 分辨率核验（1080x1920）
            probe = subprocess.run(
                [FFPROBE, '-v', 'error', '-select_streams', 'v:0',
                 '-show_entries', 'stream=width,height', '-of', 'csv=p=0',
                 deliverable],
                capture_output=True, text=True)
            self.assertEqual(probe.stdout.strip(), '1080,1920')


if __name__ == '__main__':
    unittest.main()
