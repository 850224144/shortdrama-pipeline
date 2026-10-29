# -*- coding: utf-8 -*-
"""导演/编剧弧线方案回归测试（版本差异化切片）。

需求（2026-10-29 用户）：「当前成品剧情重复率太高，开头几乎都是同一个」
「同一段素材被重复使用」「每一集都应该有故事的主线，开头是钩子，后面叙事」。

根因：_generate_reverse_segments 开头钩子不分版本（v_idx 不参与开头选择），
所有版本取同一个全局最高分钩子（或锁结局集）；且每集只取 1 个高光候选，
V1-V8 用完全相同的 6 段素材（实测段序列完全一致 = 同一视频 8 遍）。

修复：
- _HIGHLIGHT_PROMPT / _score_episode_llm：每集返回 1-3 个互不重叠候选窗口；
- _analyze_drama_arc：导演/编剧 GLM 读各集高光摘要 → 设计 count 个差异化
  叙事版本方案（不同 opening_ep + angle + order_type），失败回退默认方案；
- _generate_reverse_segments：按方案选开头集（版本不同）+ 每集候选轮换 +
  因果顺序（钩子→前因闪回→顺叙结局）。
"""
import os
import sys
import json
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import processor
from processor import PipelineProcessor


def _mk():
    logger = mock.MagicMock()
    sm = mock.MagicMock()
    return PipelineProcessor(logger, sm), logger


def _eps_hl():
    """构造 3 集高光（第20集双候选），用于 _generate_reverse_segments 测试。"""
    return [
        {"episode": 1, "path": r"X:\素材\第1集.mp4", "dur": 100.0,
         "cands": [{"score": 5, "start": 10.0, "dur": 30.0, "src": "llm",
                    "hook_type": "冲突", "reason": "r1"}]},
        {"episode": 10, "path": r"X:\素材\第10集.mp4", "dur": 100.0,
         "cands": [{"score": 9, "start": 20.0, "dur": 30.0, "src": "llm",
                    "hook_type": "真相", "reason": "r10"}]},
        {"episode": 20, "path": r"X:\素材\第20集.mp4", "dur": 100.0,
         "cands": [{"score": 8, "start": 15.0, "dur": 30.0, "src": "llm",
                    "hook_type": "高潮", "reason": "r20a"},
                   {"score": 6, "start": 60.0, "dur": 20.0, "src": "llm",
                    "hook_type": "反转", "reason": "r20b"}]},
    ]


class TestScoreEpisodeLlmMultiCand(unittest.TestCase):
    def _call(self, content, raw=None, ep_dur=100.0):
        p, _ = _mk()
        ai = mock.MagicMock()
        ai.chat_json.return_value = content
        return p._score_episode_llm(
            ai, '测试剧', 1, raw or [{"start": 0, "end": 3, "text": "x"}], ep_dur)

    def test_windows_array_returns_multi_sorted(self):
        cands = self._call(json.dumps({"windows": [
            {"score": 7, "reason": "A", "start": 10, "end": 20, "hook_type": "真相"},
            {"score": 9, "reason": "B", "start": 40, "end": 50, "hook_type": "高潮"},
            {"score": 5, "reason": "C", "start": 70, "end": 80, "hook_type": "冲突"},
        ]}))
        self.assertEqual(len(cands), 3, '应返回 3 个候选')
        self.assertEqual([c['score'] for c in cands], [9, 7, 5],
                         '候选应按 score 降序')
        # 短窗口外扩到 >=12s
        for c in cands:
            self.assertGreaterEqual(c['end'] - c['start'], 12.0)

    def test_legacy_single_window_compat(self):
        """旧格式 LLM 返回单窗口 {score,...} → 兼容为 1 候选"""
        cands = self._call(json.dumps(
            {"score": 8, "reason": "旧", "start": 20, "end": 30,
             "hook_type": "打脸"}))
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]['score'], 8)

    def test_empty_windows_returns_empty(self):
        self.assertEqual(self._call(json.dumps({"windows": []})), [])

    def test_out_of_range_window_skipped(self):
        """越界时间码（LLM 编造）→ 跳过，不产生候选"""
        cands = self._call(json.dumps({"windows": [
            {"score": 9, "reason": "越界", "start": 200, "end": 220,
             "hook_type": "高潮"}]}))
        self.assertEqual(cands, [])

    def test_bad_json_returns_empty(self):
        self.assertEqual(self._call("不是 JSON"), [])


