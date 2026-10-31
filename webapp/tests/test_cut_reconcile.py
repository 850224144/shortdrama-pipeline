# -*- coding: utf-8 -*-
"""R3 状态对账：僵死归位、缺成片标记、可恢复现场、心跳刷新回归测试。

验收（设计文档 R3）：
- 启动恢复中断任务（心跳超时的 cutting → pending）；
- 错误 running 状态被修复（completed 但无成片 → failed）；
- 对账结果可观测（返回报告 + 事件）。
"""

import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cut_manager import BatchCutManager
from job_store import JobStore


class BlockingProcessor:
    def __init__(self):
        self.started = threading.Event()
        self.cancel_event = None

    def manual_cut(self, drama_name, resume=False, cancel_event=None):
        self.cancel_event = cancel_event
        self.started.set()
        cancel_event.wait(5)
        return {'status': 'cancelled', 'cancelled': True,
                'error': '用户已停止切片'}


def _job(job_id, status, drama, account='小桃追剧中', heartbeat=None):
    return {
        'id': job_id, 'job_type': 'cut', 'status': status,
        'params': {'drama_name': drama, 'account': account, 'resume': False},
        'heartbeat_at': heartbeat,
    }


class CutReconcileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, 'jobs.db')
        self.out_root = os.path.join(self._tmp.name, 'output')
        # 建一个含成片的输出目录（对账时作为「磁盘已有成片」的证据）
        self.drama = '已出片剧'
        self.out_dir = os.path.join(self.out_root, '小桃追剧中', self.drama)
        os.makedirs(self.out_dir, exist_ok=True)
        open(os.path.join(self.out_dir, f'{self.drama}_1_打脸_去重版.mp4'),
             'w').close()

    def _manager(self):
        return BatchCutManager(BlockingProcessor(), data_dir=self._tmp.name,
                               output_root=self.out_root)

    def test_stale_cutting_reset_to_pending(self):
        JobStore(self.db).put(_job('j1', 'cutting', '中断剧',
                                   heartbeat='2000-01-01 00:00:00'))
        m = self._manager()
        rep = m.reconcile()
        self.assertEqual(rep['stale_reset'], 1)
        self.assertEqual(rep['changed'][0]['action'], 'stale_reset')
        st = m.get_status()
        self.assertEqual(st['queue_length'], 1)
        self.assertEqual(st['queue'][0]['status'], 'pending')
        self.assertEqual(st['queue'][0]['drama_name'], '中断剧')
        # 事件可观测
        self.assertTrue(any(e['event'] == 'reconcile_stale' for e in m.get_events()))

    def test_fresh_cutting_not_reset(self):
        import datetime as dt
        JobStore(self.db).put(_job('j2', 'cutting', '进行中剧',
                                   heartbeat=dt.datetime.now().strftime(
                                       '%Y-%m-%d %H:%M:%S')))
        m = self._manager()
        rep = m.reconcile()
        self.assertEqual(rep['stale_reset'], 0, '心跳新鲜的任务不得被归位')

    def test_completed_missing_output_marked_failed(self):
        JobStore(self.db).put(_job('j3', 'completed', '无成片剧'))
        m = self._manager()
        rep = m.reconcile()
        self.assertEqual(rep['completed_missing'], 1)
        self.assertEqual(m.get_status()['failed'][0]['drama_name'], '无成片剧')
        self.assertEqual(m.get_status()['failed'][0]['status'], 'failed')

    def test_completed_with_output_unchanged(self):
        JobStore(self.db).put(_job('j4', 'completed', self.drama))
        m = self._manager()
        rep = m.reconcile()
        self.assertEqual(rep['completed_missing'], 0, '有成片不得标失败')
        self.assertEqual(m.get_status()['completed_count'], 1)

    def test_pending_with_output_flagged_recoverable(self):
        JobStore(self.db).put(_job('j5', 'pending', self.drama))
        m = self._manager()
        rep = m.reconcile()
        self.assertEqual(rep['pending_with_output'], 1)
        # 状态不改（交用户确认），仅打标记
        st = m.get_status()
        self.assertEqual(st['queue_length'], 1)
        self.assertEqual(st['queue'][0]['status'], 'pending')
        self.assertTrue(st['queue'][0].get('recovered'))
        self.assertTrue(any(e['event'] == 'recoverable_scene'
                            for e in m.get_events()))

    def test_reconcile_idempotent(self):
        JobStore(self.db).put(_job('j6', 'completed', '无成片剧'))
        m = self._manager()
        first = m.reconcile()
        self.assertEqual(first['completed_missing'], 1)
        second = m.reconcile()
        self.assertEqual(second['completed_missing'], 0,
                         '重复对账不应重复标记')
        self.assertEqual(second['changed'], [])

    def test_heartbeat_refreshed_while_cutting(self):
        proc = BlockingProcessor()
        m = BatchCutManager(proc, data_dir=self._tmp.name,
                            output_root=self.out_root)
        m.HEARTBEAT_INTERVAL = 1
        m.HEARTBEAT_WINDOW = 3
        m.add_to_queue('心跳剧')
        m.start_cutting()
        self.assertTrue(proc.started.wait(1))
        # 等待一次心跳刷新
        deadline = time.time() + 3
        refreshed = False
        while time.time() < deadline:
            job = JobStore(self.db).get(
                m.get_status()['current_task']['id'])
            if job and job.get('heartbeat_at'):
                refreshed = True
                break
            time.sleep(0.2)
        self.assertTrue(refreshed, '切片运行中心跳应被刷新')
        m.stop_cutting()
        m.cut_thread.join(3)


if __name__ == '__main__':
    import time
    unittest.main(verbosity=2)
