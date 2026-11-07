# -*- coding: utf-8 -*-
"""A3 Reviewer 审稿-重试闭环：5 维打分 <0.65 → 反馈 Director 重试。

验收：
- _review_arc_plan：正常返回 (score, feedback)；无效 JSON → (None, None)；
  score clamp 到 [0,1]。
- _analyze_drama_arc：审稿通过（≥0.65）→ 不重试；未过 → 带 feedback 重试
  （Director 二次生成）；审稿失败（None）→ 保守放行接受首版。
- _generate_versions_via_llm：feedback 非空追加进 prompt。
"""

import json
import os
import tempfile
import unittest
from unittest import mock

sys_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
sys.path.insert(0, sys_path)

from processor import PipelineProcessor


def _eps_hl(tmp):
    """4 集高光摘要。"""
    eps = []
    for i, (ep, sc) in enumerate([(1, 9), (2, 8), (3, 7), (4, 6)]):
        path = os.path.join(tmp, f'剧X_第{ep}集.mp4')
        with open(path, 'wb') as f:
            f.write(b'x')
        eps.append({
            'episode': ep, 'path': path, 'dur': 100.0,
            'cands': [{'score': sc, 'hook_type': '高潮',
                       'reason': f'第{ep}集冲突', 'start': 1.0,
                       'dur': 12.0, 'src': 'llm'}],
        })
    return eps


def _director_json():
    return {'versions': [
        {'opening_ep': 1, 'angle': '高潮', 'order_type': 'reverse'},
        {'opening_ep': 2, 'angle': '打脸', 'order_type': 'flashback'},
        {'opening_ep': 3, 'angle': '真相', 'order_type': 'forward'},
    ]}


class ReviewPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='review_')
        self.addCleanup(
            lambda: __import__('shutil').rmtree(self.tmp, ignore_errors=True))
        self.proc = PipelineProcessor(mock.MagicMock(), mock.MagicMock())
        self.proc.logger = mock.MagicMock()
        self.ai = mock.MagicMock()

    def _review(self, llm_json):
        self.ai.chat_json.return_value = llm_json
        return self.proc._review_arc_plan(
            self.ai, _director_json()['versions'], _eps_hl(self.tmp), '剧X')

    def test_normal_score_feedback(self):
        s, fb = self._review(json.dumps({'score': 0.8, 'feedback': 'ok'}))
        self.assertEqual(s, 0.8)
        self.assertEqual(fb, 'ok')

    def test_invalid_json_passthrough(self):
        s, fb = self._review('不是 JSON')
        self.assertIsNone(s)
        self.assertIsNone(fb)

    def test_score_clamped(self):
        s, _ = self._review(json.dumps({'score': 1.7, 'feedback': ''}))
        self.assertEqual(s, 1.0)

    def test_score_floor(self):
        s, _ = self._review(json.dumps({'score': -0.3, 'feedback': ''}))
        self.assertEqual(s, 0.0)

    def test_missing_score_passthrough(self):
        s, fb = self._review(json.dumps({'feedback': 'x'}))
        self.assertIsNone(s)
        self.assertIsNone(fb)


class ArcReviewLoopTests(unittest.TestCase):
    """_analyze_drama_arc 审稿-重试闭环。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='arcrev_')
        self.addCleanup(
            lambda: __import__('shutil').rmtree(self.tmp, ignore_errors=True))
        self.proc = PipelineProcessor(mock.MagicMock(), mock.MagicMock())
        self.proc.logger = mock.MagicMock()
        self.ai = mock.MagicMock()
        self.ai.enabled = True
        self.calls = {'director': 0, 'reviewer': 0}
        self.review_scores = []  # 依次弹出的评审分

    def _setup_ai(self, review_scores=None):
        """chat_json 按 prompt 内容分流：director/reviewer。"""
        self.review_scores = list(review_scores or [])
        djson = json.dumps(_director_json())

        def chat_json(prompt):
            if '导演兼编剧' in prompt:
                self.calls['director'] += 1
                return djson
            # reviewer
            self.calls['reviewer'] += 1
            if self.review_scores:
                sc = self.review_scores.pop(0)
                return json.dumps({'score': sc, 'feedback': f'重试{self.calls["reviewer"]}'})
            return json.dumps({'score': 0.8, 'feedback': 'ok'})

        self.ai.chat_json.side_effect = chat_json

    def _run(self, review_scores=None):
        self._setup_ai(review_scores)
        with mock.patch('processor.AICopyGenerator', return_value=self.ai):
            return self.proc._analyze_drama_arc(_eps_hl(self.tmp), 3, '剧X')

    def test_review_passes_no_retry(self):
        versions = self._run(review_scores=[0.8])
        self.assertEqual(len(versions), 3)
        self.assertEqual(self.calls['director'], 1, '通过后不应重试')
        self.assertEqual(self.calls['reviewer'], 1)

    def test_review_fails_then_retry_passes(self):
        # 首评 0.5（未过）→ 重试 → 二评 0.7（通过）
        versions = self._run(review_scores=[0.5, 0.7])
        self.assertEqual(len(versions), 3)
        self.assertEqual(self.calls['director'], 2, '未过应重试一次')
        self.assertEqual(self.calls['reviewer'], 2)

    def test_review_fails_all_retries_accept_last(self):
        # 首评 0.4 + 重试 0.5 + 再重试 0.55（耗尽 REVIEW_MAX_RETRY=2 次）
        # → 接受最后版本（director 调 3 次：初始 + 2 次重试）
        versions = self._run(review_scores=[0.4, 0.5, 0.55])
        self.assertEqual(len(versions), 3)
        self.assertEqual(self.calls['director'], 3)
        self.assertEqual(self.calls['reviewer'], 3)

    def test_review_glm_failure_passthrough(self):
        # 评审失败（None）→ 接受首版不重试
        def chat_json(prompt):
            if '导演兼编剧' in prompt:
                self.calls['director'] += 1
                return json.dumps(_director_json())
            self.calls['reviewer'] += 1
            return '不是 JSON'
        self.ai.chat_json.side_effect = chat_json
        with mock.patch('processor.AICopyGenerator', return_value=self.ai):
            versions = self.proc._analyze_drama_arc(_eps_hl(self.tmp), 3, '剧X')
        self.assertEqual(len(versions), 3)
        self.assertEqual(self.calls['director'], 1, '评审失败不应重试')


if __name__ == '__main__':
    unittest.main()
