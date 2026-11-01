# -*- coding: utf-8 -*-
"""A5 词索引防漂移：LLM 用句索引（seg_idx）而非秒数，杜绝编造时间码。

验收：
- LLM 返回 seg_idx → 窗口锚定该句（start/end 从转写段映射）。
- 无 seg_idx（旧格式/旧缓存）→ 回退秒数 start/end。
- seg_idx 越界 → 丢弃（不映射、不编秒数）。
- 传给 LLM 的台词行带 [句号] 索引。
"""

import json
import os
import unittest
from unittest import mock

sys_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
sys.path.insert(0, sys_path)

from processor import PipelineProcessor


def _raw():
    """3 句转写：0-5s、5-12s、12-20s。"""
    return [
        {'start': 0.0, 'end': 5.0, 'text': '第一句铺垫'},
        {'start': 5.0, 'end': 12.0, 'text': '第二句冲突爆发'},
        {'start': 12.0, 'end': 20.0, 'text': '第三句反转揭示'},
    ]


class WordIndexTests(unittest.TestCase):
    def setUp(self):
        self.proc = PipelineProcessor(mock.MagicMock(), mock.MagicMock())
        self.proc.logger = mock.MagicMock()
        self.ai = mock.MagicMock()

    def _score(self, llm_json, raw=None, ep_dur=30.0):
        self.ai.chat_json.return_value = llm_json
        return self.proc._score_episode_llm(self.ai, '剧X', 1,
                                            raw or _raw(), ep_dur)

    def test_seg_idx_maps_to_sentence(self):
        # LLM 给句号 1（5-12s 冲突句），窗口锚定该句并扩到 12s
        data = {'windows': [{'score': 9, 'seg_idx': 1,
                             'reason': '冲突爆发', 'hook_type': '高潮'}]}
        cands = self._score(json.dumps(data))
        self.assertEqual(len(cands), 1)
        c = cands[0]
        # 该句 5-12s（7s），扩到 12s：前 5s（0-5）+ 句（5-12）→ 0-12s
        self.assertEqual(c['start'], 0.0)
        self.assertEqual(c['end'], 12.0)
        self.assertEqual(c['score'], 9)
        self.assertEqual(c['hook_type'], '高潮')

    def test_old_format_fallback_seconds(self):
        data = {'windows': [{'score': 8, 'start': 6.0, 'end': 20.0,
                             'reason': '对峙', 'hook_type': '打脸'}]}
        cands = self._score(json.dumps(data))
        self.assertEqual(len(cands), 1)
        # 14s ≥ MIN_WIN → 不扩展，秒数原样
        self.assertEqual(cands[0]['start'], 6.0)
        self.assertEqual(cands[0]['end'], 20.0)

    def test_seg_idx_out_of_range_rejected(self):
        data = {'windows': [{'score': 9, 'seg_idx': 99,
                             'reason': '编句号', 'hook_type': '高潮'}]}
        cands = self._score(json.dumps(data))
        self.assertEqual(cands, [], '越界 seg_idx 应丢弃，不编秒数')

    def test_seg_idx_negative_rejected(self):
        data = {'windows': [{'score': 9, 'seg_idx': -1,
                             'reason': '负句号', 'hook_type': '高潮'}]}
        self.assertEqual(self._score(json.dumps(data)), [])

    def test_invalid_json_empty(self):
        self.assertEqual(self._score('不是 JSON'), [])

    def test_no_windows_empty(self):
        self.assertEqual(self._score(json.dumps({'windows': []})), [])

    def test_prompt_contains_index(self):
        self._score(json.dumps({'windows': []}))
        prompt = self.ai.chat_json.call_args[0][0]
        self.assertIn('[0]', prompt)
        self.assertIn('[1]', prompt)
        self.assertIn('[2]', prompt)

    def test_prompt_asks_seg_idx(self):
        self._score(json.dumps({'windows': []}))
        prompt = self.ai.chat_json.call_args[0][0]
        self.assertIn('seg_idx', prompt)
        self.assertIn('句号', prompt)

    def test_window_stays_in_episode(self):
        # 末句（12-20s）+ 扩展不超 ep_dur
        data = {'windows': [{'score': 7, 'seg_idx': 2,
                             'reason': '反转', 'hook_type': '反转'}]}
        cands = self._score(json.dumps(data), ep_dur=20.0)
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertLessEqual(c['end'], 20.5)
        self.assertGreaterEqual(c['end'] - c['start'], 8.0)


if __name__ == '__main__':
    unittest.main()
