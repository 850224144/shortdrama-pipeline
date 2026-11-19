# -*- coding: utf-8 -*-
"""上传前剧集 ID 解析契约单测（A3 混合语义回归）。

背景（BUG）：dramaid_client.resolve() 返回 4 元组
(drama_id, source, matched_name, error)，但 upload_manager._execute_upload
原先按 2 元组解包 `drama_id, drama_src = self._resolve_task_drama_id(task)`，
而 _resolve_task_drama_id 在 autolookup 分支原样透传 4 元组 → 每条上传任务
必抛 `ValueError: too many values to unpack (expected 2)`，批量上传 100% 失败。

本套用例把 _resolve_task_drama_id 的返回契约固化为「自描述 dict」：
  {drama_id, source, matched_name, error}
并断言 _execute_upload 在 4 元组 resolve() 下不再崩溃。不访问网络、不起浏览器：
客户端与上传器全部 mock。
"""
import os
import tempfile
import unittest
from unittest import mock


class _FakeDramaidClient:
    """替身：resolve() 按真实契约返回 4 元组。"""

    def __init__(self, result, raise_exc=None):
        self._result = result
        self._raise_exc = raise_exc

    def resolve(self, drama_name, drama_type='series'):
        if self._raise_exc:
            raise self._raise_exc
        return self._result


class TestResolveTaskDramaIdContract(unittest.TestCase):
    """_resolve_task_drama_id 必须稳定返回 dict（四键），任何分支不抛异常。"""

    def _make_manager(self):
        from upload_manager import UploadManager
        tmp = tempfile.mkdtemp(prefix='upload_contract_')
        return UploadManager(data_dir=tmp)

    def test_bound_branch_returns_dict(self):
        mgr = self._make_manager()
        task = {'drama_id': '18657', 'drama_id_source': 'manual',
                'drama_name': '测试剧'}
        res = mgr._resolve_task_drama_id(task)
        self.assertIsInstance(res, dict)
        self.assertEqual(res['drama_id'], '18657')
        self.assertEqual(res['source'], 'manual')
        self.assertEqual(res['error'], None)

    def test_autolookup_branch_4tuple_no_crash(self):
        """回归：resolve() 返回 4 元组时，必须归一为 dict，不得解包崩溃。"""
        mgr = self._make_manager()
        mgr.set_dramaid_client(_FakeDramaidClient(('12345', 'autolookup', '匹配剧名', None)))
        task = {'drama_id': None, 'drama_name': '测试剧'}
        # 旧实现此处 `drama_id, drama_src = ...` 会抛 too many values to unpack
        res = mgr._resolve_task_drama_id(task)
        self.assertIsInstance(res, dict)
        self.assertEqual(res['drama_id'], '12345')
        self.assertEqual(res['source'], 'autolookup')
        self.assertEqual(res['matched_name'], '匹配剧名')
        self.assertIsNone(res['error'])

    def test_autolookup_none_branch(self):
        mgr = self._make_manager()
        mgr.set_dramaid_client(_FakeDramaidClient((None, 'none', None, '未找到匹配剧集')))
        res = mgr._resolve_task_drama_id({'drama_id': None, 'drama_name': '测试剧'})
        self.assertIsInstance(res, dict)
        self.assertEqual(res['drama_id'], None)
        self.assertEqual(res['source'], 'none')
        self.assertEqual(res['error'], '未找到匹配剧集')

    def test_client_missing_branch(self):
        mgr = self._make_manager()
        res = mgr._resolve_task_drama_id({'drama_id': None, 'drama_name': '测试剧'})
        self.assertIsInstance(res, dict)
        self.assertEqual(res['source'], 'none')
        self.assertIsNone(res['drama_id'])

    def test_client_raises_branch(self):
        mgr = self._make_manager()
        c = _FakeDramaidClient(None, raise_exc=RuntimeError('网络失败'))
        mgr.set_dramaid_client(c)
        res = mgr._resolve_task_drama_id({'drama_id': None, 'drama_name': '测试剧'})
        self.assertIsInstance(res, dict)
        self.assertEqual(res['drama_id'], None)
        self.assertTrue(res['source'].startswith('error:'))
        self.assertIn('网络失败', res['error'])

    def test_caller_reads_dict_keys(self):
        """回归：调用方以 res['drama_id']/res['source'] 读取（替代 2 元组解包）。"""
        mgr = self._make_manager()
        mgr.set_dramaid_client(_FakeDramaidClient(('777', 'autolookup', '别名', None)))
        res = mgr._resolve_task_drama_id({'drama_id': None, 'drama_name': '测试剧'})
        drama_id = res['drama_id']      # 旧实现 = 解包崩溃点
        drama_src = res['source']
        self.assertEqual(drama_id, '777')
        self.assertEqual(drama_src, 'autolookup')


