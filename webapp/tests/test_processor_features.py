# -*- coding: utf-8 -*-
"""断点续跑 / 因果链校验 / 剧情驱动时长规划 单元测试。

覆盖 processor.py 新增功能（R10 扩展 + 报告提案3）：
- 时长规划：_compute_version_budget（剧情量×per_unit + 跨度×per_spread + 角度
  + 抖动，clamp [min_final,max_final]，×1.05 预算，min_budget 下限）/
  _seg_budget_left（预算剩额，_active_seg_target 驱动）。
- 因果链校验：_causal_check_segments（LLM 断层次替换，fail-open，缓存命中
  不调 API，used_global 跨版本去重不变量）/ _causal_check_llm（JSON 解析，
  失败→None）/ _causal_replacement_cands（同集未用候选按 score 降序）。
- 断点续跑：_source_signature（集号+mtime 签名）/ _resume_scene_valid（现场
  有效判定）/ _find_version_final（按版本锚定找成品）/ _mark_resume_version
  （原子清单更新）/ _valid_delivered_filenames（合格成品保留）。
"""
import os
import sys
import json
import glob
import random
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import processor
from processor import PipelineProcessor, CONFIG


def _mk():
    logger = mock.MagicMock()
    sm = mock.MagicMock()
    return PipelineProcessor(logger, sm), logger


def _eps_hl():
    """3 集高光（第20集双候选），与 test_drama_arc 一致。"""
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


def _segs():
    """有序段序列（3 段），供因果链/预算测试。"""
    return [
        {"episode": 20, "start": 15.0, "duration": 30.0},
        {"episode": 10, "start": 20.0, "duration": 30.0},
        {"episode": 1, "start": 10.0, "duration": 30.0},
    ]


# ============================================================
# A. 剧情驱动时长规划
# ============================================================
class TestComputeVersionBudget(unittest.TestCase):
    def _plan(self, n=3):
        return [
            {"opening_ep": 20, "angle": "高潮", "order_type": "reverse"},
            {"opening_ep": 10, "angle": "真相", "order_type": "reverse"},
            {"opening_ep": 1, "angle": "冲突", "order_type": "forward"},
        ][:n]

    def test_story_driven_budget_in_range(self):
        """剧情驱动：预算在 [min_budget, max_final*1.05]。"""
        p, _ = _mk()
        eps = _eps_hl()
        cfg = CONFIG['duration']
        min_final = float(cfg['min_final'])
        max_final = float(cfg['max_final'])
        with tempfile.TemporaryDirectory():
            # 固定随机，避免抖动导致断言边界翻车
            with mock.patch.object(random, 'uniform', return_value=30.0):
                budget = p._compute_version_budget(self._plan(), 0, eps)
            self.assertGreaterEqual(
                budget, min_final * 1.05 + p.MIN_SEG_DUR,
                '预算须 ≥ 成片下限×1.05 + 停手间隙')
            self.assertLessEqual(budget, max_final * 1.05 + 0.01,
                                 '预算须 ≤ 成片上限×1.05')

    def test_enabled_false_falls_back_to_target_dur(self):
        """enabled=False → 退回固定 edl.target_dur（早退，不设置新 target）。"""
        p, _ = _mk()
        eps = _eps_hl()
        with mock.patch.dict(CONFIG, {'duration': {'enabled': False}}):
            budget = p._compute_version_budget(self._plan(), 0, eps)
            self.assertAlmostEqual(
                budget, float(CONFIG['edl'].get('target_dur', 200.0)))

    def test_missing_plan_uses_default_no_crash(self):
        """v_idx 越界 / plan 缺失 → 不崩，units≥1 回退。"""
        p, _ = _mk()
        eps = _eps_hl()
        budget = p._compute_version_budget([], 5, eps)  # plan 空
        self.assertIsInstance(budget, float)
        budget2 = p._compute_version_budget(None, 0, eps)  # plan None
        self.assertIsInstance(budget2, float)

    def test_clamp_lower_bound_small_story(self):
        """剧情量极小（1 集）→ clamp 到 min_final 下限，不产生超短成片。"""
        p, _ = _mk()
        cfg = CONFIG['duration']
        min_final = float(cfg['min_final'])
        eps = [{"episode": 1, "path": "x.mp4", "dur": 100.0, "cands": []}]
        with mock.patch.object(random, 'uniform', return_value=0.0):
            budget = p._compute_version_budget(
                [{"opening_ep": 1, "angle": "平淡", "order_type": "reverse"}],
                0, eps)
        self.assertGreaterEqual(budget, min_final * 1.05 + p.MIN_SEG_DUR)

    def test_jitter_varies_between_calls(self):
        """两次调用（不同抖动）通常产生不同预算。"""
        p, _ = _mk()
        eps = _eps_hl()
        b1 = p._compute_version_budget(self._plan(), 0, eps)
        b2 = p._compute_version_budget(self._plan(), 0, eps)
        # 抖动 20-55s，两次几乎不可能完全相等（除非 clamp 到同一边界）
        self.assertNotAlmostEqual(b1, b2, delta=0.5)

    def test_angle_bonus_none_key_no_crash(self):
        """vplan.angle 为 None（dict.get(None) 合法）→ 不崩，角度加成 0。"""
        p, _ = _mk()
        eps = _eps_hl()
        budget = p._compute_version_budget(
            [{"opening_ep": 20, "angle": None, "order_type": "reverse"}],
            0, eps)
        self.assertIsInstance(budget, float)


