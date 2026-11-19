# -*- coding: utf-8 -*-
"""R4 原子批量上传：一次校验 + 一个事务创建整批，无半批次。

验收（设计文档 R4）：
- 一次请求创建整批；重复、缺失和失败有结构化明细；无半批次。
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from upload_manager import UploadManager


class AddBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='add_batch_')
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.mgr = UploadManager(self.tmp)
        # 造 3 个真实文件
        self.paths = []
        for i in (1, 2, 3):
            p = os.path.join(self.tmp, f'剧A_{i}_打脸_去重版.mp4')
            open(p, 'w').close()
            self.paths.append(p)

    def _item(self, version, path=None, drama='剧A', account='福桃说剧'):
        return {
            'drama_name': drama, 'video_path': path or self.paths[version - 1],
            'account': account, 'version': version,
            'batch_id': 'B_ATOMIC', 'output_dir': self.tmp,
            'expected_versions': [1, 2, 3],
        }

    def test_creates_entire_batch_in_one_request(self):
        items = [self._item(1), self._item(2), self._item(3)]
        res = self.mgr.add_batch(items)
        self.assertEqual(res['status'], 'success')
        self.assertEqual(res['accepted'], 3)
        self.assertEqual(len(res['ids']), 3)
        self.assertEqual(res['duplicates'], [])
        self.assertEqual(res['missing'], [])
        self.assertEqual(res['rejected'], [])
        self.assertEqual(len(self.mgr.queue), 3)
        # 落库校验
        with sqlite3.connect(self.mgr.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM upload_records WHERE batch_id='B_ATOMIC'"
            ).fetchone()[0]
        self.assertEqual(count, 3)

    def test_missing_file_reported_and_others_accepted(self):
        items = [self._item(1), self._item(2, path=os.path.join(self.tmp, '不存在.mp4'))]
        res = self.mgr.add_batch(items)
        self.assertEqual(res['accepted'], 1)
        self.assertEqual(len(res['missing']), 1)
        self.assertEqual(res['missing'][0]['version'], 2)
        self.assertEqual(len(self.mgr.queue), 1, '缺失条不得入队，其余正常')
        self.assertEqual(res['missing'][0]['video_path'],
                         os.path.join(self.tmp, '不存在.mp4'))

    def test_duplicate_reported_with_structured_detail(self):
        # 先入队 V1
        first = self.mgr.add_to_queue(
            drama_name='剧A', video_path=self.paths[0], account='福桃说剧',
            version=1, batch_id='B_OLD')
        self.assertEqual(first['status'], 'success')
        res = self.mgr.add_batch([self._item(1)])
        self.assertEqual(res['accepted'], 0)
        self.assertEqual(len(res['duplicates']), 1)
        self.assertEqual(res['duplicates'][0]['account'], '福桃说剧')
        self.assertEqual(res['duplicates'][0]['version'], 1)
        self.assertEqual(len(self.mgr.queue), 1, '重复条不得重复入队')

    def test_missing_required_fields_rejected(self):
        res = self.mgr.add_batch([
            {'drama_name': '剧B', 'video_path': '', 'version': 1},
            {'drama_name': '', 'video_path': self.paths[0], 'version': 1},
            {'drama_name': '剧B', 'video_path': self.paths[1], 'version': None},
        ])
        self.assertEqual(res['accepted'], 0)
        self.assertEqual(len(res['rejected']), 3)
        self.assertTrue(all('必填' in r['reason'] for r in res['rejected']))
        self.assertEqual(len(self.mgr.queue), 0)

    def test_empty_items_is_noop(self):
        res = self.mgr.add_batch([])
        self.assertEqual(res['status'], 'success')
        self.assertEqual(res['accepted'], 0)
        self.assertEqual(len(self.mgr.queue), 0)

    def test_batch_assigns_distinct_publish_times_across_dramas(self):
        """回归：5 部剧同版本同批入队，不得共用同一个定时发表时刻。"""
        from datetime import datetime, timedelta
        items = []
        for i in range(1, 6):
            p = os.path.join(self.tmp, f'剧{i}_1_去重版.mp4')
            open(p, 'w').close()
            items.append({
                'drama_name': f'剧{i}', 'video_path': p, 'account': '福桃说剧',
                'version': 1, 'batch_id': 'B_PLAN', 'output_dir': self.tmp,
                'expected_versions': [1],
            })
        tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        res = self.mgr.add_batch(items, publish={
            'date': tomorrow, 'start_time': '07:00',
            'interval_minutes': 120, 'jitter_minutes': 0})
        self.assertEqual(res['accepted'], 5)
        times = [d['publish_time'] for d in res['publish_plan']]
        self.assertEqual(len(times), 5)
        self.assertEqual(len(set(times)), 5, '5 条必须拿到 5 个不同时刻')
        for i, t in enumerate(times):
            expect = (datetime.strptime(tomorrow + ' 07:00', '%Y-%m-%d %H:%M')
                      + timedelta(minutes=120 * i)).strftime('%Y-%m-%d %H:%M')
            self.assertEqual(t, expect, f'第{i + 1}条应按间隔顺延')
        # 落库与队列口径一致
        self.assertEqual({t['publish_time'] for t in self.mgr.queue}, set(times))

    def test_batch_avoids_times_already_queued(self):
        """当日已有 07:00 的待发任务时，新批次应从下一个空档开始。"""
        from datetime import datetime, timedelta
        tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        p0 = os.path.join(self.tmp, '剧X_1_去重版.mp4')
        open(p0, 'w').close()
        self.mgr.add_to_queue(drama_name='剧X', video_path=p0, account='福桃说剧',
                              version=1, publish_time=f'{tomorrow} 07:00')
        p1 = os.path.join(self.tmp, '剧Y_1_去重版.mp4')
        open(p1, 'w').close()
        res = self.mgr.add_batch([{
            'drama_name': '剧Y', 'video_path': p1, 'account': '福桃说剧',
            'version': 1, 'batch_id': 'B_AVOID', 'output_dir': self.tmp,
        }], publish={'date': tomorrow, 'start_time': '07:00',
                     'interval_minutes': 120, 'jitter_minutes': 0})
        self.assertEqual(res['accepted'], 1)
        self.assertNotEqual(res['publish_plan'][0]['publish_time'], f'{tomorrow} 07:00')
        self.assertEqual(res['publish_plan'][0]['publish_time'], f'{tomorrow} 09:00')
    def test_per_drama_day_mode_plumbs_through(self):
        """排期方式=一天一部剧：两部剧各自占一天。"""
        from datetime import datetime, timedelta
        items = []
        for drama, versions in (('剧P', (1, 2)), ('剧Q', (1, 2))):
            for v in versions:
                p = os.path.join(self.tmp, f'{drama}_{v}_去重版.mp4')
                open(p, 'w').close()
                items.append({
                    'drama_name': drama, 'video_path': p, 'account': '福桃说剧',
                    'version': v, 'batch_id': 'B_DDAY', 'output_dir': self.tmp,
                })
        tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        day2 = (datetime.now() + timedelta(days=2)).strftime('%Y-%m-%d')
        res = self.mgr.add_batch(items, publish={
            'date': tomorrow, 'start_time': '07:00', 'window_end': '23:00',
            'mode': 'per_drama_day', 'interval_minutes': 120, 'jitter_minutes': 0})
        self.assertEqual(res['accepted'], 4)
        plan = res['publish_plan']
        self.assertTrue(all(d['publish_time'].startswith(tomorrow) for d in plan[:2]))
        self.assertTrue(all(d['publish_time'].startswith(day2) for d in plan[2:]))
        self.assertEqual([d['publish_time'].split()[1] for d in plan], [
            '07:00', '09:00', '07:00', '09:00'])

    def test_no_half_batch_on_mixed_validation(self):
        """混合场景：1 有效 + 1 重复 + 1 缺失 → 只有有效的入队（无半批次残留）。"""
        # 预先入队的 V2 必须用另一个文件：查重是按 video_path 判定的，
        # 复用 paths[0] 会让本应有效的 V1 也被判为重复（测试自己造出的冲突）。
        extra = os.path.join(self.tmp, '剧A_4_预置_去重版.mp4')
        open(extra, 'w').close()
        self.mgr.add_to_queue(drama_name='剧A', video_path=extra,
                              account='福桃说剧', version=2)
        res = self.mgr.add_batch([
            self._item(1),
            self._item(2),                                   # 重复（已在队列）
            self._item(3, path=os.path.join(self.tmp, 'x.mp4')),  # 缺失
        ])
        self.assertEqual(res['accepted'], 1)
        self.assertEqual(len(res['duplicates']), 1)
        self.assertEqual(len(res['missing']), 1)
        # 队列 = 已有 V2 + 新入队 V1
        self.assertEqual(len(self.mgr.queue), 2)
        versions = {t['version'] for t in self.mgr.queue}
        self.assertEqual(versions, {1, 2})


if __name__ == '__main__':
    unittest.main()