class TestExecuteUploadWith4TupleResolve(unittest.TestCase):
    """端到端回归：真实 _execute_upload 在 4 元组 resolve() 下不再抛异常。

    上传器（WeChatChannelsUploader）整体 mock：check_login=True、
    upload_to_draft 返回成功 dict；仅保留 _resolve_task_drama_id 走真实
    autolookup 分支（mock 客户端返回 4 元组），验证原先的解包崩溃已消除。
    """

    def _task(self, tmp):
        video = os.path.join(tmp, 'test_v1.mp4')
        open(video, 'w', encoding='utf-8').close()
        return {
            'id': 1,
            'drama_name': '测试剧',
            'version': 1,
            'video_path': video,
            'account': '测试账号',
            'title': None,
            'description': None,
            'topics': [],
            'cover_path': None,
            'status': 'uploading',
            'batch_id': None,
            'source_dir': tmp,
            'output_dir': tmp,
        }

    def test_execute_upload_4tuple_resolve_does_not_crash(self):
        from upload_manager import UploadManager
        tmp = tempfile.mkdtemp(prefix='upload_exec_')
        mgr = UploadManager(data_dir=tmp)
        mgr.set_dramaid_client(_FakeDramaidClient(('12345', 'autolookup', '匹配剧名', None)))
        task = self._task(tmp)

        class _FakeUploader:
            def set_log_hook(self, hook):
                self.hook = hook

            def check_login(self):
                return True

            def upload_to_draft(self, **kw):
                return {'status': 'success', 'draft_saved': True,
                        'cover': {'status': 'not_requested', 'cover_path': None},
                        'error': None}

        with mock.patch('upload_manager.WeChatChannelsUploader', return_value=_FakeUploader()):
            ok = mgr._execute_upload(task)

        self.assertTrue(ok, '上传应成功，不得因剧集 ID 解析抛异常')
        self.assertEqual(task['drama_id'], '12345')
        self.assertEqual(task['drama_id_source'], 'autolookup')


class TestResolveNetworkRetry(unittest.TestCase):
    """网络/上游瞬时失败应重试（能查到的剧集 ID 必须填进去），真查不到不重试。

    背景（BUG）：lookup_best 把网络失败静默吞成 None，resolve 记为 'none'，
    与「真查不到」无法区分 → 能查到的 ID 被当查不到跳过填链接（2026-11-11
    深夜批次全部 drama_id=None 即此因）。修复后网络失败记为 'error' 并重试。
    """

    def _make_manager(self):
        from upload_manager import UploadManager
        tmp = tempfile.mkdtemp(prefix='upload_retry_')
        return UploadManager(data_dir=tmp)

    def test_network_error_then_success_retries(self):
        """第一次网络失败、第二次成功 → 重试后拿到 ID（能查到就填进去）。"""
        mgr = self._make_manager()
        calls = {'n': 0}

        class _Flaky:
            def resolve(self, name, drama_type='series'):
                calls['n'] += 1
                if calls['n'] == 1:
                    raise RuntimeError('网络失败')
                return ('12345', 'autolookup', '匹配剧名', None)

        mgr.set_dramaid_client(_Flaky())
        with mock.patch('upload_manager.time.sleep'):
            res = mgr._resolve_task_drama_id({'drama_id': None, 'drama_name': '测试剧'})
        self.assertEqual(res['drama_id'], '12345')
        self.assertEqual(res['source'], 'autolookup')
        self.assertGreaterEqual(calls['n'], 2, '网络失败后必须重试')

    def test_all_errors_exhaust_returns_error(self):
        """重试耗尽仍失败 → 返回 error（而非静默 none）。"""
        mgr = self._make_manager()
        c = _FakeDramaidClient(None, raise_exc=RuntimeError('网络失败'))
        mgr.set_dramaid_client(c)
        with mock.patch('upload_manager.time.sleep'):
            res = mgr._resolve_task_drama_id({'drama_id': None, 'drama_name': '测试剧'})
        self.assertTrue(res['source'].startswith('error:'), res)
        self.assertIsNone(res['drama_id'])

    def test_none_does_not_retry(self):
        """真查不到（source='none'）不重试——重试无意义，快速放行。"""
        mgr = self._make_manager()
        calls = {'n': 0}

        class _Never:
            def resolve(self, name, drama_type='series'):
                calls['n'] += 1
                return (None, 'none', None, '未找到匹配剧集')

        mgr.set_dramaid_client(_Never())
        res = mgr._resolve_task_drama_id({'drama_id': None, 'drama_name': '测试剧'})
        self.assertEqual(res['source'], 'none')
        self.assertEqual(calls['n'], 1, '真查不到不得重试')

    def test_error_branch_returns_dict_no_crash(self):
        """error 分支仍返回 dict 四键，不抛异常（上传照常继续）。"""
        mgr = self._make_manager()
        c = _FakeDramaidClient(None, raise_exc=RuntimeError('网络失败'))
        mgr.set_dramaid_client(c)
        with mock.patch('upload_manager.time.sleep'):
            res = mgr._resolve_task_drama_id({'drama_id': None, 'drama_name': '测试剧'})
        self.assertIsInstance(res, dict)
        for k in ('drama_id', 'source', 'matched_name', 'error'):
            self.assertIn(k, res)


