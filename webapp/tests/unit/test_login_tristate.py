# -*- coding: utf-8 -*-
"""登录校验三态（logged_in / logged_out / busy）回归。

背景：check_login 以布尔返回“未登录/测不了”，预检据此把 profile 被占用
（用户正在扫码登录）的账号任务标 failed，重新登录后也无法恢复。现在拆成
login_state()：busy 只代表“测不了”，预检必须保持 pending、不结算批次。
"""
import os
import tempfile
from unittest import mock

import upload_manager as um
from upload_manager import UploadManager
from wechat_uploader import WeChatChannelsUploader


LOGIN_STATE = {}


class _FakeUploader:
    """替身：按账号返回三态登录结果。"""
    def __init__(self, account):
        self.account = account

    def login_state(self):
        return LOGIN_STATE.get(self.account, 'logged_out')


def _make_manager():
    return UploadManager(data_dir=tempfile.mkdtemp(prefix='sd_login3_'))


def test_profile_locked_returns_busy_not_logged_out():
    """锁被占（扫码登录/别的进程上传中）→ busy；布尔接口不得报“未登录成功”。"""
    uploader = WeChatChannelsUploader('测试账号')
    uploader.user_data_path = tempfile.mkdtemp(prefix='sd_profile_')
    assert os.path.isdir(uploader.user_data_path)
    with mock.patch.object(uploader, '_acquire_profile_lock', return_value=None):
        assert uploader.login_state() == 'busy'
        assert uploader.check_login() is False


def test_precheck_busy_keeps_queue_pending():
    """有账号 busy → 返回 'blocked'，任务不标失败、不结算批次、保留 pending。"""
    mgr = _make_manager()
    mgr.queue = [{'id': 'a1', 'account': '小桃追剧中', 'batch_id': 'bA',
                  'status': 'pending'}]
    LOGIN_STATE.clear()
    LOGIN_STATE['小桃追剧中'] = 'busy'
    with mock.patch.object(um, 'WeChatChannelsUploader', _FakeUploader), \
            mock.patch.object(mgr, '_finalize_batches') as finalize:
        result = mgr._precheck_logins()
    assert result == 'blocked'
    assert mgr.queue[0]['status'] == 'pending'
    assert mgr.failed == []
    finalize.assert_not_called()


def test_login_expired_error_is_not_retried():
    """登录失效重试只会再开浏览器失败一次 → 直接转人工。"""
    mgr = _make_manager()
    task = {'drama_name': '测试剧', 'version': 1, 'retry_count': 0,
            'error_message': '登录状态已失效'}
    assert '重新登录' in mgr._retry_block_reason(task, timed_out=False)


def test_worker_skips_precheck_after_resume_verified():
    """resume 已逐账号校验 → worker 不得再跑一遍预检（每账号白开一次浏览器）。"""
    mgr = _make_manager()
    mgr.queue = []
    with mock.patch.object(mgr, '_precheck_logins') as pre:
        mgr._upload_worker(skip_precheck=True)
    pre.assert_not_called()
