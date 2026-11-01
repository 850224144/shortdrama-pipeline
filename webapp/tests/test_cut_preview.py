# -*- coding: utf-8 -*-
"""只读切片方案预览回归测试。"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cut_preview import build_cut_preview


class CutPreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cut_preview_')
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.output_root = os.path.join(self.tmp, 'output')
        self.source_root = os.path.join(self.tmp, 'source')
        self.out_dir = os.path.join(
            self.output_root, '小桃追剧中', '测试剧')
        self.transcribe = os.path.join(
            self.source_root, '测试剧', '测试剧', 'transcribe')
        os.makedirs(self.out_dir)
        os.makedirs(self.transcribe)
        self._write_config(1, hook='真相')
        with open(os.path.join(self.transcribe, 'highlight_scores.json'),
                  'w', encoding='utf-8') as f:
            json.dump({'3': {'cands': [{
                'score': 9,
                'start': 10.0,
                'end': 25.0,
                'hook_type': '真相',
                'reason': '账本终于被当众揭开',
            }]}}, f, ensure_ascii=False)

    def _write_config(self, version, hook='真相', video_exists=False):
        video_name = f'测试剧_{version}_{hook}_去重版.mp4'
        video_path = os.path.join(self.out_dir, video_name)
        if video_exists:
            with open(video_path, 'wb') as f:
                f.write(b'video')
        config = {
            'drama_name': '测试剧',
            'artifacts': {
                'video_path': video_path,
                'cover_path': os.path.join(self.out_dir, 'covers', 'cover.jpg'),
            },
            'videos': [{
                'id': version,
                'hook_keyword': hook,
                'title': '账本揭开以后',
                'description': '测试文案',
                'hashtags': ['#测试'],
                'segments': [
                    {'episode': 3, 'start': 9.8, 'duration': 15.0},
                    {'episode': 8, 'start': 30.0, 'duration': 20.0},
                ],
            }],
        }
        path = os.path.join(self.out_dir, f'edl_config_v{version}.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        return path

    def test_builds_versions_and_enriches_candidate_reason(self):
        self._write_config(2, hook='打脸', video_exists=True)
        preview = build_cut_preview(
            self.output_root, '测试剧', source_root=self.source_root)
        self.assertEqual(preview['version_count'], 2)
        self.assertEqual(preview['rendered_count'], 1)
        self.assertEqual(preview['planned_count'], 1)
        first = preview['versions'][0]
        self.assertEqual(first['status'], 'planned')
        self.assertEqual(first['segment_count'], 2)
        self.assertEqual(first['planned_duration'], 35.0)
        self.assertEqual(first['segments'][0]['reason'],
                         '账本终于被当众揭开')
        self.assertEqual(first['segments'][0]['candidate_source'], '')
        self.assertEqual(preview['versions'][1]['status'], 'rendered')
        self.assertTrue(preview['versions'][1]['video_exists'])

    def test_missing_drama_returns_none(self):
        self.assertIsNone(build_cut_preview(
            self.output_root, '不存在', source_root=self.source_root))

    def test_rejects_path_traversal(self):
        for name in ('../测试剧', r'..\测试剧', 'C:测试剧', ''):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    build_cut_preview(self.output_root, name)

    def test_preview_is_read_only(self):
        path = os.path.join(self.out_dir, 'edl_config_v1.json')
        before = open(path, 'rb').read()
        build_cut_preview(self.output_root, '测试剧',
                          source_root=self.source_root)
        self.assertEqual(open(path, 'rb').read(), before)


if __name__ == '__main__':
    unittest.main()