class TestSegBudgetLeft(unittest.TestCase):
    def test_uses_active_seg_target_when_set(self):
        """_active_seg_target 已设 → 预算 = target - 已用段长。"""
        p, _ = _mk()
        p._active_seg_target = 300.0
        segs = [{"duration": 40.0}, {"duration": 30.0}]
        self.assertAlmostEqual(p._seg_budget_left(segs), 230.0)

    def test_falls_back_to_planned_max_without_target(self):
        """无 _active_seg_target 且 non_overlap 关 → PLANNED_SEGS_MAX 原逻辑。"""
        p, _ = _mk()
        if hasattr(p, '_active_seg_target'):
            del p._active_seg_target
        with mock.patch.dict(CONFIG['edl'], {'non_overlap': False}):
            left = p._seg_budget_left([{"duration": 50.0}])
        self.assertAlmostEqual(left, p.PLANNED_SEGS_MAX - 50.0)

    def test_falls_back_to_target_dur_with_nonoverlap(self):
        """无 target 但 non_overlap 开 → edl.target_dur。"""
        p, _ = _mk()
        if hasattr(p, '_active_seg_target'):
            del p._active_seg_target
        with mock.patch.dict(CONFIG['edl'],
                             {'non_overlap': True, 'target_dur': 240.0}):
            left = p._seg_budget_left([{"duration": 60.0}])
        self.assertAlmostEqual(left, 180.0)


# ============================================================
# B. 因果链校验
# ============================================================
class TestCausalReplacementCands(unittest.TestCase):
    def test_same_episode_untaken_sorted_by_score(self):
        """同集未用候选按 score 降序；used_global 去重。"""
        p, _ = _mk()
        eps = _eps_hl()  # ep20 两个候选：score8@15, score6@60
        old = {"episode": 20, "start": 15.0, "duration": 30.0}
        used = set()
        cands = p._causal_replacement_cands(eps, old, _segs(), used)
        self.assertEqual([c['score'] for c in cands], [8, 6],
                         '同集候选按 score 降序')
        # 已用候选应被排除
        used.add((20, 15.0))
        cands2 = p._causal_replacement_cands(eps, old, _segs(), used)
        self.assertEqual([c['score'] for c in cands2], [6],
                         'used_global 命中应排除该候选')

    def test_cross_episode_excluded(self):
        """非当前集候选被排除（只取同集，保守不引入跨集跳切）。"""
        p, _ = _mk()
        eps = _eps_hl()
        old = {"episode": 10, "start": 20.0, "duration": 30.0}
        cands = p._causal_replacement_cands(eps, old, _segs(), set())
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]['episode'], 10)

    def test_min_seg_dur_filter(self):
        """dur < MIN_SEG_DUR 的候选被过滤。"""
        p, _ = _mk()
        eps = [{"episode": 5, "path": "x.mp4", "dur": 100.0,
                "cands": [{"score": 9, "start": 0, "dur": 5.0,
                           "src": "llm", "hook_type": "x"}]}]
        old = {"episode": 5, "start": 0, "duration": 30.0}
        cands = p._causal_replacement_cands(eps, old, [old], set())
        self.assertEqual(cands, [], 'dur < MIN_SEG_DUR 应被过滤')


