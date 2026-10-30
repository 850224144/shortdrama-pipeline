# -*- coding: utf-8 -*-
"""剧级档案（archive.py）单测：建表 / CRUD / 防重下载 / 搜索 / 上传聚合。"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import archive


class TestArchive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, 'test.db')
        self.patcher = mock.patch.object(archive, 'DB_PATH', self.db)
        self.patcher.start()
        archive.init_database()

    def tearDown(self):
        self.patcher.stop()

    def test_upsert_and_get(self):
        archive.upsert_archive('测试剧', source_url='u', source_passwd='9999',
                               download_state='downloading')
        d = archive.get_archive('测试剧')
        self.assertEqual(d['download_state'], 'downloading')
        self.assertEqual(d['source_url'], 'u')
        self.assertEqual(d['source_passwd'], '9999')
        # 再次 upsert 更新（UNIQUE drama_name 冲突走 DO UPDATE）
        archive.upsert_archive('测试剧', download_state='done', episode_count=36)
        d = archive.get_archive('测试剧')
        self.assertEqual(d['download_state'], 'done')
        self.assertEqual(d['episode_count'], 36)
        # 下载状态更新后应保留 source_url（未传的列不动）
        self.assertEqual(d['source_url'], 'u')

    def test_should_download_local_dir_exists(self):
        """本地目录已存在 → 不下载（防重①）。"""
        d = tempfile.mkdtemp()
        ok, reason = archive.should_download('某剧', d)
        self.assertFalse(ok)
        self.assertIn('已存在', reason)

    def test_should_download_done_history(self):
        """历史 download_state=done → 不下载（防重②）。"""
        archive.upsert_archive('已下载剧', download_state='done')
        ok, reason = archive.should_download('已下载剧', os.path.join(tempfile.mkdtemp(), 'nope'))
        self.assertFalse(ok)
        self.assertIn('已下载成功', reason)

    def test_should_download_downloading_history_allowed(self):
        """历史 downloading（未完成）不阻断重下（可重试）。"""
        archive.upsert_archive('进行中剧', download_state='downloading')
        ok, reason = archive.should_download('进行中剧', os.path.join(tempfile.mkdtemp(), 'nope'))
        self.assertTrue(ok)

    def test_should_download_ok(self):
        """本地无目录且无历史 → 可下载。"""
        ok, reason = archive.should_download('新剧', os.path.join(tempfile.mkdtemp(), 'nope'))
        self.assertTrue(ok)

    def test_aggregate_upload_none(self):
        """upload_records 表未建 → none。"""
        state, ok, bad = archive.aggregate_upload_state('剧')
        self.assertEqual(state, 'none')

    def test_search_and_filter(self):
        archive.upsert_archive('测试剧A', download_state='done', slice_state='done')
        archive.upsert_archive('测试剧B', download_state='downloading')
        archive.upsert_archive('另一剧', download_state='done')
        res = archive.search_archive(q='测试')
        self.assertEqual(len(res), 2)
        res2 = archive.search_archive(q='测试', download_state='done')
        self.assertEqual(len(res2), 1)
        self.assertEqual(res2[0]['drama_name'], '测试剧A')
        res3 = archive.search_archive(q='测试', slice_state='done')
        self.assertEqual(len(res3), 1)

    def test_search_upload_state_filter(self):
        """upload_state 筛选（查询时聚合）。无 upload_records → none，筛选 none 命中。"""
        archive.upsert_archive('未上传剧', download_state='done')
        res = archive.search_archive(q='未上传', upload_state='none')
        self.assertEqual(len(res), 1)


if __name__ == '__main__':
    unittest.main()
