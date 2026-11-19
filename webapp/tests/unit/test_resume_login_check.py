# -*- coding: utf-8 -*-
"""resume_upload 登录检测契约（2026-11-12 补传方案）。

场景：服务重启后 queue 已从 DB 恢复（_restore_unfinished_queue），但 worker 未启动。
resume_upload 必须在启动 worker 前同步检测账号登录态：
  - 未登录 → 返回 {status:'failed', login_required:True}，**不标 failed**（保持 pending 等登录），
    且**不启动** worker（uploading=False，upload_thread=None）。
  - 已登录 → 启动 worker 处理队列里已有的 pending。
"""
import threading

import pytest

from upload_manager import UploadManager


@pytest.fixture
def mgr(tmp_path):
    m = UploadManager(data_dir=str(tmp_path))
    m.uploading = False
    m.upload_thread = None
    m.prechecking = False
    m.paused = False
    m._stopped = False
    m._run_epoch = 0
    m.current_account = None
    m.queue = [{
        'account': '测试账号', 'id': 1, 'drama_name': '测试剧', 'version': 1,
        'status': 'pending', 'video_path': 'x.mp4',
    }]
    return m


class _FakeUploader:
    def __init__(self, logged_in):
        self._logged_in = logged_in

    def check_login(self):
        return self._logged_in


def test_not_logged_in_returns_login_required(mgr, monkeypatch):
    """未登录 → login_required=True，不标 failed，不启动 worker。"""
    monkeypatch.setattr('upload_manager.WeChatChannelsUploader',
                        lambda acct: _FakeUploader(logged_in=False))
    res = mgr.resume_upload()
    assert res['status'] == 'failed'
    assert res.get('login_required') is True
    assert '未登录' in res['error']
    # 未启动 worker、队列保持 pending（未被标 failed）
    assert mgr.uploading is False
    assert mgr.upload_thread is None
    assert mgr.queue[0]['status'] == 'pending'


def test_login_check_exception_returns_login_required(mgr, monkeypatch):
    """登录检测抛异常 → 也返回 login_required（不让 worker 启动）。"""
    class _Boom:
        def check_login(self):
            raise RuntimeError('浏览器启动失败')
    monkeypatch.setattr('upload_manager.WeChatChannelsUploader', lambda acct: _Boom())
    res = mgr.resume_upload()
    assert res['status'] == 'failed'
    assert res.get('login_required') is True
    assert mgr.uploading is False
    assert mgr.upload_thread is None


def test_logged_in_starts_worker(mgr, monkeypatch):
    """已登录 → 启动 worker 处理队列 pending，且要求跳过重复预检。"""
    started = threading.Event()
    called_with = []
    monkeypatch.setattr('upload_manager.WeChatChannelsUploader',
                        lambda acct: _FakeUploader(logged_in=True))
    monkeypatch.setattr(mgr, '_upload_worker',
                        lambda skip_precheck=False: (called_with.append(skip_precheck),
                                                     started.set()))
    res = mgr.resume_upload()
    assert res['status'] == 'success'
    assert mgr.uploading is True
    assert mgr.upload_thread is not None
    assert started.wait(3), 'worker 未启动'
    assert called_with == [True], 'resume 已校验登录，worker 不得再跑预检'


def test_empty_queue_returns_failed(mgr, monkeypatch):
    """队列为空 → failed，不触发登录检测。"""
    mgr.queue = []
    checked = []
    monkeypatch.setattr('upload_manager.WeChatChannelsUploader',
                        lambda acct: checked.append(acct) or _FakeUploader(logged_in=True))
    res = mgr.resume_upload()
    assert res['status'] == 'failed'
    assert '队列为空' in res['error']
    assert checked == []  # 空队列不检测登录
