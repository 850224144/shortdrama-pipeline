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


if __name__ == '__main__':
    unittest.main()