class TestExecuteUploadNoDramaIdForcesDraft(unittest.TestCase):
    """查不到剧集 ID → 强制保存草稿（不发表/不定时）回归。

    需求（用户，2026-11-19）：上传后查不到剧集 ID 就不要发表、不要选择
    定时发布，直接保存到草稿箱——无法填「链接 → 视频号剧集」就不发布，
    避免发出后挂不上剧集链接。scheduled=True 是定时发表，False 是保存草稿。
    """

    def _task(self, tmp):
        video = os.path.join(tmp, 'test_v1.mp4')
        open(video, 'w', encoding='utf-8').close()
        return {
            'id': 1, 'drama_name': '测试剧', 'version': 1, 'video_path': video,
            'account': '测试账号', 'title': None, 'description': None,
            'topics': [], 'cover_path': None, 'status': 'uploading',
            'batch_id': None, 'source_dir': tmp, 'output_dir': tmp,
        }

    def _run(self, resolve_result, scheduled_mode):
        from upload_manager import UploadManager
        tmp = tempfile.mkdtemp(prefix='upload_draft_')
        mgr = UploadManager(data_dir=tmp)
        mgr.set_dramaid_client(_FakeDramaidClient(resolve_result))
        mgr._scheduled_publish = scheduled_mode
        task = self._task(tmp)
        calls = {}

        class _FakeUploader:
            def set_log_hook(self, hook):
                self.hook = hook

            def check_login(self):
                return True

            def upload_to_draft(self, **kw):
                calls.update(kw)
                return {'status': 'success', 'draft_saved': True,
                        'cover': {'status': 'not_requested', 'cover_path': None},
                        'error': None}

        with mock.patch('upload_manager.WeChatChannelsUploader', return_value=_FakeUploader()):
            ok = mgr._execute_upload(task)
        self.assertTrue(ok)
        return calls

    def test_no_drama_id_forces_draft_even_when_scheduled(self):
        """定时发表模式下，查不到剧集 ID → 强制 scheduled=False（存草稿）。"""
        calls = self._run((None, 'none', None, '未找到匹配剧集'), scheduled_mode=True)
        self.assertFalse(calls['scheduled'], '查不到剧集 ID 不得定时发表')
        self.assertEqual(calls['drama_id'], None)

    def test_no_drama_id_forces_draft_when_not_scheduled(self):
        """非定时模式，查不到剧集 ID → 仍存草稿。"""
        calls = self._run((None, 'none', None, '未找到匹配剧集'), scheduled_mode=False)
        self.assertFalse(calls['scheduled'])

    def test_error_resolve_also_forces_draft(self):
        """网络失败（error 分支，drama_id=None）→ 也强制存草稿，不发表。"""
        calls = self._run((None, 'error', None, '共享资料库查询失败'), scheduled_mode=True)
        self.assertFalse(calls['scheduled'], 'resolve error 也不得发表')

    def test_has_drama_id_keeps_scheduled(self):
        """有剧集 ID → 保持原定时发表设置（行为不变）。"""
        calls = self._run(('12345', 'autolookup', '匹配剧名', None), scheduled_mode=True)
        self.assertTrue(calls['scheduled'], '有剧集 ID 应照常定时发表')
        self.assertEqual(calls['drama_id'], '12345')


if __name__ == '__main__':
    unittest.main()
