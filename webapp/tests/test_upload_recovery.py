# -*- coding: utf-8 -*-
"""上传队列跨服务重启恢复。"""
import os
import shutil
import sqlite3
import tempfile
import unittest

from upload_manager import UploadManager


class UploadRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='upload_restore_')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_uploading_record_is_restored_as_pending(self):
        first = UploadManager(self.tmp)
        res = first.add_to_queue(
            drama_name='恢复剧', video_path=os.path.join(self.tmp, 'v1.mp4'),
            account='测试号', version=1, batch_id='B_RESTORE',
            expected_versions=[1, 2])
        self.assertEqual(res['status'], 'success')
        with sqlite3.connect(first.db_path) as conn:
            conn.execute(
                "UPDATE upload_records SET status='uploading', started_at=CURRENT_TIMESTAMP "
                "WHERE id=?", (res['id'],))
            conn.commit()

        second = UploadManager(self.tmp)
        self.assertEqual(len(second.queue), 1)
        task = second.queue[0]
        self.assertEqual(task['drama_name'], '恢复剧')
        self.assertEqual(task['status'], 'pending')
        self.assertTrue(task['restored_after_restart'])
        with sqlite3.connect(second.db_path) as conn:
            status, started = conn.execute(
                "SELECT status, started_at FROM upload_records WHERE id=?",
                (res['id'],)).fetchone()
        self.assertEqual(status, 'pending')
        self.assertIsNone(started)


if __name__ == '__main__':
    unittest.main()
