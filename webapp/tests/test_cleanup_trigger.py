# -*- coding: utf-8 -*-
"""#4/#5 回归：自动清理触发机制。

- 批次自然结束（worker）才标记完成并触发后端清理（#4）。
- 人工停止（stop_upload 置 _stopped）不得进入自然结束触发（#5）。
- 批次仍含 pending/uploading 记录时不标记完成（避免搁置误标）。
- 未启用 auto_cleanup 时即使批次完成也不触发清理。
"""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

from upload_manager import UploadManager


class CleanupTriggerTests(unittest.TestCase):
    def make_manager(self):
        self.tmp = tempfile.mkdtemp(prefix='cleanup_trig_')
        return UploadManager(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed_batch(self, m, batch_id='B1', n_records=3, statuses=('completed',)):
        with sqlite3.connect(m.db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO upload_batches (batch_id, drama_name, status) "
                "VALUES (?, '测试剧', 'running')", (batch_id,))
            for i in range(n_records):
                conn.execute(
                    "INSERT INTO upload_records (drama_name, version, video_path, account, status, batch_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ('测试剧', i + 1, f'C:/v{i}.mp4', 'acc', statuses[i] if i < len(statuses) else 'completed', batch_id))
            conn.commit()

    def test_natural_end_marks_batch_complete_and_triggers_cleanup(self):
        m = self.make_manager()
        self._seed_batch(m, 'B1', 2, ('completed', 'completed'))
        m._run_auto_cleanup = True
        cb = Mock()
        m.set_cleanup_callback(cb)

        m._finalize_batches(['B1'])

        with sqlite3.connect(m.db_path) as conn:
            status = conn.execute("SELECT status FROM upload_batches WHERE batch_id='B1'").fetchone()[0]
            ac = conn.execute("SELECT auto_cleanup FROM upload_batches WHERE batch_id='B1'").fetchone()[0]
        self.assertEqual(status, 'completed')
        self.assertEqual(ac, 1)
        cb.assert_called_once_with('B1')

    def test_no_cleanup_when_auto_cleanup_off(self):
        m = self.make_manager()
        self._seed_batch(m, 'B2', 1, ('completed',))
        m._run_auto_cleanup = False
        cb = Mock()
        m.set_cleanup_callback(cb)

        m._finalize_batches(['B2'])

        cb.assert_not_called()
        with sqlite3.connect(m.db_path) as conn:
            status = conn.execute("SELECT status FROM upload_batches WHERE batch_id='B2'").fetchone()[0]
        self.assertEqual(status, 'completed')

    def test_pending_records_block_complete(self):
        m = self.make_manager()
        self._seed_batch(m, 'B3', 2, ('completed', 'pending'))
        m._run_auto_cleanup = True
        cb = Mock()
        m.set_cleanup_callback(cb)

        m._finalize_batches(['B3'])

        cb.assert_not_called()
        with sqlite3.connect(m.db_path) as conn:
            status = conn.execute("SELECT status FROM upload_batches WHERE batch_id='B3'").fetchone()[0]
        # 仍有 pending，不得标记完成
        self.assertEqual(status, 'running')

    def test_stop_upload_sets_stopped_flag(self):
        """#5：人工停止必须置 _stopped，使 worker 自然结束路径跳过触发。"""
        m = self.make_manager()
        with sqlite3.connect(m.db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO upload_batches (batch_id, drama_name, status) "
                "VALUES ('B4', '测试剧', 'running')")
            conn.commit()
        # 模拟 worker 已启动
        m.uploading = True
        m.queue.append({'batch_id': 'B4', 'drama_name': '测试剧', 'version': 1})
        res = m.stop_upload()
        self.assertEqual(res['status'], 'success')
        self.assertTrue(m._stopped)
        self.assertFalse(m.uploading)


if __name__ == '__main__':
    unittest.main()
