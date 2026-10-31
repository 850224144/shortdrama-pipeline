# -*- coding: utf-8 -*-
"""R2 切片队列持久化：服务重建恢复、停止后续跑、事件查询回归测试。

验收（设计文档 R2）：
- 服务重建 manager 后恢复 pending/cutting（cutting 归位 pending）；
- 停止任务重启后仍可继续；
- 事件可查询。
"""

import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cut_manager import BatchCutManager
from job_store import JobStore


class BlockingProcessor:
    """manual_cut 模拟。

    - 'cancelled'：阻塞到 cancel_event 被置位（用于测试中途停止）；
    - 其他：立即返回（success/skipped/failed），不阻塞。
    by_drama 按剧名指定结果，未命中回落 result_status。
    """

    def __init__(self, result_status='success', by_drama=None):
        self.result_status = result_status
        self.by_drama = by_drama or {}
        self.started = threading.Event()
        self.cancel_event = None
        self.calls = []

    def manual_cut(self, drama_name, resume=False, cancel_event=None):
        self.calls.append((drama_name, resume))
        self.cancel_event = cancel_event
        status = self.by_drama.get(drama_name, self.result_status)
        self.started.set()
        if status == 'cancelled':
            cancel_event.wait(3)
            if cancel_event.is_set():
                return {'status': 'cancelled', 'cancelled': True,
                        'error': '用户已停止切片'}
            return {'status': 'success', 'cut': 3, 'output_dirs': ['o1']}
        if status == 'skipped':
            return {'status': 'skipped', 'output_dirs': ['existing']}
        if status == 'failed':
            return {'status': 'failed', 'error': '渲染失败'}
        return {'status': 'success', 'cut': 3, 'output_dirs': ['o1']}


class CutPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, 'jobs.db')

    def _reopen(self, processor):
        """模拟服务重启：同一 db 路径新建 manager（新建连接）。"""
        return BatchCutManager(processor, data_dir=self._tmp.name)

    def test_pending_job_recovered_after_manager_rebuild(self):
        m1 = BatchCutManager(BlockingProcessor(), data_dir=self._tmp.name)
        m1.add_to_queue('测试剧', account='小桃追剧中')
        m2 = self._reopen(BlockingProcessor())
        st = m2.get_status()
        self.assertEqual(st['queue_length'], 1)
        self.assertEqual(st['queue'][0]['drama_name'], '测试剧')
        self.assertEqual(st['queue'][0]['status'], 'pending')
        self.assertEqual(st['queue'][0]['account'], '小桃追剧中')

    def test_cutting_job_recovered_as_pending(self):
        """进程崩溃时正在切的任务恢复为 pending，可重新切片。"""
        # 直接向库写入一个 'cutting' 状态任务，模拟崩溃现场
        JobStore(self.db).put({
            'id': 'crash1', 'job_type': 'cut', 'status': 'cutting',
            'params': {'drama_name': '崩溃剧', 'account': '小桃追剧中',
                       'resume': False},
        })
        m = self._reopen(BlockingProcessor())
        st = m.get_status()
        self.assertEqual(st['queue_length'], 1)
        self.assertEqual(st['queue'][0]['drama_name'], '崩溃剧')
        self.assertEqual(st['queue'][0]['status'], 'pending')
        self.assertEqual(st['failed_count'], 0, '崩溃不能计为失败')

    def test_stopped_task_continues_after_restart(self):
        """停止的任务保持 pending，重建后仍可继续切片。"""
        proc1 = BlockingProcessor(result_status='cancelled')
        m1 = BatchCutManager(proc1, data_dir=self._tmp.name)
        m1.add_to_queue('停止剧')
        self.assertEqual(m1.start_cutting()['status'], 'success')
        self.assertTrue(proc1.started.wait(1))
        m1.stop_cutting()
        m1.cut_thread.join(3)
        self.assertFalse(m1.cut_thread.is_alive())

        # 库中任务必须为 pending（可继续）
        stored = JobStore(self.db).list('cut')
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]['status'], 'pending')

        # 重建后恢复为 pending，再次启动即继续
        proc2 = BlockingProcessor()
        m2 = self._reopen(proc2)
        st = m2.get_status()
        self.assertEqual(st['queue_length'], 1)
        self.assertEqual(st['queue'][0]['status'], 'pending')
        self.assertEqual(m2.start_cutting()['status'], 'success')
        self.assertTrue(proc2.started.wait(1))
        m2.cut_thread.join(3)
        self.assertEqual(m2.get_status()['completed_count'], 1)

    def test_completed_and_failed_jobs_retained_after_restart(self):
        proc = BlockingProcessor(by_drama={'成功剧': 'success',
                                           '失败剧': 'failed'})
        m1 = BatchCutManager(proc, data_dir=self._tmp.name)
        m1.add_to_queue('成功剧')
        m1.add_to_queue('失败剧')
        self.assertEqual(m1.start_cutting()['status'], 'success')
        m1.cut_thread.join(3)
        st = m1.get_status()
        self.assertEqual(st['completed_count'], 1)
        self.assertEqual(st['failed_count'], 1)

        m2 = self._reopen(BlockingProcessor())
        st2 = m2.get_status()
        self.assertEqual(st2['completed_count'], 1)
        self.assertEqual(st2['failed_count'], 1)
        self.assertEqual(st2['completed'][0]['drama_name'], '成功剧')
        self.assertEqual(st2['failed'][0]['drama_name'], '失败剧')
        # 完成/失败任务在重建后不得被误当成待切
        self.assertEqual(st2['queue_length'], 0)

    def test_resume_flag_preserved_across_restart(self):
        m1 = BatchCutManager(BlockingProcessor(), data_dir=self._tmp.name)
        m1.add_to_queue('续跑剧', resume=True)
        m2 = self._reopen(BlockingProcessor())
        self.assertTrue(m2.get_status()['queue'][0]['resume'])

    def test_events_persisted_and_queryable(self):
        m = BatchCutManager(BlockingProcessor(), data_dir=self._tmp.name)
        m.add_to_queue('事件剧')
        self.assertEqual(m.start_cutting()['status'], 'success')
        m.cut_thread.join(3)
        # 队列级事件（job_id=''）
        all_events = m.get_events()
        self.assertGreaterEqual(len(all_events), 3, '应有入队/开始/完成事件')
        messages = [e['message'] for e in all_events]
        self.assertTrue(any('添加切片任务' in s for s in messages))
        self.assertTrue(any('切片完成' in s for s in messages))
        # 按任务查询
        task_id = m.get_status()['completed'][0]['id']
        task_events = m.get_events(job_id=task_id)
        self.assertGreaterEqual(len(task_events), 1)

    def test_terminal_event_carries_stage_duration(self):
        """R6：终态事件（cut_completed/cut_failed/cut_stopped）带阶段耗时元数据。"""
        proc = BlockingProcessor(by_drama={'成功剧': 'success', '失败剧': 'failed'})
        m = BatchCutManager(proc, data_dir=self._tmp.name)
        m.add_to_queue('成功剧')
        m.add_to_queue('失败剧')
        self.assertEqual(m.start_cutting()['status'], 'success')
        m.cut_thread.join(3)
        events = m.get_events()
        completed = [e for e in events if e['event'] == 'cut_completed']
        failed = [e for e in events if e['event'] == 'cut_failed']
        self.assertEqual(len(completed), 1)
        self.assertEqual(len(failed), 1)
        meta = completed[0].get('metadata') or {}
        self.assertIn('duration_seconds', meta, '完成事件必须带阶段耗时')
        self.assertIn('stage', meta)
        self.assertEqual(meta['stage'], 'cut')
        self.assertGreaterEqual(meta['duration_seconds'], 0)
        fmeta = failed[0].get('metadata') or {}
        self.assertIn('duration_seconds', fmeta, '失败事件必须带阶段耗时')

    def test_clear_queue_deletes_persisted_jobs(self):
        m1 = BatchCutManager(BlockingProcessor(), data_dir=self._tmp.name)
        m1.add_to_queue('待清剧')
        self.assertEqual(m1.clear_queue('pending')['cleared'], 1)
        self.assertEqual(JobStore(self.db).list('cut'), [])
        m2 = self._reopen(BlockingProcessor())
        self.assertEqual(m2.get_status()['queue_length'], 0)

    def test_recovered_pending_blocks_duplicate_add(self):
        m1 = BatchCutManager(BlockingProcessor(), data_dir=self._tmp.name)
        m1.add_to_queue('去重剧')
        m2 = self._reopen(BlockingProcessor())
        dup = m2.add_to_queue('去重剧')
        self.assertEqual(dup['status'], 'failed')
        self.assertIn('已在队列中', dup['error'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