class TestLoadHighlightsCacheCompat(unittest.TestCase):
    def _setup(self, cached):
        d = tempfile.mkdtemp()
        tr = os.path.join(d, 'transcribe')
        os.makedirs(tr)
        ep = os.path.join(d, '第1集.mp4')
        open(ep, 'w').close()
        with open(os.path.join(tr, 'EP第1集.json'), 'w', encoding='utf-8') as f:
            json.dump([{"start": 0.0, "end": 3.0, "text": "对话"}], f)
        with open(os.path.join(tr, 'highlight_scores.json'), 'w',
                  encoding='utf-8') as f:
            json.dump(cached, f, ensure_ascii=False)
        return d, ep

    def test_new_multi_cand_cache(self):
        p, _ = _mk()
        d, ep = self._setup({'1': {'cands': [
            {"score": 8, "reason": "a", "start": 10, "end": 22,
             "hook_type": "高潮"},
            {"score": 6, "reason": "b", "start": 40, "end": 55,
             "hook_type": "反转"}], 'src': 'llm'}})
        with mock.patch.object(p, '_episode_real_duration', return_value=100.0):
            out = p._load_episode_highlights([ep], drama_name='测试剧')
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0]['cands']), 2, '新缓存应还原 2 候选')
        self.assertEqual(out[0]['cands'][0]['start'], 10.0)
        self.assertEqual(out[0]['cands'][1]['start'], 40.0)

    def test_legacy_single_cand_cache(self):
        """旧缓存单候选 dict（有 score 无 cands）→ 兼容为 1 候选"""
        p, _ = _mk()
        d, ep = self._setup({'1': {'score': 7, 'reason': '旧', 'start': 10,
                                   'end': 25, 'hook_type': '真相', 'src': 'llm'}})
        with mock.patch.object(p, '_episode_real_duration', return_value=100.0):
            out = p._load_episode_highlights([ep], drama_name='测试剧')
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0]['cands']), 1)
        self.assertEqual(out[0]['cands'][0]['score'], 7)
        self.assertEqual(out[0]['cands'][0]['src'], 'llm')


class TestValidateArcPlan(unittest.TestCase):
    def test_valid_plan_accepted(self):
        p, _ = _mk()
        eps = _eps_hl()
        plan = {"versions": [
            {"opening_ep": 20, "angle": "高潮", "order_type": "reverse"},
            {"opening_ep": 10, "angle": "真相", "order_type": "flashback"},
        ]}
        v = p._validate_arc_plan(plan, eps, 2)
        self.assertIsNotNone(v)
        self.assertEqual(v[0]['opening_ep'], 20)

    def test_duplicate_opening_ep_dropped(self):
        """opening_ep 重复 → 只保留第一个，不足 count 回退 None"""
        p, _ = _mk()
        eps = _eps_hl()
        plan = {"versions": [
            {"opening_ep": 20, "angle": "高潮", "order_type": "reverse"},
            {"opening_ep": 20, "angle": "真相", "order_type": "reverse"},
        ]}
        self.assertIsNone(p._validate_arc_plan(plan, eps, 2),
                          '去重后不足 count 应回退 None')

    def test_bad_angle_and_order_fallback(self):
        p, _ = _mk()
        eps = _eps_hl()
        plan = {"versions": [
            {"opening_ep": 20, "angle": "乱写", "order_type": "乱序"},
            {"opening_ep": 10, "angle": "真相", "order_type": "forward"},
        ]}
        v = p._validate_arc_plan(plan, eps, 2)
        self.assertIsNotNone(v)
        self.assertEqual(v[0]['angle'], '高潮', '非法 angle 兜底高潮')
        self.assertEqual(v[0]['order_type'], 'reverse', '非法 order 兜底 reverse')

    def test_opening_ep_not_in_episodes_dropped(self):
        p, _ = _mk()
        eps = _eps_hl()
        plan = {"versions": [
            {"opening_ep": 999, "angle": "高潮", "order_type": "reverse"},
            {"opening_ep": 10, "angle": "真相", "order_type": "reverse"},
        ]}
        self.assertIsNone(p._validate_arc_plan(plan, eps, 2))


