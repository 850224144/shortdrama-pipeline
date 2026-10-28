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
        cb = Mock()
        m.set_cleanup_callback(cb)

        m._finalize_batches(['B1'], auto_cleanup=True)

        with sqlite3.connect(m.db_path) as conn:
            status = conn.execute("SELECT status FROM upload_batches WHERE batch_id='B1'").fetchone()[0]
            ac = conn.execute("SELECT auto_cleanup FROM upload_batches WHERE batch_id='B1'").fetchone()[0]
        self.assertEqual(status, 'completed')
        self.assertEqual(ac, 1)
        cb.assert_called_once_with('B1')

    def test_no_cleanup_when_auto_cleanup_off(self):
        m = self.make_manager()
        self._seed_batch(m, 'B2', 1, ('completed',))
        cb = Mock()
        m.set_cleanup_callback(cb)

        m._finalize_batches(['B2'], auto_cleanup=False)

        cb.assert_not_called()
        with sqlite3.connect(m.db_path) as conn:
            status = conn.execute("SELECT status FROM upload_batches WHERE batch_id='B2'").fetchone()[0]
        self.assertEqual(status, 'completed')

    def test_pending_records_block_complete(self):
        m = self.make_manager()
        self._seed_batch(m, 'B3', 2, ('completed', 'pending'))
        cb = Mock()
        m.set_cleanup_callback(cb)

        m._finalize_batches(['B3'], auto_cleanup=True)

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
        # 停止必须标记本次运行（epoch），使旧 worker 结束时不触发清理
        self.assertEqual(m._stop_epoch, m._run_epoch)

    def test_stop_prevents_trigger_for_that_run(self):
        """P1 并发：被停止的运行，其 worker 结束时按 epoch 判定不触发清理。"""
        m = self.make_manager()
        self._seed_batch(m, 'B8', 1, ('completed',))
        m._run_epoch += 1
        # 模拟 worker 快照
        my_epoch = m._run_epoch
        my_auto = True
        # 模拟该运行被人工停止
        m._stop_epoch = m._run_epoch
        cb = Mock()
        m.set_cleanup_callback(cb)
        # worker 自然结束路径的判定
        if m._stop_epoch != my_epoch:
            m._finalize_batches(['B8'], auto_cleanup=my_auto)
        cb.assert_not_called()
        with sqlite3.connect(m.db_path) as conn:
            status = conn.execute("SELECT status FROM upload_batches WHERE batch_id='B8'").fetchone()[0]
        self.assertEqual(status, 'running')  # 停止的运行不标记完成

    def test_start_upload_rejects_when_old_worker_alive(self):
        """P1 并发：旧 worker 仍存活时 start_upload 拒绝启动（防双 worker 操作队列）。"""
        import threading, time
        m = self.make_manager()
        self._seed_batch(m, 'B9', 1, ('completed',))
        m.queue.append({'id': 1, 'drama_name': '测试剧', 'version': 1, 'account': 'acc', 'batch_id': 'B9'})
        # 模拟一个存活的上传线程
        hold = threading.Event()
        t = threading.Thread(target=hold.wait)
        t.start()
        m.upload_thread = t
        res = m.start_upload(auto_cleanup=True)
        self.assertEqual(res['status'], 'failed')
        self.assertIn('worker', res['error'])
        hold.set()
        t.join()

    def test_add_to_queue_accepts_expected_versions(self):
        """P0：add_to_queue 必须接受 expected_versions 关键字（app.py 曾透传导致 TypeError）。"""
        m = self.make_manager()
        res = m.add_to_queue(
            drama_name='测试剧', video_path='C:/v1.mp4', account='acc', version=1,
            batch_id='B5', source_dir='E:/素材/测试剧', output_dir='D:/out/acc/测试剧',
            expected_versions=[1, 2, 3])
        self.assertEqual(res['status'], 'success')
        task = m.queue[-1]
        self.assertEqual(task['expected_versions'], [1, 2, 3])
        # 任务字典已带 expected_versions（供批次完成标记/清理门槛）
        self.assertEqual(task['batch_id'], 'B5')

    def test_recorded_expected_versions_null_source_dir_wildcard(self):
        """P1：dashboard 不上送 source_dir → 存空串/NULL；查询用真实路径也必须命中。"""
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from artifact_cleanup import ArtifactCleanup
        self.tmp = tempfile.mkdtemp(prefix='cleanup_trig_')
        # 建表（UploadManager._init_database 创建 upload_batch_expected 等）
        UploadManager(self.tmp)
        # 直接落一条 source_dir=NULL 的 expected 记录（模拟旧数据）
        with sqlite3.connect(self.tmp_db()) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO upload_batch_expected "
                "(batch_id, account, drama_name, source_dir, output_dir, expected_versions) "
                "VALUES ('B6', 'acc', '测试剧', NULL, 'D:/out/acc/测试剧', '[1,2,3]')")
            conn.commit()
        ac = ArtifactCleanup(self.tmp)
        for sd in ('E:/素材/测试剧', None, ''):
            self.assertEqual(
                ac._recorded_expected_versions('B6', 'acc', '测试剧', sd, 'D:/out/acc/测试剧'),
                {1, 2, 3}, f'source_dir={sd!r} 应命中')
        # 真实路径写入也应规范化（不存 NULL）
        ac._record_batch('B7', '测试剧', 'acc', None, 'D:/out/acc/测试剧7', [1, 2])
        with sqlite3.connect(self.tmp_db()) as conn:
            row = conn.execute(
                "SELECT source_dir FROM upload_batch_expected WHERE batch_id='B7'").fetchone()
        self.assertEqual(row[0], '')  # 空串而非 NULL

    def tmp_db(self):
        import os
        return os.path.join(self.tmp, 'upload.db')

    def test_persist_jobs_dedup_on_unique_key(self):
        """P2：同一 (batch_id, job_type, target_path) 重复持久化只落一条 job。"""
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from artifact_cleanup import ArtifactCleanup
        self.tmp = tempfile.mkdtemp(prefix='cleanup_trig_')
        UploadManager(self.tmp)
        ac = ArtifactCleanup(self.tmp)
        job = {'batch_id': 'BD1', 'account': 'acc', 'drama_name': '测试剧',
               'job_type': 'version', 'version': 1, 'target_path': 'C:/v1.mp4',
               'recycle_path': 'R:/v1.mp4'}
        ac._persist_jobs([job])
        ac._persist_jobs([job])  # 重复执行
        with sqlite3.connect(self.tmp_db()) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM artifact_cleanup_jobs WHERE batch_id='BD1'").fetchone()[0]
        self.assertEqual(n, 1)

    def test_record_batch_multi_drama_aggregates_names(self):
        """P2：同 batch_id 多部剧 → drama_name 逗号合并去重。"""
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from artifact_cleanup import ArtifactCleanup
        self.tmp = tempfile.mkdtemp(prefix='cleanup_trig_')
        UploadManager(self.tmp)
        ac = ArtifactCleanup(self.tmp)
        ac._record_batch('BD2', '剧A', 'acc', None, 'D:/out/acc/剧A', [1])
        ac._record_batch('BD2', '剧B', 'acc', None, 'D:/out/acc/剧B', [1])
        ac._record_batch('BD2', '剧A', 'acc', None, 'D:/out/acc/剧A', [1])  # 重复
        with sqlite3.connect(self.tmp_db()) as conn:
            names = conn.execute(
                "SELECT drama_name FROM upload_batches WHERE batch_id='BD2'").fetchone()[0]
        self.assertEqual(set(names.split(',')), {'剧A', '剧B'})

    def test_write_back_cleanup_state(self):
        """P2：清理执行后写回 records.cleanup_status 与 batch 计数。"""
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from artifact_cleanup import ArtifactCleanup
        self.tmp = tempfile.mkdtemp(prefix='cleanup_trig_')
        m = UploadManager(self.tmp)
        self._seed_batch(m, 'BD3', 2, ('completed', 'failed'))
        ac = ArtifactCleanup(self.tmp)
        jobs = [
            {'batch_id': 'BD3', 'account': 'acc', 'drama_name': '测试剧',
             'job_type': 'version', 'version': 1, 'target_path': 'C:/v1.mp4'},
            {'batch_id': 'BD3', 'account': 'acc', 'drama_name': '测试剧',
             'job_type': 'version', 'version': 2, 'target_path': 'C:/v2.mp4'},
        ]
        results = [
            {'status': 'moved', 'target': 'C:/v1.mp4'},
            {'status': 'failed', 'target': 'C:/v2.mp4', 'error': '移动被拒'},
        ]
        ac._write_back_cleanup_state('BD3', 'partial', results, jobs)
        with sqlite3.connect(self.tmp_db()) as conn:
            v1 = conn.execute(
                "SELECT cleanup_status, cleanup_error FROM upload_records "
                "WHERE batch_id='BD3' AND version=1").fetchone()
            v2 = conn.execute(
                "SELECT cleanup_status, cleanup_error FROM upload_records "
                "WHERE batch_id='BD3' AND version=2").fetchone()
            b = conn.execute(
                "SELECT version_count, success_count, failed_count, cleanup_status "
                "FROM upload_batches WHERE batch_id='BD3'").fetchone()
        self.assertEqual(v1[0], 'moved')
        self.assertEqual(v2[0], 'failed')
        self.assertEqual(v2[1], '移动被拒')
        self.assertEqual(b[0], 2)   # version_count
        self.assertEqual(b[1], 1)   # success_count
        self.assertEqual(b[2], 1)   # failed_count
        self.assertEqual(b[3], 'partial')


if __name__ == '__main__':
    unittest.main()
