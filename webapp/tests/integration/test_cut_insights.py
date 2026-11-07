# -*- coding: utf-8 -*-
"""切片版本差异与内容风险审计回归测试。"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from content_fingerprint import FingerprintStore
from cut_insights import build_cut_insights
from upload_manager import UploadManager


class CutInsightsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cut_insights_')
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.output_root = os.path.join(self.tmp, 'output')
        self.source_root = os.path.join(self.tmp, 'source')
        self.out_dir = os.path.join(
            self.output_root, '小桃追剧中', '测试剧')
        self.source_dir = os.path.join(self.source_root, '测试剧')
        self.transcribe = os.path.join(self.source_dir, 'transcribe')
        self.data_dir = os.path.join(self.tmp, 'data')
        os.makedirs(self.out_dir)
        os.makedirs(self.transcribe)
        os.makedirs(self.data_dir)
        self.db_path = os.path.join(self.data_dir, 'upload.db')
        UploadManager(self.data_dir)
        self._write_transcripts()

    def _write_transcripts(self):
        rows1 = [
            {'start': 0.0, 'end': 20.0, 'text': '她当众揭开账本真相'},
            {'start': 20.0, 'end': 40.0, 'text': '所有人都沉默了'},
        ]
        rows2 = [
            {'start': 0.0, 'end': 20.0, 'text': '父亲终于承认错误'},
            {'start': 20.0, 'end': 40.0, 'text': '一家人重新开始'},
        ]
        for ep, rows in ((1, rows1), (2, rows2)):
            with open(os.path.join(self.transcribe, f'EP{ep}.json'),
                      'w', encoding='utf-8') as f:
                json.dump(rows, f, ensure_ascii=False)

    def _write_version(self, version, segments):
        name = f'测试剧_{version}_真相_去重版.mp4'
        video_path = os.path.join(self.out_dir, name)
        with open(video_path, 'wb') as f:
            f.write(b'video' * 10)
        config = {
            'drama_name': '测试剧',
            'source_dir': self.source_dir,
            'source_pattern': '第{ep}集.mp4',
            'artifacts': {'video_path': video_path},
            'videos': [{
                'id': version,
                'hook_keyword': '真相',
                'title': f'版本{version}',
                'segments': segments,
            }],
        }
        with open(os.path.join(self.out_dir, f'edl_config_v{version}.json'),
                  'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False)
        return video_path

    def test_builds_version_matrix_and_risk(self):
        v1 = self._write_version(1, [
            {'episode': 1, 'start': 0.0, 'duration': 20.0},
            {'episode': 2, 'start': 0.0, 'duration': 20.0},
        ])
        v2 = self._write_version(2, [
            {'episode': 1, 'start': 0.0, 'duration': 20.0},
            {'episode': 1, 'start': 20.0, 'duration': 20.0},
            {'episode': 2, 'start': 20.0, 'duration': 20.0},
        ])
        store = FingerprintStore(self.db_path)
        store.upsert('测试剧', v1, [1, 2, 3], version=1,
                     account='小桃追剧中')
        store.upsert('测试剧', v2, [1, 2, 4], version=2,
                     account='小桃追剧中')

        data = build_cut_insights(
            self.output_root, '测试剧', source_root=self.source_root,
            db_path=self.db_path)
        self.assertIsNotNone(data)
        self.assertEqual(data['version_count'], 2)
        self.assertEqual(data['summary']['segments_total'], 5)
        self.assertEqual(data['summary']['segments_unique'], 4)
        self.assertEqual(data['summary']['exact_reuse_segments'], 1)
        self.assertEqual(data['summary']['visual_pairs_available'], 1)
        self.assertEqual(data['content_risk']['level'], 'high')
        self.assertFalse(data['content_risk']['bulk_publish_allowed'])
        self.assertFalse(data['platform_comparison']['available'])
        self.assertTrue(data['versions'][0]['dialogue_excerpt'])
        self.assertEqual(data['versions'][1]['exact_reuse_count'], 1)
        self.assertTrue(any('源剧' in r for r in
                            data['versions'][0]['risk']['reasons']))

    def test_reads_cross_account_local_upload_records(self):
        self._write_version(1, [
            {'episode': 1, 'start': 0.0, 'duration': 20.0},
        ])
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                '''INSERT INTO upload_records
                   (drama_name, version, video_path, account, status)
                   VALUES (?, ?, ?, ?, ?)''',
                ('测试剧', 1, 'old.mp4', '福桃说剧', 'completed'))
        data = build_cut_insights(
            self.output_root, '测试剧', source_root=self.source_root,
            db_path=self.db_path)
        self.assertEqual(len(data['local_upload_records']), 1)
        self.assertEqual(data['local_upload_records'][0]['account'], '福桃说剧')

    def test_missing_drama_returns_none(self):
        self.assertIsNone(build_cut_insights(
            self.output_root, '不存在', source_root=self.source_root,
            db_path=self.db_path))

    def test_rejects_path_traversal(self):
        with self.assertRaises(ValueError):
            build_cut_insights(self.output_root, '../测试剧',
                               source_root=self.source_root,
                               db_path=self.db_path)


if __name__ == '__main__':
    unittest.main()
