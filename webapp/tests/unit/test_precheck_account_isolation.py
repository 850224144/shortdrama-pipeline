# -*- coding: utf-8 -*-
"""账号级登录预检隔离单测（2026-11-10 重新设计：不再因一个账号掉线拖垮整批）。

根因：原 _precheck_logins 任一账号未登录就把整批标失败；且自动选剧按
ACCOUNTS[slot] 绑两个账号，一次只能登录一个时必然有一半任务失败。
修复：只失败掉线账号的任务并结算其批次，登录正常的账号照常上传。
"""
import unittest
import tempfile
import os
from unittest import mock

import upload_manager as um
from upload_manager import UploadManager


def _make_manager():
    """隔离临时目录的 UploadManager，避免污染生产 data/upload.db。"""
    tmp = tempfile.mkdtemp(prefix='sd_precheck_')
    return UploadManager(data_dir=tmp), tmp


class _FakeUploader:
    """替身：按账号返回预设登录态。"""
    def __init__(self, account):
        self.account = account

    def check_login(self):
        return LOGIN_STATE.get(self.account, False)


# 每个测试用例设置：account -> logged_in?
LOGIN_STATE = {}


class TestPrecheckAccountIsolation(unittest.TestCase):
    def _run_precheck(self, tasks, login_state):
        mgr, tmp = _make_manager()
        self.addCleanup(lambda: None)
        LOGIN_STATE.clear()
        LOGIN_STATE.update(login_state)
        # 塞入队列（复制，避免共享）
        mgr.queue = [dict(t) for t in tasks]
        with mock.patch.object(um, 'WeChatChannelsUploader', _FakeUploader), \
                mock.patch.object(mgr, '_finalize_batches') as _finalize:
            result = mgr._precheck_logins()
            return mgr, result, _finalize

    def test_partial_login_fails_only_bad_account(self):
        # A 登录、B 掉线：B 的任务失败并移出队列，A 的任务保留，返回 True
        tasks = [
            {'id': 'a1', 'account': '小桃追剧中', 'batch_id': 'bA', 'status': 'pending'},
            {'id': 'b1', 'account': '福桃说剧', 'batch_id': 'bB', 'status': 'pending'},
        ]
        mgr, result, finalize = self._run_precheck(
            tasks, {'小桃追剧中': True, '福桃说剧': False})
        self.assertTrue(result, '部分账号登录时预检应放行（继续上传登录账号任务）')
        # B 的任务被标失败
        b_failed = [t for t in mgr.failed if t['id'] == 'b1']
        self.assertEqual(len(b_failed), 1)
        self.assertIn('未登录', b_failed[0]['error_message'])
        # A 的任务仍在队列（未被拖垮）
        self.assertIn('a1', [t['id'] for t in mgr.queue])
        self.assertNotIn('b1', [t['id'] for t in mgr.queue])
        # B 的批次被结算（auto_cleanup=False，登录失败≠上传完成）
        finalize.assert_called_once_with({'bB'}, auto_cleanup=False)

    def test_all_logged_out_returns_false(self):
        tasks = [
            {'id': 'a1', 'account': '小桃追剧中', 'batch_id': 'bA', 'status': 'pending'},
            {'id': 'b1', 'account': '福桃说剧', 'batch_id': 'bB', 'status': 'pending'},
        ]
        mgr, result, finalize = self._run_precheck(
            tasks, {'小桃追剧中': False, '福桃说剧': False})
        self.assertFalse(result, '全部账号未登录应返回 False（不启动上传）')
        self.assertEqual(len(mgr.failed), 2)
        self.assertEqual(mgr.queue, [])

    def test_all_logged_in_returns_true_and_touches_nothing(self):
        tasks = [
            {'id': 'a1', 'account': '小桃追剧中', 'batch_id': 'bA', 'status': 'pending'},
            {'id': 'b1', 'account': '福桃说剧', 'batch_id': 'bB', 'status': 'pending'},
        ]
        mgr, result, finalize = self._run_precheck(
            tasks, {'小桃追剧中': True, '福桃说剧': True})
        self.assertTrue(result)
        self.assertEqual(len(mgr.failed), 0)
        self.assertEqual(len(mgr.queue), 2)
        finalize.assert_not_called()


if __name__ == '__main__':
    unittest.main()
