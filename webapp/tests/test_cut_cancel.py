# -*- coding: utf-8 -*-
"""切片停止链路：取消信号、进程清理与队列状态回归测试。"""

import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cut_manager import BatchCutManager
from processor import CutCancelled, PipelineProcessor


class BlockingProcessor:
    def __init__(self, result_status='cancelled'):
        self.started = threading.Event()
        self.cancel_event = None
        self.result_status = result_status

    def manual_cut(self, drama_name, resume=False, cancel_event=None):
        self.cancel_event = cancel_event
        self.started.set()
        cancel_event.wait(3)
        if self.result_status == 'skipped':
            return {'status': 'skipped', 'output_dirs': ['existing']}
        return {'status': 'cancelled', 'cancelled': True, 'error': '用户已停止切片'}


class CutManagerCancelTests(unittest.TestCase):
    def test_stop_signals_worker_and_requeues_task(self):
        processor = BlockingProcessor()
        manager = BatchCutManager(processor)
        self.assertEqual(manager.add_to_queue('测试剧')['status'], 'success')
        self.assertEqual(manager.start_cutting()['status'], 'success')
        self.assertTrue(processor.started.wait(1), '切片任务应已启动')

        stopped = manager.stop_cutting()
        self.assertEqual(stopped['status'], 'success')
        manager.cut_thread.join(3)
        self.assertFalse(manager.cut_thread.is_alive(), '取消后 worker 必须退出')
        self.assertTrue(processor.cancel_event.is_set())

        status = manager.get_status()
        self.assertFalse(status['cutting'])
        self.assertFalse(status['stopping'])
        self.assertEqual(status['failed_count'], 0, '用户停止不能计入失败')
        self.assertEqual(status['queue_length'], 1)
        self.assertEqual(status['queue'][0]['status'], 'pending')
        self.assertIn('已停止', status['queue'][0]['error'])

    def test_already_cut_is_completed_not_failed(self):
        processor = BlockingProcessor(result_status='skipped')
        processor.manual_cut = lambda *args, **kwargs: {
            'status': 'skipped', 'output_dirs': ['existing']}
        manager = BatchCutManager(processor)
        manager.add_to_queue('已切片剧')
        manager.start_cutting()
        manager.cut_thread.join(2)

        status = manager.get_status()
        self.assertEqual(status['completed_count'], 1)
        self.assertEqual(status['failed_count'], 0)
        self.assertTrue(status['completed'][0]['skipped'])


class ProcessorCancelTests(unittest.TestCase):
    def setUp(self):
        self.processor = PipelineProcessor(mock.MagicMock(), mock.MagicMock())

    def test_cancellable_command_kills_long_running_child(self):
        cancel_event = threading.Event()
        timer = threading.Timer(0.2, cancel_event.set)
        timer.start()
        started_at = time.monotonic()
        with self.assertRaises(CutCancelled):
            self.processor._run_command_cancellable(
                [sys.executable, '-c', 'import time; time.sleep(30)'],
                timeout=20,
                cancel_event=cancel_event,
            )
        timer.cancel()
        self.assertLess(time.monotonic() - started_at, 5)

    def test_cut_batch_does_not_retry_after_cancel(self):
        event = threading.Event()
        self.processor._cut_once = mock.Mock(return_value={
            'name': '测试剧', 'success': False, 'cancelled': True,
            'output_dir': None, 'error': '用户已停止切片',
        })
        with mock.patch('archive.init_database'), mock.patch('archive.upsert_archive'):
            results = self.processor.cut_batch(
                [{'drama_name': '测试剧', 'link_id': '测试剧'}],
                cancel_event=event,
            )
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]['cancelled'])
        self.processor._cut_once.assert_called_once()
        self.assertIs(self.processor._cut_once.call_args.kwargs['cancel_event'], event)

    def test_cut_once_with_preset_cancel_has_no_side_effect(self):
        event = threading.Event()
        event.set()
        result = self.processor._cut_once(
            {'drama_name': '测试剧', 'download_files': []},
            cancel_event=event,
        )
        self.assertTrue(result['cancelled'])
        self.assertFalse(result['success'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
