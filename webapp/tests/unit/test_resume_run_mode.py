# -*- coding: utf-8 -*-
"""resume 运行模式（定时发表 / 自动清理）持久化单测（review L3）。

背景：``_scheduled_publish`` / ``_run_auto_cleanup`` 是 UploadManager 实例属性，
而 upload_manager 是模块级单例；``resume_upload`` 入口不带模式参数，恢复 worker
若只看内存属性，就会沿用上一批次（甚至上一进程残留）的模式——同一进程里先跑
「定时发表」批次、停止后恢复「保存草稿」批次，恢复的那批会按定时发表点发表。

修复：start_upload 把模式落到 upload_batches，resume 从批次行恢复；批次行缺失
时保守回退「保存草稿 + 不自动清理」。
"""
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

import upload_manager as upload_module
from upload_manager import UploadManager


class TestRunModePersistRestore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='sd_runmode_')
        self.m = UploadManager(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed_batch(self, batch_id, scheduled=0, auto_cleanup=0):
        with sqlite3.connect(self.m.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO upload_batches "
                "(batch_id, drama_name, status, scheduled, auto_cleanup) "
                "VALUES (?, '测试剧', 'running', ?, ?)",
                (batch_id, scheduled, auto_cleanup))
            conn.commit()

    def _mode_row(self, batch_id):
        with sqlite3.connect(self.m.db_path) as conn:
            return conn.execute(
                "SELECT scheduled, auto_cleanup FROM upload_batches WHERE batch_id=?",
                (batch_id,)).fetchone()

    def test_scheduled_column_exists_after_init(self):
        cols = {r[1] for r in sqlite3.connect(self.m.db_path).execute(
            'PRAGMA table_info(upload_batches)').fetchall()}
        self.assertIn('scheduled', cols)

    def test_persist_run_mode_writes_batch_row(self):
        self._seed_batch('B1')
        self.m.queue = [{'batch_id': 'B1'}, {'batch_id': 'B1'}]
        self.m._scheduled_publish = True
        self.m._run_auto_cleanup = True
        self.assertEqual(self.m._persist_run_mode(), 1)
        self.assertEqual(self._mode_row('B1'), (1, 1))

    def test_persist_without_batch_id_is_noop(self):
        self.m.queue = [{'batch_id': None}]
        self.m._scheduled_publish = True
        self.assertEqual(self.m._persist_run_mode(), 0)

    def test_restore_reads_mode_from_batch(self):
        self._seed_batch('B2', scheduled=1, auto_cleanup=1)
        self.m.queue = [{'batch_id': 'B2'}]
        # 内存里是上一批次的相反模式（串号现场）
        self.m._scheduled_publish = False
        self.m._run_auto_cleanup = False
        self.m._restore_run_mode()
        self.assertTrue(self.m._scheduled_publish)
        self.assertTrue(self.m._run_auto_cleanup)

    def test_restore_missing_batch_falls_back_to_draft(self):
        self.m.queue = [{'batch_id': 'NOT_EXIST'}]
        self.m._scheduled_publish = True   # 上一批次残留
        self.m._run_auto_cleanup = True
        self.m._restore_run_mode()
        self.assertFalse(self.m._scheduled_publish)
        self.assertFalse(self.m._run_auto_cleanup)

    def test_resume_upload_restores_mode_before_worker(self):
        """resume_upload 必须在启动 worker 前从批次行恢复模式。"""
        self._seed_batch('B3', scheduled=1, auto_cleanup=1)
        self.m.queue = [{'batch_id': 'B3', 'account': 'accA', 'status': 'pending'}]
        self.m.uploading = False
        self.m.prechecking = False
        self.m.upload_thread = None
        self.m._scheduled_publish = False   # 上一批次残留相反模式
        self.m._run_auto_cleanup = False
        with mock.patch.object(upload_module, 'WeChatChannelsUploader') as Uploader, \
                mock.patch.object(upload_module.threading, 'Thread') as Thread:
            Uploader.return_value.check_login.return_value = True
            result = self.m.resume_upload()
        self.assertEqual(result.get('status'), 'success')
        self.assertTrue(self.m._scheduled_publish)
        self.assertTrue(self.m._run_auto_cleanup)
        self.assertEqual(Thread.call_count, 1)

    def test_resume_requires_login_before_restore(self):
        """登录未通过则不得恢复/开跑（登录门控先于模式恢复）。"""
        self._seed_batch('B4', scheduled=1)
        self.m.queue = [{'batch_id': 'B4', 'account': 'accA', 'status': 'pending'}]
        self.m.uploading = False
        self.m.prechecking = False
        self.m.upload_thread = None
        self.m._scheduled_publish = False
        with mock.patch.object(upload_module, 'WeChatChannelsUploader') as Uploader, \
                mock.patch.object(upload_module.threading, 'Thread') as Thread:
            Uploader.return_value.check_login.return_value = False
            result = self.m.resume_upload()
        self.assertEqual(result.get('status'), 'failed')
        self.assertTrue(result.get('login_required'))
        self.assertEqual(Thread.call_count, 0)
        self.assertFalse(self.m._scheduled_publish)


if __name__ == '__main__':
    unittest.main()
