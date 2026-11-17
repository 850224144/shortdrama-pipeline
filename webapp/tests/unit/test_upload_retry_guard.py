# -*- coding: utf-8 -*-
"""上传重试守卫单测（2026-11-17 接入 base_queue 重试工具）。

原则：只有「确定没有产生平台副作用」才允许重排队；草稿已保存、已发表、
定时发表未确认（pending_confirm）三种情况一律转人工，否则重传=重复上传。
"""
import tempfile
import unittest

from upload_manager import UploadManager


def _mgr():
    return UploadManager(data_dir=tempfile.mkdtemp(prefix='sd_retry_'))


class TestRetryGuard(unittest.TestCase):
    def test_fresh_failure_is_retryable(self):
        m = _mgr()
        task = {'id': 900, 'retry_count': 0}
        self.assertFalse(m._upload_side_effect_done(task))
        self.assertEqual(m._retry_block_reason(task, False), '')

    def test_retry_count_accumulates_and_stops_at_limit(self):
        m = _mgr()
        task = {'id': 901, 'retry_count': 0}
        self.assertEqual(m._record_retry(task), 1)
        self.assertEqual(m._retry_block_reason(task, False), '')
        self.assertEqual(m._record_retry(task), 2)
        self.assertIn('已达上限', m._retry_block_reason(task, False))

    def test_draft_saved_blocks_retry(self):
        m = _mgr()
        task = {'id': 902, 'retry_count': 0, 'draft_saved': True}
        self.assertIn('副作用', m._retry_block_reason(task, False))

    def test_published_blocks_retry(self):
        m = _mgr()
        task = {'id': 903, 'retry_count': 0, 'published': True}
        self.assertIn('副作用', m._retry_block_reason(task, False))

    def test_pending_confirm_blocks_retry(self):
        # 定时发表未确认：作品可能已在已发布列表 → 禁止自动重传
        m = _mgr()
        task = {'id': 904, 'retry_count': 0, 'pending_confirm': True}
        self.assertIn('副作用', m._retry_block_reason(task, False))

    def test_timeout_blocks_retry(self):
        m = _mgr()
        task = {'id': 905, 'retry_count': 0}
        self.assertIn('超时', m._retry_block_reason(task, True))

    def test_timeout_precedence(self):
        # 超时 + 无副作用：仍不重试（上传可能已进行到一半）
        m = _mgr()
        task = {'id': 906, 'retry_count': 0}
        self.assertTrue(m._retry_block_reason(task, True).startswith('单任务执行超时'))


class TestTimeoutDetection(unittest.TestCase):
    def test_stale_started_at_exceeds_default(self):
        from datetime import datetime, timedelta
        m = _mgr()
        fmt = '%Y-%m-%d %H:%M:%S'
        self.assertFalse(m._timeout_exceeded(None))
        self.assertFalse(m._timeout_exceeded(
            {'started_at': (datetime.now() - timedelta(minutes=1)).strftime(fmt)}))
        self.assertTrue(m._timeout_exceeded(
            {'started_at': (datetime.now() - timedelta(minutes=60)).strftime(fmt)}))

    def test_elapsed_handles_bad_input(self):
        m = _mgr()
        self.assertEqual(m._elapsed_seconds(''), 0)
        self.assertEqual(m._elapsed_seconds('not-a-time'), 0)


if __name__ == '__main__':
    unittest.main()