class TestCausalCheckLlm(unittest.TestCase):
    def _call(self, content, retry=1):
        p, _ = _mk()
        ai = mock.MagicMock()
        ai.enabled = True
        ai.chat_json.return_value = content
        with mock.patch.object(processor, 'AICopyGenerator',
                               return_value=ai):
            return p._causal_check_llm('测试剧', ['[0] 第20集 [15-45s] x'], retry)

    def test_valid_breaks_parsed(self):
        breaks = self._call(json.dumps(
            {"breaks": [{"index": 1, "reason": "前后矛盾"},
                        {"index": 2, "reason": "时间倒流"}]}))
        self.assertEqual(breaks, [{"index": 1, "reason": "前后矛盾"},
                                  {"index": 2, "reason": "时间倒流"}])

    def test_braces_extracted_from_noisy(self):
        """JSON 嵌在额外文本里 → 提取大括号包裹段。"""
        breaks = self._call("前文 { \"breaks\": [{\"index\": 0, \"reason\": \"断层\"}] } 后文")
        self.assertEqual(breaks, [{"index": 0, "reason": "断层"}])

    def test_invalid_returns_none(self):
        """真正失败（非 dict / JSON 解析失败）→ None（fail-open 信号）。"""
        self.assertIsNone(self._call("不是json"))

    def test_no_breaks_key_returns_empty_list(self):
        """dict 但 breaks 无效/缺失 → []（成功解析、无断层），非 None。"""
        self.assertEqual(self._call(json.dumps({"foo": 1})), [])
        self.assertEqual(self._call(json.dumps({"breaks": "notalist"})), [])

    def test_disabled_ai_returns_none(self):
        p, _ = _mk()
        ai = mock.MagicMock()
        ai.enabled = False
        with mock.patch.object(processor, 'AICopyGenerator',
                               return_value=ai):
            self.assertIsNone(p._causal_check_llm('测试剧', ['x']))

    def test_retry_then_success(self):
        """首次失败 → 重试成功。"""
        p, _ = _mk()
        ai = mock.MagicMock()
        ai.enabled = True
        ai.chat_json.side_effect = [None, json.dumps(
            {"breaks": [{"index": 0, "reason": "r"}]})]
        with mock.patch.object(processor, 'AICopyGenerator',
                               return_value=ai):
            breaks = p._causal_check_llm('测试剧', ['x'], retry=1)
        self.assertEqual(breaks, [{"index": 0, "reason": "r"}])
        self.assertEqual(ai.chat_json.call_count, 2)


