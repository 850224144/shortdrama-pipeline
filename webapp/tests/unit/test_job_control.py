# -*- coding: utf-8 -*-
"""主处理任务停止、统一取消和资源闸门测试。"""

import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from job_runtime import JobCancelled, execution_gate
from process_job import ProcessJobController
from processor import PipelineProcessor


class BlockingProcess:
    def __init__(self):
        self.started = threading.Event()
        self.event = None

    def process_message(self, message, cancel_event=None):
        self.event = cancel_event
        self.started.set()
        cancel_event.wait(3)
        if cancel_event.is_set():
            raise JobCancelled('用户已停止任务')
        return {'status': 'success', 'cut': 1, 'failed_cuts': 0}


class ProcessJobControllerTests(unittest.TestCase):
    def test_stop_cancels_running_process_and_releases_gate(self):
        processor = BlockingProcess()
        updates = []
        controller = ProcessJobController(processor, on_status=updates.append)

        body, status = controller.start('测试消息')
        self.assertEqual(status, 202)
        self.assertTrue(processor.started.wait(1))
        self.assertTrue(controller.snapshot()['processing'])

        stopped, stop_status = controller.stop()
        self.assertEqual(stop_status, 202)
        self.assertTrue(stopped['stopping'])
        self.assertTrue(processor.event.is_set())
        controller.thread.join(3)

        state = controller.snapshot()
        self.assertFalse(state['processing'])
        self.assertFalse(state['stopping'])
        self.assertEqual(state['message'], '任务已停止')
        self.assertTrue(updates)
        self.assertTrue(execution_gate.try_acquire_pipeline(),
                        '任务结束后必须释放全局流水线闸门')
        execution_gate.release_pipeline()

    def test_second_pipeline_is_rejected_while_first_runs(self):
        first = ProcessJobController(BlockingProcess())
        second = ProcessJobController(BlockingProcess())
        try:
            self.assertEqual(first.start('一')[1], 202)
            result, code = second.start('二')
            self.assertEqual(code, 409)
            self.assertIn('正在运行', result['error'])
        finally:
            first.stop()
            first.thread.join(3)

    def test_stop_without_job_is_truthful_failure(self):
        controller = ProcessJobController(BlockingProcess())
        result, code = controller.stop()
        self.assertEqual(code, 409)
        self.assertEqual(result['status'], 'failed')


class ProcessorCancellationTests(unittest.TestCase):
    def setUp(self):
        self.processor = PipelineProcessor(mock.MagicMock(), mock.MagicMock())

    def test_process_message_honours_preset_cancel(self):
        event = threading.Event()
        event.set()
        result = self.processor.process_message('测试', cancel_event=event)
        self.assertEqual(result['status'], 'cancelled')

    def test_download_retry_returns_cancelled_without_failure_archive(self):
        event = threading.Event()
        event.set()
        link = {'link': 'https://example.invalid/s/1', 'link_id': '测试剧', 'pwd': '1'}
        with mock.patch('archive.upsert_archive') as archive_update:
            result = self.processor._download_with_retry(link, 3, cancel_event=event)
        self.assertTrue(result['cancelled'])
        self.assertFalse(result['success'])
        archive_update.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