class TestAnalyzeDramaArcDefault(unittest.TestCase):
    def test_default_plan_rotates_top_score_openings(self):
        """AI 禁用 → 默认方案：top-N 高分集轮换做开头（不同集）"""
        p, _ = _mk()
        eps = _eps_hl()  # scores: ep10=9, ep20=8, ep1=5
        with mock.patch.object(processor, 'AICopyGenerator') as AG:
            AG.return_value.enabled = False
            plan = p._analyze_drama_arc(eps, 3, drama_name='测试剧')
        self.assertEqual(len(plan), 3)
        self.assertEqual(plan[0]['opening_ep'], 10, '最高分集优先做开头')
        self.assertEqual(plan[1]['opening_ep'], 20)
        self.assertEqual(plan[2]['opening_ep'], 1)
        eps_seen = [v['opening_ep'] for v in plan]
        self.assertEqual(len(set(eps_seen)), 3, '每版开头集必须不同')

    def test_cache_hit_skips_api(self):
        """缓存 arc_plan.json 命中 → 不调 GLM"""
        p, _ = _mk()
        with tempfile.TemporaryDirectory() as d:
            eps = _eps_hl()
            for e in eps:
                e['path'] = os.path.join(d, f'第{e["episode"]}集.mp4')
            cache_dir = os.path.join(d, 'transcribe')
            os.makedirs(cache_dir, exist_ok=True)
            plan = {"versions": [
                {"opening_ep": 20, "angle": "高潮", "order_type": "reverse"},
                {"opening_ep": 10, "angle": "真相", "order_type": "reverse"},
                {"opening_ep": 1, "angle": "冲突", "order_type": "forward"},
            ]}
            with open(os.path.join(cache_dir, 'arc_plan.json'), 'w',
                      encoding='utf-8') as f:
                json.dump(plan, f, ensure_ascii=False)
            with mock.patch.object(processor, 'AICopyGenerator') as AG:
                got = p._analyze_drama_arc(eps, 3, drama_name='测试剧')
                AG.assert_not_called()
            self.assertEqual(got[0]['opening_ep'], 20)


class TestBuildArcOrder(unittest.TestCase):
    def test_reverse_hook_then_before_desc_then_after_asc(self):
        p, _ = _mk()
        eps = _eps_hl()
        order = p._build_arc_order(eps, 20, 'reverse')
        self.assertEqual(order, [20, 10, 1], '钩子20→前因倒序(10,1)→无后果')

    def test_forward_hook_then_time_order(self):
        p, _ = _mk()
        eps = _eps_hl()
        order = p._build_arc_order(eps, 10, 'forward')
        self.assertEqual(order, [10, 1, 20], '钩子10→时间顺序(1,20)')


class TestReverseSegmentsVersionDiffer(unittest.TestCase):
    def test_versions_have_different_opening_and_sequence(self):
        """V1/V2 带方案：开头集不同 + 段序列不同 + 候选轮换"""
        p, _ = _mk()
        eps = _eps_hl()
        plan = [
            {"opening_ep": 20, "angle": "高潮", "order_type": "reverse"},
            {"opening_ep": 10, "angle": "真相", "order_type": "reverse"},
        ]
        with mock.patch.object(p, '_load_episode_highlights',
                               return_value=eps):
            v1 = p._generate_reverse_segments([], 0, plan=plan)
            v2 = p._generate_reverse_segments([], 1, plan=plan)
        self.assertTrue(v1 and v2)
        # 开头不同
        self.assertNotEqual(v1[0]['episode'], v2[0]['episode'],
                            'V1/V2 开头集必须不同')
        self.assertEqual(v1[0]['episode'], 20)
        self.assertEqual(v2[0]['episode'], 10)
        # 段序列不同（不是同一视频 8 遍）
        seq1 = [(s['episode'], s['start']) for s in v1]
        seq2 = [(s['episode'], s['start']) for s in v2]
        self.assertNotEqual(seq1, seq2, 'V1/V2 段序列必须不同')
        # 第20集候选轮换：V1 用候选0(start15)、V2 用候选1(start60)
        ep20_1 = [s for s in v1 if s['episode'] == 20][0]
        ep20_2 = [s for s in v2 if s['episode'] == 20][0]
        self.assertEqual(ep20_1['start'], 15.0)
        self.assertEqual(ep20_2['start'], 60.0)

    def test_no_plan_falls_back_to_old_logic(self):
        """无方案 → 回退原逻辑（结局集分≥70%最高则锁结局，否则最炸），兼容行为"""
        p, _ = _mk()
        eps = _eps_hl()
        with mock.patch.object(p, '_load_episode_highlights',
                               return_value=eps):
            segs = p._generate_reverse_segments([], 0, plan=None)
        self.assertTrue(segs)
        # 结局集(ep20)分 8 / 全局最高(ep10)分 9 = 89% ≥ 70% → 锁结局集做开头
        self.assertEqual(segs[0]['episode'], 20, '结局集分≥70%最高 → 锁结局')
        eps_seq = [s['episode'] for s in segs]
        self.assertEqual(eps_seq, sorted(eps_seq, reverse=True),
                         '无方案回退集号逆序')


if __name__ == '__main__':
    unittest.main()
