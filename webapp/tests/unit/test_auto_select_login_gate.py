# -*- coding: utf-8 -*-
"""自动选剧单账号运行单测（2026-11-10 分离方案）。

用户决策：自动选剧的下载/切片不需要微信登录，只有上传前才查登录。
目标账号 = 调用方指定，否则默认第一个账号（仅作成品归属标记）。
上传时按账号隔离（_precheck_logins），登哪个账号传哪个账号的批次。
"""
import unittest
from unittest import mock

from automatic_selection import AutomaticSelection, ACCOUNTS


def _make_selection(account=None):
    logger = mock.MagicMock()
    sel = AutomaticSelection(
        processor=mock.MagicMock(),
        cut_manager=mock.MagicMock(),
        config=mock.MagicMock(),
        logger=logger,
    )
    sel._pipeline_held = False
    sel._target_account = account
    sel.cancel_event = mock.MagicMock()
    sel.cancel_event.is_set.return_value = False
    return sel, logger


class TestAutoSelectSingleAccount(unittest.TestCase):
    def _run_with_download_fail(self, sel, account_arg):
        """下载返回失败：验证下载照跑（不因登录拦截）+ 用指定/默认账号。"""
        ra = mock.Mock(return_value=('item1', 'entry1'))
        with mock.patch('automatic_selection.library') as lib, \
                mock.patch.object(sel, '_download', return_value=(None, '模拟下载失败')), \
                mock.patch.object(sel, '_record_attempt', ra), \
                mock.patch.object(sel, '_content_fingerprint_exists', return_value=False), \
                mock.patch.object(sel, '_remove_auto_queue_items'), \
                mock.patch.object(sel, '_min_free_gb', return_value=500.0):
            sel._run('run123', 1, [{'title': 'X', 'title_key': 'x'}])
            # 关键断言 1：从未标"无已登录账号"失败（门控已删）
            for call in lib.update_auto_run.call_args_list:
                err = call[1].get('error', '') or ''
                self.assertNotIn('无已登录', err)
            # 关键断言 2：下载确实被尝试（_download 被调用），不是被登录拦住
            sel._download.assert_called_once()
            # 关键断言 3：尝试记录使用目标账号
            ra.assert_called_once()
            self.assertEqual(ra.call_args.args[2], account_arg)
            return lib

    def test_no_specified_account_defaults_to_first(self):
        sel, _logger = _make_selection(account=None)
        self._run_with_download_fail(sel, ACCOUNTS[0])

    def test_specified_account_is_used(self):
        sel, _logger = _make_selection(account='福桃说剧')
        self._run_with_download_fail(sel, '福桃说剧')


class TestStartAccountValidation(unittest.TestCase):
    """start() 入口对 account 的校验（reviewer 逻辑错误修复）。"""

    def _make(self):
        sel, logger = _make_selection(account=None)
        sel._start_lock = mock.MagicMock()
        sel._start_lock.acquire.return_value = False  # 锁被占→校验后即 409，不深走
        return sel, logger

    def test_non_string_account_does_not_crash(self):
        # 原 bug：int/dict/list 直接拼接 '目标账号=' + account 抛 TypeError
        sel, logger = self._make()
        r, code = sel.start(count=2, account=123)
        self.assertEqual(code, 409)
        logger.log.assert_any_call('default', 'WARNING', mock.ANY)

    def test_unknown_account_falls_back(self):
        sel, logger = self._make()
        r, code = sel.start(count=2, account='不存在的账号')
        self.assertEqual(code, 409)
        logger.log.assert_any_call('default', 'WARNING', mock.ANY)

    def test_valid_account_passes_validation(self):
        # 合法账号不记录"回退默认账号"警告
        sel, logger = self._make()
        r, code = sel.start(count=2, account=ACCOUNTS[1])
        self.assertEqual(code, 409)
        for call in logger.log.call_args_list:
            self.assertNotIn('回退默认账号', str(call))


if __name__ == '__main__':
    unittest.main()
