# -*- coding: utf-8 -*-
"""批量切片实时阶段进度回归测试。"""

import os
import shutil
import tempfile
import threading
import unittest

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cut_manager import BatchCutManager


class ProgressProcessor:
    def __init__(self):
        self.callback = None
        self.stage_seen = threading.Event()
        self.release = threading.Event()

    def set_cut_progress_callback(self, callback):
        self.callback = callback

    def manual_cut(self, drama_name, **kwargs):
        self.callback('asr', 23, 'ASR 转写完成', {'episode': 12})
        self.stage_seen.set()
        self.release.wait(2)
        self.callback('completed', 100, '完成', {})
        return {'status': 'success', 'output_dirs': [], 'cut': 1}


class CutProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cut_progress_')
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.processor = ProgressProcessor()
        self.manager = BatchCutManager(
            self.processor, self.tmp,
            output_root=os.path.join(self.tmp, 'output'))

    def test_status_exposes_real_stage_and_progress(self):
        self.manager.add_to_queue('测试剧')
        self.assertEqual(self.manager.start_cutting()['status'], 'success')
        self.assertTrue(self.processor.stage_seen.wait(2))
        status = self.manager.get_status()
        self.assertIsNotNone(status['current_task'])
        self.assertEqual(status['current_task']['stage'], 'asr')
        self.assertEqual(status['current_task']['progress'], 23)
        self.assertEqual(status['current_task']['stage_message'], 'ASR 转写完成')
        self.processor.release.set()
        self.manager.cut_thread.join(3)
        self.assertFalse(self.manager.cut_thread.is_alive())
        self.assertIsNone(self.processor.callback)
        final = self.manager.get_status()
        self.assertEqual(final['completed_count'], 1)
        self.assertEqual(final['completed'][0]['progress'], 100)


if __name__ == '__main__':
    unittest.main()
