# -*- coding: utf-8 -*-
"""批次级定时发表时间分配：同一账号从起始时间起按间隔顺延 + 抖动 + 避让。

背景（BUG）：旧逻辑 _compute_publish_time 只按版本号取槽，5 部剧的 V1 全部
拿到 times[0]，同日同刻齐发。这里锁定新规则：
- 同一账号内逐条递增 interval，抖动不超过 interval/3、不打乱顺序；
- 与已占用时刻相差 < MIN_PUBLISH_GAP 就后移，不撞点；
- 缺省=次日 + config 首槽位 + 120 分钟 + ±10 抖动；
- 日期越界/非法值回退，不抛异常。
"""
import os
import random
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from upload_manager import UploadManager


def _opts(**kw):
    base = {'date': (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d'),
            'start_time': '07:00', 'interval_minutes': 120, 'jitter_minutes': 0}
    base.update(kw)
    return base


class PlanPublishTimesTests(unittest.TestCase):
    def test_same_version_across_dramas_gets_distinct_times(self):
        """核心 bug：不同剧的 V1 不得撞在同一时刻。"""
        entries = [{'account': '福桃说剧'} for _ in range(5)]
        out = UploadManager.plan_publish_times(entries, _opts())
        self.assertEqual(len(out), 5)
        self.assertEqual(len(set(out)), 5, '同一批 5 条必须 5 个不同时刻')
        times = [datetime.strptime(t, '%Y-%m-%d %H:%M') for t in out]
        for a, b in zip(times, times[1:]):
            self.assertEqual((b - a).total_seconds(), 120 * 60, '间隔应等于 interval')

    def test_jitter_stays_within_bound_and_keeps_order(self):
        entries = [{'account': 'A'} for _ in range(6)]
        out = UploadManager.plan_publish_times(
            entries, _opts(interval_minutes=60, jitter_minutes=20),
            rng=random.Random(2026))
        base = datetime.strptime(_opts()['date'] + ' 07:00', '%Y-%m-%d %H:%M')
        times = [datetime.strptime(t, '%Y-%m-%d %H:%M') for t in out]
        for i, t in enumerate(times):
            expect = base + timedelta(minutes=60 * i)
            self.assertLessEqual(abs((t - expect).total_seconds()), 20 * 60 + 1,
                                 f'第{i}条抖动超出 ±20 分钟')
        self.assertEqual(times, sorted(times), '抖动不得打乱发表顺序')

    def test_jitter_clamped_to_interval_third(self):
        entries = [{'account': 'A'}]
        opts = UploadManager._normalize_publish_options(
            _opts(interval_minutes=30, jitter_minutes=120))
        self.assertLessEqual(opts['jitter_minutes'], 10, '30 分钟间隔的抖动上限是 10')

    def test_window_rolls_to_next_day_not_midnight(self):
        """多剧/多版本排满当天窗口（07:00–23:00）后顺延次日 07:00，不排凌晨。"""
        entries = [{'account': 'A'} for _ in range(12)]
        out = UploadManager.plan_publish_times(entries, _opts())
        day1 = _opts()['date']
        day2 = (datetime.strptime(day1, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
        self.assertEqual(out[0].split()[1], '07:00')
        self.assertEqual(out[8].split()[1], '23:00', '当天第 9 条排到窗口终点')
        self.assertEqual(out[9].split()[0], day2, '第 10 条应顺延次日')
        self.assertEqual(out[9].split()[1], '07:00')
        self.assertEqual(out[11].split()[1], '11:00')
        for t in out:
            hour = int(t.split()[1].split(':')[0])
            self.assertTrue(7 <= hour <= 23, f'不得排到窗口外: {t}')

    def test_order_strictly_increasing_across_window_overflow(self):
        """跨天顺延 + 抖动 + 避让后，发表顺序仍必须严格递增（V2 不得抢在 V1 前）。"""
        entries = [{'account': 'A'} for _ in range(20)]
        taken = {'A': [datetime.strptime(_opts()['date'] + ' 11:00', '%Y-%m-%d %H:%M')]}
        out = UploadManager.plan_publish_times(
            entries, _opts(jitter_minutes=10), taken_by_account=taken,
            rng=random.Random(2027))
        times = [datetime.strptime(t, '%Y-%m-%d %H:%M') for t in out]
        self.assertEqual(times, sorted(times), '排期顺序不得回退')
        self.assertEqual(len(set(out)), len(out), '不得出现同刻重复')

    def test_per_drama_day_gives_each_drama_its_own_day(self):
        entries = ([{'account': 'A', 'drama': '剧A'}] * 3
                   + [{'account': 'A', 'drama': '剧B'}] * 3)
        out = UploadManager.plan_publish_times(
            entries, _opts(mode='per_drama_day'))
        day1 = _opts()['date']
        day2 = (datetime.strptime(day1, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
        self.assertEqual([t.split()[1] for t in out[:3]], ['07:00', '09:00', '11:00'])
        self.assertTrue(all(t.startswith(day1) for t in out[:3]))
        self.assertTrue(all(t.startswith(day2) for t in out[3:]),
                        '第二部剧应从第二天开始')
        self.assertEqual([t.split()[1] for t in out[3:]], ['07:00', '09:00', '11:00'])

    def test_per_drama_day_overflow_keeps_drama_contiguous(self):
        """剧内条数超过每天容量：同一部剧连续顺延，下一部剧再往后推一天。"""
        entries = ([{'account': 'A', 'drama': '剧A'}] * 12
                   + [{'account': 'A', 'drama': '剧B'}] * 1)
        out = UploadManager.plan_publish_times(
            entries, _opts(mode='per_drama_day'))
        day1 = datetime.strptime(_opts()['date'], '%Y-%m-%d')
        day2 = (day1 + timedelta(days=1)).strftime('%Y-%m-%d')
        day3 = (day1 + timedelta(days=2)).strftime('%Y-%m-%d')
        self.assertEqual(out[0].split()[1], '07:00')
        self.assertEqual(out[8].split()[1], '23:00')
        self.assertTrue(out[9].startswith(day2), '第 10 条起顺延次日')
        self.assertEqual(out[9].split()[1], '07:00')
        self.assertEqual(out[11].split()[1], '11:00')
        self.assertTrue(out[12].startswith(day3), '下一部剧再推一天')
        self.assertEqual(out[12].split()[1], '07:00')

    def test_sequence_mode_matches_legacy_expectation(self):
        entries = [{'account': 'A', 'drama': '剧A'}] * 3
        out = UploadManager.plan_publish_times(entries, _opts())
        self.assertEqual([t.split()[1] for t in out], ['07:00', '09:00', '11:00'])
        self.assertTrue(all(t.startswith(_opts()['date']) for t in out))

    def test_avoids_taken_times(self):
        taken = {'A': [datetime.strptime(_opts()['date'] + ' 07:00', '%Y-%m-%d %H:%M'),
                       datetime.strptime(_opts()['date'] + ' 09:00', '%Y-%m-%d %H:%M')]}
        out = UploadManager.plan_publish_times(
            [{'account': 'A'}, {'account': 'A'}], _opts(), taken_by_account=taken)
        self.assertEqual(out[0].split()[1], '11:00', '07:00/09:00 被占用应顺延到下一空档')
        self.assertEqual(out[1].split()[1], '13:00', '第二条继续顺延，保持固定间隔')

    def test_other_account_does_not_block(self):
        """不同账号同时发不冲突：A 的占用不应挤占 B 的起始槽位。"""
        taken = {'A': [datetime.strptime(_opts()['date'] + ' 07:00', '%Y-%m-%d %H:%M')]}
        out = UploadManager.plan_publish_times(
            [{'account': 'B'}], _opts(), taken_by_account=taken)
        self.assertEqual(out[0].split()[1], '07:00')

    def test_default_is_next_day_with_config_defaults(self):
        with patch('config.get_publish_schedule', return_value={
                'day_offset': 1, 'times': ['07:00'],
                'interval_minutes': 120, 'jitter_minutes': 0}):
            out = UploadManager.plan_publish_times([{'account': 'A'}], None)
        tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        self.assertTrue(out[0].startswith(tomorrow), f'缺省应为次日: {out[0]}')
        self.assertEqual(out[0].split()[1], '07:00', '缺省起点取 config 首槽位')

    def test_past_start_rolls_to_next_day(self):
        today = datetime.now().strftime('%Y-%m-%d')
        out = UploadManager.plan_publish_times(
            [{'account': 'A'}], _opts(date=today, start_time='00:00'))
        self.assertTrue(out[0].startswith(
            (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')))

    def test_invalid_options_fall_back(self):
        out = UploadManager.plan_publish_times(
            [{'account': 'A'}],
            {'date': '乱写', 'start_time': '25:99', 'interval_minutes': -5,
             'jitter_minutes': 'x'})
        self.assertEqual(len(out), 1)
        self.assertRegex(out[0], r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$')

    def test_lead_days_clamped(self):
        far = (datetime.now() + timedelta(days=99)).strftime('%Y-%m-%d')
        opts = UploadManager._normalize_publish_options({'date': far})
        ceiling = (datetime.now() + timedelta(days=UploadManager.MAX_PUBLISH_LEAD_DAYS)).date()
        self.assertLessEqual(opts['date'], ceiling)


if __name__ == '__main__':
    unittest.main()
