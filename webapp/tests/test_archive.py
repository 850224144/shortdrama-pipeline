# -*- coding: utf-8 -*-
"""剧级档案（archive.py）单测：建表 / CRUD / 防重下载 / 搜索 / 上传聚合。"""
import os
import sys
import tempfile
import sqlite3
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

    def test_should_download_failed_state_allows_retry_with_existing_dir(self):
        """上次失败留下目录时仍应允许重试，不能被防重误杀。"""
        local = tempfile.mkdtemp()
        archive.upsert_archive('失败重试剧', download_state='failed')
        ok, reason = archive.should_download('失败重试剧', local)
        self.assertTrue(ok)
        self.assertIn('失败', reason)

    def test_should_download_same_source_done_blocks(self):
        archive.upsert_archive('来源剧', download_state='done',
                               source_url='https://pan.baidu.com/s/abc')
        ok, reason = archive.should_download(
            '另一名字', os.path.join(tempfile.mkdtemp(), 'nope'),
            source_url='https://pan.baidu.com/s/abc')
        self.assertFalse(ok)
        self.assertIn('同一来源', reason)

    def test_aggregate_upload_none(self):
        """upload_records 表未建 → none。"""
        state, ok, bad = archive.aggregate_upload_state('剧')
        self.assertEqual(state, 'none')

    def test_aggregate_upload_uses_completed_status(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                'CREATE TABLE upload_records '
                '(id INTEGER PRIMARY KEY, drama_name TEXT, account TEXT, '
                'version INTEGER, status TEXT)')
            conn.execute(
                "INSERT INTO upload_records (drama_name, account, version, status) "
                "VALUES ('完成剧', 'A', 1, 'completed'), "
                "('完成剧', 'A', 2, 'completed')")
        self.assertEqual(archive.aggregate_upload_state('完成剧'),
                         ('done', 2, 0))

    def test_aggregate_upload_latest_retry_supersedes_failure(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                'CREATE TABLE upload_records '
                '(id INTEGER PRIMARY KEY, drama_name TEXT, account TEXT, '
                'version INTEGER, status TEXT)')
            conn.execute(
                "INSERT INTO upload_records (drama_name, account, version, status) "
                "VALUES ('重传剧', 'A', 1, 'failed')")
            conn.execute(
                "INSERT INTO upload_records (drama_name, account, version, status) "
                "VALUES ('重传剧', 'A', 1, 'completed')")
        self.assertEqual(archive.aggregate_upload_state('重传剧'),
                         ('done', 1, 0))

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

    def test_search_upload_filter_applies_before_limit(self):
        """命中的上传状态在 50 条之后时也不能被 LIMIT 提前截掉。"""
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                'CREATE TABLE upload_records '
                '(id INTEGER PRIMARY KEY, drama_name TEXT, account TEXT, '
                'version INTEGER, status TEXT)')
            conn.execute(
                "INSERT INTO upload_records (drama_name, account, version, status) "
                "VALUES ('最老目标剧', 'A', 1, 'completed')")
        archive.upsert_archive('最老目标剧', download_state='done')
        for i in range(55):
            archive.upsert_archive(f'填充剧{i:02d}', download_state='done')
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "UPDATE drama_archive SET updated_at='2020-01-01 00:00:00' "
                "WHERE drama_name='最老目标剧'")
        res = archive.search_archive(upload_state='done', limit=1)
        self.assertEqual([r['drama_name'] for r in res], ['最老目标剧'])

    def test_legacy_archive_migration(self):
        legacy = os.path.join(self.tmp, 'legacy.db')
        target = os.path.join(self.tmp, 'target.db')
        old_path = archive.DB_PATH
        try:
            archive.DB_PATH = legacy
            archive.init_database()
            archive.upsert_archive('迁移剧', download_state='done')
            archive.DB_PATH = target
            archive.init_database()
            moved = archive.migrate_legacy_archive(target, legacy)
            migrated = archive.get_archive('迁移剧')
        finally:
            archive.DB_PATH = old_path
        self.assertEqual(moved, 1)
        self.assertIsNotNone(migrated)


if __name__ == '__main__':
    unittest.main()
