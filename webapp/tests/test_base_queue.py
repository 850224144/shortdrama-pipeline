# -*- coding: utf-8 -*-
"""4.3 队列基类 BaseQueueManager 单测：公共状态、重试上限、超时熔断、归位工具。"""
import unittest
from datetime import datetime, timedelta

from base_queue import BaseQueueManager, DEFAULT_MAX_RETRIES, DEFAULT_TASK_TIMEOUT
from cut_manager import BatchCutManager
from upload_manager import UploadManager


class _Dummy(BaseQueueManager):
    """最小可实例化子类，避免触碰真实 worker / 浏览器。"""
    pass


class TestBaseQueueState(unittest.TestCase):
    def test_common_state_initialized(self):
        b = _Dummy()
        self.assertEqual(b.queue, [])
        self.assertEqual(b.completed, [])
        self.assertEqual(b.failed, [])
        self.assertEqual(b.logs, [])
        self.assertIsNone(b.current_task)
        self.assertFalse(b.paused)
        self.assertFalse(b.stopping)
        self.assertFalse(b.running)
        self.assertEqual(b.max_retries, DEFAULT_MAX_RETRIES)
        self.assertEqual(b.task_timeout, DEFAULT_TASK_TIMEOUT)

    def test_subclasses_are_base_instances(self):
        self.assertTrue(issubclass(BatchCutManager, BaseQueueManager))
        self.assertTrue(issubclass(UploadManager, BaseQueueManager))

    def test_execute_task_hook_not_implemented(self):
        b = _Dummy()
        with self.assertRaises(NotImplementedError):
            b._execute_task({'id': 'x'})


class TestRetryLimit(unittest.TestCase):
    def test_retry_count_accumulates(self):
        b = _Dummy()
        task = {'id': 't1'}
        self.assertEqual(b._record_retry(task), 1)
        self.assertEqual(b._record_retry(task), 2)
        self.assertEqual(task['retry_count'], 2)

    def test_should_retry_within_limit(self):
        b = _Dummy()
        task = {'id': 't1'}
        self.assertTrue(b._should_retry(task))          # 0 < 2
        b._record_retry(task)
        self.assertTrue(b._should_retry(task))          # 1 < 2
        b._record_retry(task)
        self.assertFalse(b._should_retry(task))        # 2 == 2 达上限

    def test_custom_max_retries(self):
        b = _Dummy()
        b.max_retries = 1
        task = {'id': 't1'}
        self.assertTrue(b._should_retry(task))
        b._record_retry(task)
        self.assertFalse(b._should_retry(task))


class TestTimeoutBreaker(unittest.TestCase):
    def test_elapsed_seconds_no_start_or_bad_format(self):
        b = _Dummy()
        self.assertEqual(b._elapsed_seconds(None), 0)
        self.assertEqual(b._elapsed_seconds(''), 0)
        self.assertEqual(b._elapsed_seconds('not-a-date'), 0)

    def test_timeout_not_exceeded_for_fresh_task(self):
        b = _Dummy()
        task = {'id': 't1', 'started_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        self.assertFalse(b._timeout_exceeded(task))
        self.assertFalse(b._timeout_exceeded(None))

    def test_timeout_exceeded_for_stale_task(self):
        b = _Dummy()
        stale = (datetime.now() - timedelta(seconds=DEFAULT_TASK_TIMEOUT + 60)) \
            .strftime('%Y-%m-%d %H:%M:%S')
        task = {'id': 't1', 'started_at': stale}
        self.assertTrue(b._timeout_exceeded(task))

    def test_custom_timeout_threshold(self):
        b = _Dummy()
        b.task_timeout = 10
        stale = (datetime.now() - timedelta(seconds=20)).strftime('%Y-%m-%d %H:%M:%S')
        self.assertTrue(b._timeout_exceeded({'started_at': stale}))


class TestTaskSettlement(unittest.TestCase):
    def test_mark_completed(self):
        b = _Dummy()
        task = {'id': 't1', 'status': 'pending'}
        b._mark_completed(task)
        self.assertEqual(b.completed, ['t1'])
        self.assertEqual(task['status'], 'completed')
        self.assertIn('completed_at', task)

    def test_mark_failed(self):
        b = _Dummy()
        task = {'id': 't2', 'status': 'pending'}
        b._mark_failed(task, '崩溃原因')
        self.assertEqual(b.failed, ['t2'])
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['error'], '崩溃原因')
        self.assertIn('failed_at', task)


if __name__ == '__main__':
    unittest.main()
