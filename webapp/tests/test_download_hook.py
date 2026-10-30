# -*- coding: utf-8 -*-
"""下载钩子单测：archive 防重跳过 + 成功回填 + 失败标记。

验证 _download_once 入口防重（本地目录已存在 或 历史 done → 跳过）
与 _download_with_retry 统一回填（成功 done / 失败 failed）。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import archive
from processor import PipelineProcessor, CONFIG


class TestDownloadHook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_db = archive.DB_PATH
        self._old_base = CONFIG['download_base_dir']
        archive.DB_PATH = os.path.join(self.tmp, 'test_archive.db')
        CONFIG['download_base_dir'] = self.tmp
        archive.init_database()

    def tearDown(self):
        archive.DB_PATH = self._old_db
        CONFIG['download_base_dir'] = self._old_base

    def _mk(self):
        return PipelineProcessor(mock.MagicMock(), mock.MagicMock())

    def _li(self, drama='测试剧'):
        return {'link': 'https://pan.baidu.com/s/abc123?pwd=9999', 'link_id': drama, 'pwd': '9999'}

    def test_skip_local_existing(self):
        """本地目录已存在 → 防重跳过（不下载）。"""
        p = self._mk()
        os.makedirs(os.path.join(self.tmp, '测试剧'), exist_ok=True)
        r = p._download_once(self._li())
        self.assertTrue(r['success'])
        self.assertTrue((r.get('error') or '').startswith('防重'))
        self.assertEqual(r['drama_name'], '测试剧')

    def test_skip_history_done(self):
        """历史 download_state=done → 防重跳过。"""
        p = self._mk()
        archive.upsert_archive('测试剧', download_state='done')
        r = p._download_once(self._li())
        self.assertTrue(r['success'])
        self.assertTrue((r.get('error') or '').startswith('防重'))

    def test_success_backfill(self):
        """下载成功 → archive 回填 done + episode_count。"""
        p = self._mk()
        for f in ('a.mp4', 'b.mp4'):
            with open(os.path.join(self.tmp, f), 'wb') as fh:
                fh.write(b'x' * 100)

        def fake_once(li):
            return {'success': True,
                    'downloaded_files': [os.path.join(self.tmp, 'a.mp4'), os.path.join(self.tmp, 'b.mp4')],
                    'drama_name': '测试剧', 'error': None, 'transferred': False}
        p._download_once = fake_once
        r = p._download_with_retry(self._li(), 1)
        self.assertTrue(r['success'])
        a = archive.get_archive('测试剧')
        self.assertIsNotNone(a)
        self.assertEqual(a['download_state'], 'done')
        self.assertEqual(a['episode_count'], 2)

    def test_fail_mark(self):
        """下载最终失败 → archive 标记 failed。"""
        p = self._mk()

        def fake_once(li):
            return {'success': False, 'downloaded_files': [], 'drama_name': '测试剧',
                    'error': '模拟失败', 'transferred': False}
        p._download_once = fake_once
        r = p._download_with_retry(self._li(), 1)
        self.assertFalse(r['success'])
        a = archive.get_archive('测试剧')
        self.assertIsNotNone(a)
        self.assertEqual(a['download_state'], 'failed')


if __name__ == '__main__':
    unittest.main()
