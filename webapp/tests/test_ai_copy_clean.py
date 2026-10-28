# -*- coding: utf-8 -*-
"""AI 文案生成器回归测试：时间码前缀剥离 + ASR 碎片标题拒绝。

覆盖 2026-10-28 根因：ai_copy_generator 把带 [5.8-7.4] 时间码的 ASR 台词
喂给模型，模型整段回显成标题，_clean_title 删标点后 "58474她突然晕倒"
恰好落在 6-16 字合规区间，脏标题一路进 edl_config。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ai_copy_generator import AICopyGenerator


class AICopyCleanTitleTest(unittest.TestCase):
    def setUp(self):
        self.gen = AICopyGenerator()

    def test_strip_quote_timecode_basic(self):
        self.assertEqual(self.gen._strip_quote_timecode('[5.8-7.4]她突然晕倒'),
                         '她突然晕倒')

    def test_strip_quote_timecode_variants(self):
        for raw, want in [
            ('[5.8-7.4] 她突然晕倒', '她突然晕倒'),
            ('[32.3-33.3]几分钟前突然晕倒', '几分钟前突然晕倒'),
            ('【9.7-10.9】你不会真以为', '你不会真以为'),
            ('[0.0-1.2]你真去了', '你真去了'),
            ('无时间码台词', '无时间码台词'),
            ('', ''),
        ]:
            self.assertEqual(self.gen._strip_quote_timecode(raw), want, raw)

    def test_clean_title_removes_punct(self):
        self.assertEqual(self.gen._clean_title('[5.8-7.4]她突然晕倒'),
                         '5874她突然晕倒')

    def test_looks_like_fragment(self):
        self.assertTrue(self.gen._looks_like_fragment('58474她突然晕倒'))
        self.assertTrue(self.gen._looks_like_fragment('她突然晕倒5'))
        self.assertFalse(self.gen._looks_like_fragment('嫁进豪门我仍在卖馒头'))

    def test_build_prompt_strips_timecode_from_quotes(self):
        prompt = self.gen._build_prompt(
            '测试剧', 1, '逆袭',
            plot_summary='本剧高光台词参考：[5.8-7.4]她突然晕倒',
            drama_genre='短剧',
            quotes=['[5.8-7.4]她突然晕倒', '[9.7-10.9]你不会真以为'])
        # quotes_block 中不再出现时间码
        self.assertNotIn('[5.8-7.4]', prompt)
        self.assertNotIn('[9.7-10.9]', prompt)
        self.assertIn('她突然晕倒', prompt)
        self.assertIn('你不会真以为', prompt)

    def test_validate_result_rejects_fragment_title(self):
        result = {'title': '[5.8-7.4]她突然晕倒',
                  'description': '这是一个足够长的描述，用来通过验证逻辑的检查。',
                  'hashtags': ['#短剧', '#逆袭', '#推荐']}
        # 走模板兜底（_generate_title 用稳定摘要），标题不含数字
        out = self.gen._validate_result(result, '馒头姑娘旺翻沈家', '逆袭')
        self.assertFalse(self.gen._looks_like_fragment(out['title']))
        self.assertTrue(6 <= len(out['title']) <= 16)


if __name__ == '__main__':
    unittest.main(verbosity=2)
