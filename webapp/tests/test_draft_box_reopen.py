# -*- coding: utf-8 -*-
"""草稿箱多次打开回归测试。

覆盖 2026-10-28 用户报告「点击一次后再打开就打不开了」：
open_draft_box 用 `account in self.draft_pages` 短路返回，但用户手动点 X 关掉
Chrome 后，draft_pages 里残留的是已断连的死 page 对象——第二次点击返回
「已打开」而实际无窗口。修复：_page_is_alive 用 states.is_alive 探测，
死页面自动清理并重开；get_draft_status 过滤死页面让前端按钮复位。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from upload_manager import UploadManager


class FakeStates:
    def __init__(self, alive):
        self._alive = alive

    @property
    def is_alive(self):
        return self._alive


class FakePage:
    def __init__(self, alive=True):
        self.states = FakeStates(alive)
        self.quit_called = False

    def get(self, url):
        self.url = url

    def quit(self):
        self.quit_called = True


class DraftBoxReopenTest(unittest.TestCase):
    def setUp(self):
        self.mgr = UploadManager()
        self.mgr.draft_pages = {}
        self.mgr.uploading = False
        self.mgr.current_account = None

    def test_alive_page_short_circuits(self):
        """存活页面：直接返回已打开，不重开。"""
        page = FakePage(alive=True)
        self.mgr.draft_pages['福桃说剧'] = page
        with mock.patch.object(self.mgr, '_open_draft_box_browser') as opener:
            res = self.mgr.open_draft_box('福桃说剧')
        self.assertEqual(res['status'], 'success')
        opener.assert_not_called()

    def test_dead_page_reopens(self):
        """死页面（手动关窗残留）：自动清理并重开新浏览器。"""
        dead = FakePage(alive=False)
        self.mgr.draft_pages['福桃说剧'] = dead
        fresh = FakePage(alive=True)
        with mock.patch.object(self.mgr, '_open_draft_box_browser',
                               return_value=fresh) as opener:
            res = self.mgr.open_draft_box('福桃说剧')
        self.assertEqual(res['status'], 'success')
        opener.assert_called_once_with('福桃说剧')
        self.assertIs(self.mgr.draft_pages['福桃说剧'], fresh)

    def test_dead_page_but_uploading_same_account_blocked(self):
        """死页面 + 同账号正在上传：先清死页面，再被上传互斥拦截。"""
        self.mgr.draft_pages['福桃说剧'] = FakePage(alive=False)
        self.mgr.uploading = True
        self.mgr.current_account = '福桃说剧'
        with mock.patch.object(self.mgr, '_open_draft_box_browser') as opener:
            res = self.mgr.open_draft_box('福桃说剧')
        self.assertEqual(res['status'], 'failed')
        self.assertIn('正在上传', res['error'])
        opener.assert_not_called()

    def test_get_draft_status_filters_dead(self):
        """状态查询过滤死页面并清理，前端按钮可复位。"""
        self.mgr.draft_pages = {
            '福桃说剧': FakePage(alive=True),
            '小桃追剧中': FakePage(alive=False),
        }
        status = self.mgr.get_draft_status()
        self.assertEqual(status['open_accounts'], ['福桃说剧'])
        self.assertNotIn('小桃追剧中', self.mgr.draft_pages)

    def test_close_dead_page_does_not_raise(self):
        """关闭时页面 quit 抛错（已断连）也返回成功并清字典。"""
        dead = FakePage(alive=False)
        dead.quit = mock.Mock(side_effect=Exception('PageDisconnectedError'))
        self.mgr.draft_pages['福桃说剧'] = dead
        res = self.mgr.close_draft_box('福桃说剧')
        self.assertEqual(res['status'], 'success')
        self.assertNotIn('福桃说剧', self.mgr.draft_pages)

    def test_open_retries_on_profile_lock(self):
        """profile 锁竞态：前 2 次失败重试，第 3 次成功。"""
        fresh = FakePage(alive=True)
        with mock.patch('upload_manager.WeChatChannelsUploader') as wcu, \
             mock.patch('upload_manager.ChromiumPage') as cp:
            wcu.return_value.user_data_path = 'C:/fake/profile'
            cp.side_effect = [Exception('profile in use'),
                              Exception('profile in use'), fresh]
            page = self.mgr._open_draft_box_browser('福桃说剧')
        self.assertIs(page, fresh)
        self.assertEqual(cp.call_count, 3)

    def test_open_all_retries_exhausted_raises(self):
        """5 次全失败：抛出最后错误，open_draft_box 返回 failed。"""
        with mock.patch('upload_manager.WeChatChannelsUploader') as wcu, \
             mock.patch('upload_manager.ChromiumPage') as cp, \
             mock.patch.object(self.mgr, '_log') as log:
            wcu.return_value.user_data_path = 'C:/fake/profile'
            cp.side_effect = Exception('_dl_mgr race')
            res = self.mgr.open_draft_box('福桃说剧')
        self.assertEqual(res['status'], 'failed')
        self.assertIn('_dl_mgr', res['error'])
        self.assertEqual(cp.call_count, 5)

    def test_page_is_alive_none(self):
        self.assertFalse(self.mgr._page_is_alive(None))

    def test_page_is_alive_catches_exception(self):
        class Boom:
            @property
            def states(self):
                raise RuntimeError('boom')
        self.assertFalse(self.mgr._page_is_alive(Boom()))


if __name__ == '__main__':
    unittest.main(verbosity=2)
