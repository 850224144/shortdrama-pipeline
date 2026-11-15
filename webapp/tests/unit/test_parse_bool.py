# -*- coding: utf-8 -*-
"""_parse_bool 宽松布尔解析单测（api/upload.py，reviewer 建议 3）。"""
import unittest

from api.upload import _parse_bool


class TestParseBool(unittest.TestCase):
    def test_bool_passthrough(self):
        self.assertTrue(_parse_bool(True))
        self.assertFalse(_parse_bool(False))

    def test_string_true(self):
        for s in ('true', 'True', 'TRUE', '1', 'yes', 'YES'):
            self.assertTrue(_parse_bool(s), f'{s!r} 应为 True')

    def test_string_false(self):
        # 原 bug：bool('false') == True；现 'false'/'0'/'no'/'' 应为 False
        for s in ('false', 'False', '0', 'no', '', '2', 'garbage'):
            self.assertFalse(_parse_bool(s), f'{s!r} 应为 False')

    def test_none_default(self):
        self.assertFalse(_parse_bool(None))
        self.assertTrue(_parse_bool(None, default=True))

    def test_int(self):
        self.assertTrue(_parse_bool(1))
        self.assertFalse(_parse_bool(0))

    def test_non_scalar_no_crash(self):
        # dict/list 等非标量不崩溃，回退默认
        self.assertFalse(_parse_bool({}))
        self.assertFalse(_parse_bool([]))


if __name__ == '__main__':
    unittest.main()
