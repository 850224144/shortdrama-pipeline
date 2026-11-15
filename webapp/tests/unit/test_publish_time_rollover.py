# -*- coding: utf-8 -*-
"""定时发表时间过期顺延单测。

背景（BUG）：publish_time 入队时按 +day_offset 算死（2026-11-10 15:44 入队
+1 天 = 11-11 07:00–17:00）；批次 11-11 22:52–23:53 深夜才上传，目标时间已是
过去 → _set_scheduled_publish 设不上过去时间 → 静默回退保存草稿（勾了定时却
没发表）。修复：_ensure_future_publish_time 把过期时间顺延到最近未来槽位。
"""
import unittest
from datetime import datetime, timedelta

from wechat_uploader import WeChatChannelsUploader


def _uploader():
    # 方法不访问 self 属性，用裸实例即可（避免构造需要的浏览器/账号依赖）
    return object.__new__(WeChatChannelsUploader)


_SLOTS = ('07:00', '09:00', '11:00', '13:00', '15:00', '17:00',
          '19:00', '21:00', '23:00')


class TestEnsureFuturePublishTime(unittest.TestCase):
    def test_future_time_unchanged(self):
        """未过期时间原样返回（不动）。"""
        future = (datetime.now() + timedelta(days=2)).strftime('%Y-%m-%d %H:%M')
        self.assertEqual(_uploader()._ensure_future_publish_time(future), future)

    def test_past_time_rolled_to_future(self):
        """过期时间 → 顺延到未来槽位（结果 > now 且是合法槽位）。"""
        past = (datetime.now() - timedelta(hours=3)).strftime('%Y-%m-%d %H:%M')
        out = _uploader()._ensure_future_publish_time(past)
        parsed = datetime.strptime(out, '%Y-%m-%d %H:%M')
        self.assertGreater(parsed, datetime.now(), f'应顺延到未来: {out}')
        self.assertIn(out[11:], _SLOTS, f'必须是合法槽位: {out}')

    def test_yesterday_rolls_to_future_slot(self):
        """昨日过期时间 → 顺延到今天/明天未来槽位。"""
        yday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d %H:%M')
        out = _uploader()._ensure_future_publish_time(yday)
        parsed = datetime.strptime(out, '%Y-%m-%d %H:%M')
        self.assertGreater(parsed, datetime.now())
        self.assertIn(out[11:], _SLOTS)

    def test_invalid_format_returns_as_is(self):
        """解析不了的格式原样返回（交回默认时间兜底）。"""
        for bad in ('not-a-time', '', None):
            self.assertEqual(_uploader()._ensure_future_publish_time(bad), bad)

    def test_now_equals_publish_time_rolls(self):
        """等于 now 视为过期（已无意义），应顺延。"""
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
        out = _uploader()._ensure_future_publish_time(now_str)
        parsed = datetime.strptime(out, '%Y-%m-%d %H:%M')
        self.assertGreater(parsed, datetime.now())


class TestRolloverUsesRuntimeConfig(unittest.TestCase):
    """L4：顺延槽位必须读运行时 CONFIG（与入队算点 _compute_publish_time 同源）。

    旧实现读 DEFAULT_CONFIG，运行时改过槽位后，顺延结果会落到默认的 9 档时间轴，
    与入队算点/用户配置不一致。
    """

    def test_rollover_slots_follow_runtime_config(self):
        import config as _cfg
        from unittest import mock
        with mock.patch.object(_cfg, 'CONFIG', {
                'upload': {'publish_schedule': {'day_offset': 1, 'times': ['23:55']}}}):
            out = _uploader()._ensure_future_publish_time('2020-01-01 00:00')
        self.assertTrue(out.endswith('23:55'), f'应使用运行时槽位: {out}')
        self.assertGreater(datetime.strptime(out, '%Y-%m-%d %H:%M'), datetime.now())

    def test_compute_publish_time_shares_same_config(self):
        import config as _cfg
        from unittest import mock
        from upload_manager import UploadManager
        with mock.patch.object(_cfg, 'CONFIG', {
                'upload': {'publish_schedule': {'day_offset': 1, 'times': ['07:05', '23:55']}}}):
            v1 = UploadManager._compute_publish_time(1)
            v2 = UploadManager._compute_publish_time(2)
        self.assertTrue(v1.endswith('07:05'), v1)
        self.assertTrue(v2.endswith('23:55'), v2)


if __name__ == '__main__':
    unittest.main()