class TestCausalCheckSegments(unittest.TestCase):
    def _transcribe(self, out_dir, ep_num, text="台词"):
        """造 transcribe 词表 JSON，供 seg_text 读取。"""
        td = os.path.join(out_dir, 'transcribe')
        os.makedirs(td, exist_ok=True)
        # ep_raw 用 basename 构造 EP<basename>.json
        jp = os.path.join(td, f'EP第{ep_num}集.json')
        with open(jp, 'w', encoding='utf-8') as f:
            json.dump([{"start": 0, "end": 100, "text": text}], f)
        return jp

    def _eps_with_real_paths(self, out_dir):
        eps = _eps_hl()
        td = os.path.join(out_dir, 'transcribe')
        os.makedirs(td, exist_ok=True)
        for e in eps:
            e['path'] = os.path.join(out_dir, f'第{e["episode"]}集.mp4')
            # ep_raw 拼接用 basename：第N集.mp4 → EP第N集.json
            jp = os.path.join(td, f'EP第{e["episode"]}集.json')
            if not os.path.exists(jp):
                with open(jp, 'w', encoding='utf-8') as f:
                    json.dump([{"start": 0, "end": 100, "text": "台词"}], f)
        return eps

    def test_disabled_returns_same(self):
        p, _ = _mk()
        segs = _segs()
        with mock.patch.dict(CONFIG, {'causal_check': {'enabled': False}}):
            out = p._causal_check_segments(
                segs, _eps_hl(), '测试剧', tempfile.mkdtemp(), 0)
        self.assertIs(out, segs)

    def test_lt_two_segments_returns_same(self):
        p, _ = _mk()
        segs = [{"episode": 20, "start": 15.0, "duration": 30.0}]
        with mock.patch.dict(CONFIG, {'causal_check': {'enabled': True,
                                                       'max_replace': 2,
                                                       'retry': 0}}):
            out = p._causal_check_segments(
                segs, _eps_hl(), '测试剧', tempfile.mkdtemp(), 0)
        self.assertIs(out, segs)

    def test_llm_success_replaces_and_updates_used(self):
        """LLM 返回断层次 → 替换对应段 + 更新 used_global。"""
        p, _ = _mk()
        with tempfile.TemporaryDirectory() as d:
            eps = self._eps_with_real_paths(d)
            used = set()
            # 第2段(ep10@20)断层，替换为同集候选；第3段正常
            with mock.patch.object(
                    p, '_causal_check_llm',
                    return_value=[{"index": 1, "reason": "时间线断层"}]):
                out = p._causal_check_segments(
                    _segs(), eps, '测试剧', d, 0, used_global=used)
            self.assertEqual(out[1]['episode'], 10,
                             '断层段应替换为同集候选')
            self.assertIn((10, 20.0), used, 'used_global 应记录替换后的段')

    def test_llm_failure_fail_open(self):
        """LLM 失败（None）→ 原样放行，不崩。"""
        p, _ = _mk()
        with tempfile.TemporaryDirectory() as d:
            eps = self._eps_with_real_paths(d)
            with mock.patch.object(p, '_causal_check_llm',
                                   return_value=None):
                out = p._causal_check_segments(
                    _segs(), eps, '测试剧', d, 0)
            self.assertEqual(out, _segs())

    def test_no_dialogue_passes_through(self):
        """全无台词 → 视为连贯放行。"""
        p, _ = _mk()
        with tempfile.TemporaryDirectory() as d:
            eps = self._eps_with_real_paths(d)
            # 清空词表 → seg_text 全空
            for e in eps:
                jp = os.path.join(d, 'transcribe', f'EP第{e["episode"]}集.json')
                with open(jp, 'w', encoding='utf-8') as f:
                    json.dump([{"start": 0, "end": 100, "text": ""}], f)
            with mock.patch.object(p, '_causal_check_llm') as m:
                out = p._causal_check_segments(
                    _segs(), eps, '测试剧', d, 0)
                m.assert_not_called()
            self.assertEqual(out, _segs())

    def test_cache_hit_skips_llm(self):
        """第二次同段序列 → 缓存命中，不重复调 LLM。"""
        p, _ = _mk()
        with tempfile.TemporaryDirectory() as d:
            eps = self._eps_with_real_paths(d)
            with mock.patch.object(
                    p, '_causal_check_llm',
                    return_value=[{"index": 1, "reason": "x"}]) as m:
                p._causal_check_segments(_segs(), eps, '测试剧', d, 0)
                self.assertEqual(m.call_count, 1)
                p._causal_check_segments(_segs(), eps, '测试剧', d, 0)
                self.assertEqual(m.call_count, 1,
                                 '缓存命中不应重复调 LLM')

    def test_max_replace_cap(self):
        """替换数不超过 max_replace。"""
        p, _ = _mk()
        with tempfile.TemporaryDirectory() as d:
            eps = self._eps_with_real_paths(d)
            # 3 个断层但 max_replace=2
            with mock.patch.object(
                    p, '_causal_check_llm',
                    return_value=[{"index": 0, "reason": "a"},
                                  {"index": 1, "reason": "b"},
                                  {"index": 2, "reason": "c"}]):
                out = p._causal_check_segments(
                    _segs(), eps, '测试剧', d, 0)
            # 统计与原始不同的段
            changed = sum(1 for a, b in zip(out, _segs())
                          if a != b)
            self.assertLessEqual(changed, 2, '替换数须 ≤ max_replace')


# ============================================================
# C. 断点续跑
# ============================================================
class TestResumeManifest(unittest.TestCase):
    def test_mark_and_load_roundtrip(self):
        """_mark_resume_version 写入 → _load 读取。"""
        p, _ = _mk()
        with tempfile.TemporaryDirectory() as d:
            p._mark_resume_version(d, 1, status='post_done',
                                   extra='v1_extra')
            man = p._load_resume_manifest(d)
            self.assertEqual(man['versions']['1']['status'], 'post_done')
            self.assertEqual(man['versions']['1']['extra'], 'v1_extra')

    def test_load_missing_returns_empty(self):
        p, _ = _mk()
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(p._load_resume_manifest(d), {})

    def test_mark_two_versions_keeps_both(self):
        p, _ = _mk()
        with tempfile.TemporaryDirectory() as d:
            p._mark_resume_version(d, 1, status='post_done')
            p._mark_resume_version(d, 2, status='qc_done')
            man = p._load_resume_manifest(d)
            self.assertEqual(len(man['versions']), 2)
            self.assertEqual(man['versions']['2']['status'], 'qc_done')


class TestSourceSignature(unittest.TestCase):
    def test_returns_eps_and_mtimes(self):
        """list_episodes → eps 集号列表 + mtimes。"""
        p, _ = _mk()
        with tempfile.TemporaryDirectory() as d:
            for ep in (1, 2, 3):
                open(os.path.join(d, f'第{ep}集.mp4'), 'w').close()
            with mock.patch.object(p, 'list_episodes',
                                   return_value=[(1, os.path.join(d, '第1集.mp4')),
                                                 (2, os.path.join(d, '第2集.mp4')),
                                                 (3, os.path.join(d, '第3集.mp4'))]):
                sig = p._source_signature(d)
            self.assertEqual(sig['eps'], [1, 2, 3])
            self.assertEqual(len(sig['mtimes']), 3)

    def test_failure_returns_none(self):
        p, _ = _mk()
        with mock.patch.object(p, 'list_episodes',
                               side_effect=Exception('boom')):
            self.assertIsNone(p._source_signature('X'))


