"""交叉轮询账号归属的测试。

用户指令：切片时短剧归属按交叉顺序（一个剧小桃、下一个剧福桃、循环），
不再按剧情分类。本文件验证 assign_account / get_watermark_name /
get_output_subdir 三者一致且严格交替。
"""
import unittest
from unittest import mock

from processor import PipelineProcessor, CONFIG


class TestCrossAssignAccount(unittest.TestCase):
    """验证 assign_account 纯交叉轮询。"""

    def setUp(self):
        # 真实实例 + MagicMock 依赖，与 test_content_dedup 一致。
        self.proc = PipelineProcessor(mock.MagicMock(), mock.MagicMock())

    def test_cross_round_robin(self):
        """submit_order 从0开始严格交替：偶小桃、奇福桃。"""
        expected = {0: '小桃追剧中', 1: '福桃说剧',
                    2: '小桃追剧中', 3: '福桃说剧',
                    4: '小桃追剧中', 5: '福桃说剧'}
        for order, acct in expected.items():
            got = self.proc.assign_account('任意剧名', submit_order=order)
            self.assertEqual(got, acct,
                             f'submit_order={order} 应归 {acct}，实际 {got}')

    def test_cross_ignores_drama_name(self):
        """归属只由序号决定，与剧名/题材无关（不再按剧情分类）。"""
        names = ['萌宝三岁找爹', '真千金认亲', '战神兵王', '婆婆孝道',
                 '豪门总裁', '年代种田', '复仇重生']
        for order in (0, 1, 2, 3):
            acct = self.proc.assign_account(names[order % len(names)],
                                            submit_order=order)
            exp = '小桃追剧中' if order % 2 == 0 else '福桃说剧'
            self.assertEqual(acct, exp,
                             f'剧名 {names[order % len(names)]} 序号{order} '
                             f'应归 {exp}，实际 {acct}')

    def test_cross_no_order_uses_completed_count(self):
        """无序号时按已完成剧数继续交叉（保持轮询连贯）。"""
        with mock.patch.object(self.proc, 'get_submit_order',
                               return_value=6) as m:
            self.assertEqual(self.proc.assign_account('剧X'), '小桃追剧中')
        with mock.patch.object(self.proc, 'get_submit_order',
                               return_value=7) as m:
            self.assertEqual(self.proc.assign_account('剧X'), '福桃说剧')

    def test_watermark_matches_account(self):
        """水印名 = 账号名 = 输出目录名，三者统一。"""
        # 新交叉轮询产出的账号直接就是水印/目录名
        self.assertEqual(self.proc.get_watermark_name('小桃追剧中'), '小桃追剧中')
        self.assertEqual(self.proc.get_watermark_name('福桃说剧'), '福桃说剧')
        # 旧逻辑名兼容（历史数据/调用方可能仍传福桃追剧）
        self.assertEqual(self.proc.get_watermark_name('福桃追剧'), '福桃说剧')

    def test_output_subdir_uses_actual_account(self):
        """输出目录优先取实际分配账号，默认回退 upload_account。"""
        self.assertEqual(
            self.proc.get_output_subdir('剧X', account='小桃追剧中'),
            '小桃追剧中')
        self.assertEqual(
            self.proc.get_output_subdir('剧X', account='福桃说剧'),
            '福桃说剧')
        # 未传 account 时回退全局配置
        with mock.patch.dict(CONFIG, {'upload_account': '福桃说剧'}):
            self.assertEqual(self.proc.get_output_subdir('剧X'), '福桃说剧')


if __name__ == '__main__':
    unittest.main()