class TestResumeSceneValid(unittest.TestCase):
    def _write_config(self, out_dir):
        with open(os.path.join(out_dir, 'edl_config_v1.json'),
                  'w', encoding='utf-8') as f:
            json.dump({'drama_name': 't', 'videos': []}, f)

    def test_no_config_false(self):
        p, _ = _mk()
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(p._resume_scene_valid(d, d))

    def test_no_manifest_false(self):
        """有 config 但无 source_sig → 不接管（保守）。"""
        p, _ = _mk()
        with tempfile.TemporaryDirectory() as d:
            self._write_config(d)
            self.assertFalse(p._resume_scene_valid(d, d))

    def test_signature_match_true(self):
        """有 config + 签名匹配 → 可续跑。"""
        p, _ = _mk()
        with tempfile.TemporaryDirectory() as d:
            self._write_config(d)
            p._mark_resume_version(d, 1)  # 建清单
            man = p._load_resume_manifest(d)
            man['source_sig'] = {'eps': [1, 2], 'mtimes': [100, 200]}
            p._save_resume_manifest(d, man)
            with mock.patch.object(p, '_source_signature',
                                   return_value={'eps': [1, 2],
                                                 'mtimes': [100, 200]}):
                self.assertTrue(p._resume_scene_valid(d, d))

    def test_signature_mismatch_false(self):
        """签名不匹配（素材已变）→ 不续跑，回退全量。"""
        p, _ = _mk()
        with tempfile.TemporaryDirectory() as d:
            self._write_config(d)
            man = {'versions': {}, 'source_sig': {'eps': [1], 'mtimes': [1]}}
            p._save_resume_manifest(d, man)
            with mock.patch.object(p, '_source_signature',
                                   return_value={'eps': [2], 'mtimes': [2]}):
                self.assertFalse(p._resume_scene_valid(d, d))

    def test_signature_fail_passes(self):
        """签名算不出 → 放行续跑（保守放行）。"""
        p, _ = _mk()
        with tempfile.TemporaryDirectory() as d:
            self._write_config(d)
            man = {'versions': {}, 'source_sig': {'eps': [1], 'mtimes': [1]}}
            p._save_resume_manifest(d, man)
            with mock.patch.object(p, '_source_signature',
                                   return_value=None):
                self.assertTrue(p._resume_scene_valid(d, d))


class TestFindVersionFinal(unittest.TestCase):
    def test_finds_versioned_mp4(self):
        p, _ = _mk()
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, '测试剧_1_打脸_去重版.mp4'), 'w').close()
            f = p._find_version_final(d, 0)
            self.assertTrue(f and f.endswith('_1_打脸_去重版.mp4'))

    def test_finds_v_mp4(self):
        p, _ = _mk()
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, '测试剧V2.mp4'), 'w').close()
            f = p._find_version_final(d, 1)
            self.assertTrue(f and f.endswith('V2.mp4'))

    def test_not_found_returns_none(self):
        p, _ = _mk()
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(p._find_version_final(d, 0))


class TestValidDeliveredFilenames(unittest.TestCase):
    def test_keeps_valid_excludes_invalid(self):
        p, _ = _mk()
        with tempfile.TemporaryDirectory() as d:
            ok = os.path.join(d, 'a_1_x_去重版.mp4')
            bad = os.path.join(d, 'b_2_x_去重版.mp4')
            non = os.path.join(d, 'c_3_x_raw.mp4')
            for f in (ok, bad, non):
                open(f, 'w').close()
            with mock.patch.object(
                    p, '_validate_delivery_media',
                    side_effect=lambda f: {'ok': f == ok}):
                kept = p._valid_delivered_filenames(d)
            self.assertIn(os.path.basename(ok), kept)
            self.assertNotIn(os.path.basename(bad), kept)
            self.assertNotIn(os.path.basename(non), kept)

    def test_all_invalid_returns_none(self):
        """无合格成品 → None（调用方回退全清，保守）。"""
        p, _ = _mk()
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, 'a_1_x_去重版.mp4'), 'w').close()
            with mock.patch.object(p, '_validate_delivery_media',
                                   return_value={'ok': False}):
                self.assertIsNone(p._valid_delivered_filenames(d))


if __name__ == '__main__':
    unittest.main()
