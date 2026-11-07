# -*- coding: utf-8 -*-
"""本剧切制参数（cut_config）单元测试。

覆盖 2026-11-05「每部剧可配置参数」功能（方案A）：
- _resolve_cut_config：默认合并 / 版本数 clamp [1,8] / 时长 clamp [60,1200]
  / min<max 保证 / reuse_material 布尔 / watermark 透传。
- _compute_version_budget：config 的 min_dur/max_dur 替代旧 max_final=1200
  （回归：剧情量巨大也不膨胀到 1260s，方案A 每版都在 [min_dur, max_dur]）。
- _capacity_version_count：reuse_material 门控——禁止复用 clamp 到零复用容量，
  允许复用则按设定 ≤8，显式设定低于容量则尊重设定。
"""
import os
import sys
import random
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import processor
from processor import PipelineProcessor, CONFIG, DEFAULT_CUT_CONFIG, CUT_CONFIG_ABS


def _mk():
    logger = mock.MagicMock()
    sm = mock.MagicMock()
    return PipelineProcessor(logger, sm), logger


def _eps_hl(n=36):
    """n 集高光（每集一个候选），供剧情量巨大场景。"""
    return [
        {"episode": i + 1, "path": rf"X:\素材\第{i+1}集.mp4", "dur": 100.0,
         "cands": [{"score": 5, "start": 10.0, "dur": 30.0, "src": "llm",
                    "hook_type": "冲突", "reason": f"r{i+1}"}]}
        for i in range(n)
    ]


def _plan():
    return [{"opening_ep": 36, "angle": "高潮", "order_type": "reverse"}]


# ============================================================
# A. _resolve_cut_config
# ============================================================
class TestResolveCutConfig(unittest.TestCase):
    def test_none_returns_defaults(self):
        p, _ = _mk()
        rc = p._resolve_cut_config(None)
        self.assertEqual(rc['version_count'], None)
        self.assertEqual(rc['min_dur'], 180.0)
        self.assertEqual(rc['max_dur'], 330.0)
        self.assertIs(rc['reuse_material'], False)
        self.assertIsNone(rc['watermark'])

    def test_version_count_clamped_1_8(self):
        p, _ = _mk()
        self.assertEqual(p._resolve_cut_config({'version_count': 0})['version_count'], 1)
        self.assertEqual(p._resolve_cut_config({'version_count': 99})['version_count'], 8)
        self.assertEqual(p._resolve_cut_config({'version_count': 3})['version_count'], 3)

    def test_duration_clamped_60_1200(self):
        p, _ = _mk()
        self.assertEqual(p._resolve_cut_config({'min_dur': 10})['min_dur'], 60.0)
        self.assertEqual(p._resolve_cut_config({'max_dur': 5000})['max_dur'], 1200.0)

    def test_min_lt_max_enforced(self):
        p, _ = _mk()
        rc = p._resolve_cut_config({'min_dur': 400, 'max_dur': 300})
        self.assertLess(rc['min_dur'], rc['max_dur'])
        self.assertEqual(rc['min_dur'], 400.0)
        self.assertEqual(rc['max_dur'], 460.0)  # min+60

    def test_reuse_and_watermark_roundtrip(self):
        p, _ = _mk()
        rc = p._resolve_cut_config({'reuse_material': True, 'watermark': '福桃说剧'})
        self.assertIs(rc['reuse_material'], True)
        self.assertEqual(rc['watermark'], '福桃说剧')

    def test_partial_fields_keep_defaults(self):
        """只写一个字段 → 其余保持默认。"""
        p, _ = _mk()
        rc = p._resolve_cut_config({'max_dur': 250})
        self.assertEqual(rc['max_dur'], 250.0)
        self.assertEqual(rc['min_dur'], 180.0)
        self.assertEqual(rc['version_count'], None)


# ============================================================
# B. _compute_version_budget 用 config 时长（方案A 回归）
# ============================================================
class TestComputeVersionBudgetConfig(unittest.TestCase):
    def test_big_arc_capped_by_default_330(self):
        """回归：36 集全弧线剧情量巨大，预算须被默认 max_dur=330 夹住，
        绝不膨胀到旧 max_final=1200 → 1260s。"""
        p, _ = _mk()
        eps = _eps_hl(36)
        with mock.patch.object(random, 'uniform', return_value=30.0):
            budget = p._compute_version_budget(_plan(), 0, eps, config=None)
        self.assertLessEqual(budget, 330.0 * 1.05 + 0.01,
                             '预算须 ≤ 默认 max_dur×1.05（修复 1260s 膨胀）')
        self.assertGreaterEqual(budget, 180.0 * 1.05 + p.MIN_SEG_DUR - 0.01)

    def test_config_max_dur_overrides(self):
        """config 显式 max_dur=100 → 预算被 100 夹住（替代全局 max_final）。"""
        p, _ = _mk()
        eps = _eps_hl(36)
        with mock.patch.object(random, 'uniform', return_value=30.0):
            budget = p._compute_version_budget(
                _plan(), 0, eps, config={'min_dur': 60, 'max_dur': 100})
        self.assertLessEqual(budget, 100.0 * 1.05 + 0.01)
        self.assertGreaterEqual(budget, 60.0 * 1.05 + p.MIN_SEG_DUR - 0.01)

    def test_larger_max_dur_allows_longer(self):
        """config max_dur=600 → 预算可超过默认 346.5（允许用户设长版）。"""
        p, _ = _mk()
        eps = _eps_hl(36)
        with mock.patch.object(random, 'uniform', return_value=30.0):
            budget = p._compute_version_budget(
                _plan(), 0, eps, config={'min_dur': 180, 'max_dur': 600})
        self.assertGreater(budget, 330.0 * 1.05 + 0.01,
                           'max_dur=600 时应比默认 330 版本更长')


# ============================================================
# C. _capacity_version_count 素材复用门控
# ============================================================
class TestCapacityVersionCountConfig(unittest.TestCase):
    def _patch_env(self, p, total_cands=30):
        """30 候选 / 每版 10 段 → 零复用上限 3 版。"""
        cfg_edl = {'target_dur': 200.0, 'segment_dur': 20.0,
                   'allow_controlled_reuse': False}
        hl = [{"episode": i + 1,
               "cands": [{"score": 5, "start": 10.0, "dur": 30.0}]}
              for i in range(total_cands)]
        return mock.patch.dict(CONFIG, {'edl': cfg_edl}), \
            mock.patch.object(p, '_load_episode_highlights',
                              return_value=hl)

    def test_reuse_forbidden_clamps_to_capacity(self):
        """reuse_material=False：设 8 版但零复用只够 3 → clamp 到 3，绝不超容复用。"""
        p, _ = _mk()
        edl_eps = [{"episode": i + 1} for i in range(36)]  # auto=8
        cfg_patch, hl_patch = self._patch_env(p)
        with cfg_patch, hl_patch:
            count = p._capacity_version_count(
                edl_eps, '测试剧', config={'version_count': 8,
                                           'reuse_material': False})
        self.assertEqual(count, 3)

    def test_reuse_allowed_keeps_explicit(self):
        """reuse_material=True：允许跨版本复用 → 按设定 8 版（≤8）。"""
        p, _ = _mk()
        edl_eps = [{"episode": i + 1} for i in range(36)]
        cfg_patch, hl_patch = self._patch_env(p)
        with cfg_patch, hl_patch:
            count = p._capacity_version_count(
                edl_eps, '测试剧', config={'version_count': 8,
                                           'reuse_material': True})
        self.assertEqual(count, 8)

    def test_explicit_below_capacity_respected(self):
        """显式设 2 版（< 零复用容量 3）→ 尊重设定 2。"""
        p, _ = _mk()
        edl_eps = [{"episode": i + 1} for i in range(36)]
        cfg_patch, hl_patch = self._patch_env(p)
        with cfg_patch, hl_patch:
            count = p._capacity_version_count(
                edl_eps, '测试剧', config={'version_count': 2,
                                           'reuse_material': False})
        self.assertEqual(count, 2)

    def test_capacity_failure_falls_back_to_episode_rule(self):
        """容量计算抛异常 → 回退集数判定（36 集 → 8）。"""
        p, _ = _mk()
        edl_eps = [{"episode": i + 1} for i in range(36)]
        with mock.patch.object(p, '_load_episode_highlights',
                               side_effect=RuntimeError('boom')):
            count = p._capacity_version_count(
                edl_eps, '测试剧', config={'version_count': 8,
                                           'reuse_material': False})
        self.assertEqual(count, 8)

    def test_config_knob_max_versions_respected(self):
        """CONFIG edl.max_versions=6 收紧 → 设 8 实际 6（既有旋钮仍生效）。"""
        p, _ = _mk()
        edl_eps = [{"episode": i + 1} for i in range(36)]
        cfg_edl = {'target_dur': 200.0, 'segment_dur': 20.0,
                   'allow_controlled_reuse': False,
                   'min_versions': 1, 'max_versions': 6}
        hl = [{"episode": i + 1,
               "cands": [{"score": 5, "start": 10.0, "dur": 30.0}]}
              for i in range(30)]
        with mock.patch.dict(CONFIG, {'edl': cfg_edl}), \
             mock.patch.object(p, '_load_episode_highlights',
                               return_value=hl):
            count = p._capacity_version_count(
                edl_eps, '测试剧', config={'version_count': 8,
                                           'reuse_material': True})
        self.assertEqual(count, 6, 'max_versions=6 旋钮应把 8 收紧到 6')


# ============================================================
# D. _select_best_videos 时长窗口用 config（方案A）
# ============================================================
class TestSelectBestVideosConfig(unittest.TestCase):
    def _videos(self):
        """8 片：6 片在窗口 [180,330]，1 片过短(100s)，1 片过长(400s)。"""
        vs = []
        for i in range(6):
            vs.append({"filename": f"剧_{i+1}_主题_去重版.mp4",
                       "path": f"p{i+1}.mp4"})
        vs.append({"filename": "剧_7_主题_去重版.mp4", "path": "p7.mp4"})  # 100s
        vs.append({"filename": "剧_8_主题_去重版.mp4", "path": "p8.mp4"})  # 400s
        return vs

    def test_window_uses_config_bounds(self):
        """config 窗口 [180,330]：100s/400s 被拒（valid=6 不触发兜底）。"""
        p, _ = _mk()
        dur_map = {f"p{i+1}.mp4": 300.0 for i in range(6)}
        dur_map["p7.mp4"] = 100.0
        dur_map["p8.mp4"] = 400.0
        with mock.patch.object(p, '_get_video_duration',
                               side_effect=lambda path: dur_map[path]):
            best = p._select_best_videos(
                self._videos(), config={'min_dur': 180, 'max_dur': 330})
        names = [v['filename'] for v in best]
        self.assertEqual(len(best), 6, 'valid=6 不应触发 <6 兜底')
        self.assertIn("剧_1_主题_去重版.mp4", names)
        self.assertNotIn("剧_7_主题_去重版.mp4", names)  # 100s 过短
        self.assertNotIn("剧_8_主题_去重版.mp4", names)  # 400s 过长

    def test_default_window_180_330(self):
        """默认窗口（config=None）也收敛到 [180,330]，不再接受 21 分钟片。"""
        p, _ = _mk()
        with mock.patch.object(p, '_get_video_duration',
                               side_effect=lambda path: 1260.0):  # 21 分钟
            best = p._select_best_videos(
                [{"filename": "剧_1_主题_去重版.mp4", "path": "p1.mp4"},
                 {"filename": "剧_2_主题_去重版.mp4", "path": "p2.mp4"},
                 {"filename": "剧_3_主题_去重版.mp4", "path": "p3.mp4"},
                 {"filename": "剧_4_主题_去重版.mp4", "path": "p4.mp4"},
                 {"filename": "剧_5_主题_去重版.mp4", "path": "p5.mp4"},
                 {"filename": "剧_6_主题_去重版.mp4", "path": "p6.mp4"},
                 {"filename": "剧_7_主题_去重版.mp4", "path": "p7.mp4"}],
                config=None)
        # 全部 1260s 超窗口 → valid=0 → 兜底 <6 全加回（不归零）
        self.assertEqual(len(best), 7, '超窗但 valid<6 → 兜底保留，不归零')


# ============================================================
# E. cut_config 表 CRUD 往返（job_store）
# ============================================================
class TestCutConfigStore(unittest.TestCase):
    def test_upsert_roundtrip_preserves_values(self):
        """写 5 版/250s/复用开/水印 → 读回原值（回归：版本数不再存成 1）。"""
        from job_store import JobStore
        store = JobStore(':memory:')
        store.upsert_cut_config('余生由我落笔', version_count=5, min_dur=200,
                                max_dur=250, reuse_material=True,
                                watermark='福桃说剧')
        row = store.get_cut_config('余生由我落笔')
        assert row is not None
        self.assertEqual(row['version_count'], 5, '版本数须存用户值，非 1')
        self.assertEqual(row['min_dur'], 200.0)
        self.assertEqual(row['max_dur'], 250.0)
        self.assertIs(row['reuse_material'], True)
        self.assertEqual(row['watermark'], '福桃说剧')

    def test_reuse_false_stored_as_zero(self):
        """reuse_material=False 显式写入 → 存 0（回归：不再误存成 1）。"""
        from job_store import JobStore
        store = JobStore(':memory:')
        store.upsert_cut_config('测试剧', reuse_material=False)
        row = store.get_cut_config('测试剧')
        assert row is not None
        self.assertIs(row['reuse_material'], False)
        self.assertEqual(row['reuse_material'], 0 if False else False)

    def test_partial_update_keeps_old(self):
        """只写 max_dur → 旧 version_count 保留（COALESCE 语义）。"""
        from job_store import JobStore
        store = JobStore(':memory:')
        store.upsert_cut_config('剧A', version_count=4, min_dur=180,
                                max_dur=330)
        store.upsert_cut_config('剧A', max_dur=280)  # 只改上限
        row = store.get_cut_config('剧A')
        assert row is not None
        self.assertEqual(row['version_count'], 4, '未写字段须保留旧值')
        self.assertEqual(row['max_dur'], 280.0)

    def test_delete_removes_override(self):
        from job_store import JobStore
        store = JobStore(':memory:')
        store.upsert_cut_config('剧B', version_count=3)
        store.delete_cut_config('剧B')
        self.assertIsNone(store.get_cut_config('剧B'))

    def test_list_returns_all(self):
        from job_store import JobStore
        store = JobStore(':memory:')
        store.upsert_cut_config('剧C', version_count=2)
        store.upsert_cut_config('剧D', version_count=6)
        names = [c['drama_name'] for c in store.list_cut_configs()]
        self.assertEqual(names, ['剧C', '剧D'])


# ============================================================
# E. _detect_source_pattern / _precheck_source_pattern（源文件名前缀修复）
# ============================================================
class TestDetectSourcePattern(unittest.TestCase):
    """2026-11-05 修复：剧名带序号前缀（如"12重生成他弟媳后"）而文件名不带时，
    原 {name} 占位符替换后全部失配 → fail-fast 预检系统性误杀。改为从样例文件
    取真实字面量前缀。"""

    def test_literal_prefix_when_name_mismatch(self):
        """"12重生成他弟媳后" vs 文件"重生成他弟媳后-第10集.mp4" → 字面量前缀。"""
        pat = PipelineProcessor._detect_source_pattern(
            '重生成他弟媳后-第10集.mp4')
        self.assertEqual(pat, '重生成他弟媳后-第{ep}集.mp4')

    def test_literal_prefix_dash_space(self):
        """"15婆婆的新婚规矩" vs 文件"婆婆的新婚规矩 - 第10集.mp4"（空格-）。"""
        pat = PipelineProcessor._detect_source_pattern(
            '婆婆的新婚规矩 - 第10集.mp4')
        self.assertEqual(pat, '婆婆的新婚规矩 - 第{ep}集.mp4')

    def test_standard_name_still_matches(self):
        """正常一致（前缀==剧名）仍产出正确模式，行为不变。"""
        pat = PipelineProcessor._detect_source_pattern(
            '余生由我落笔-第01集.mp4')
        self.assertEqual(pat, '余生由我落笔-第{ep:02d}集.mp4')
        self.assertEqual(
            pat.format(name='余生由我落笔', ep=1), '余生由我落笔-第01集.mp4')

    def test_name_contains_episode_marker_uses_last(self):
        """剧名本身含"第N集"（如"第1集就离婚"）时取最后一个为集号标记。"""
        pat = PipelineProcessor._detect_source_pattern(
            '第1集就离婚-第02集.mp4')
        self.assertEqual(pat, '第1集就离婚-第{ep:02d}集.mp4')
        self.assertEqual(
            pat.format(name='第1集就离婚', ep=2), '第1集就离婚-第02集.mp4')

    def test_underscore_suffix_literal(self):
        """"_集号.mp4" 后缀式：字面量前缀含分隔符。"""
        pat = PipelineProcessor._detect_source_pattern('剧名_10.mp4')
        self.assertEqual(pat, '剧名_{ep}.mp4')
        self.assertEqual(pat.format(name='剧名', ep=10), '剧名_10.mp4')

    def test_braces_in_prefix_escaped(self):
        """字面量前缀含花括号须转义，防 .format() 误解析。"""
        pat = PipelineProcessor._detect_source_pattern(
            '剧{名}-第3集.mp4')
        self.assertEqual(pat, '剧{{名}}-第{ep}集.mp4')
        self.assertEqual(pat.format(name='x', ep=3), '剧{名}-第3集.mp4')


class TestPrecheckSourcePattern(unittest.TestCase):
    """预检在"剧名带序号前缀、文件名不带"时须通过（回归用例）。"""

    def _tmp_src(self, filenames):
        import tempfile
        d = tempfile.mkdtemp(prefix='srcchk_')
        for fn in filenames:
            with open(os.path.join(d, fn), 'w', encoding='utf-8') as f:
                f.write('x')
        return d

    def test_precheck_passes_with_numeric_prefix_name(self):
        """"12重生成他弟媳后" + 文件"重生成他弟媳后-第N集.mp4" → 预检通过。"""
        p, _ = _mk()
        src = self._tmp_src([
            '重生成他弟媳后-第10集.mp4',
            '重生成他弟媳后-第11集.mp4',
            '重生成他弟媳后-第12集.mp4',
        ])
        try:
            pat = PipelineProcessor._detect_source_pattern(
                '重生成他弟媳后-第10集.mp4')
            err = p._precheck_source_pattern(src, pat, '12重生成他弟媳后')
            self.assertIsNone(err, f'预检应通过，得到：{err}')
        finally:
            import shutil
            shutil.rmtree(src, ignore_errors=True)

    def test_precheck_still_fails_on_true_missing(self):
        """真缺素材（目录无 mp4）仍须拦下。"""
        p, _ = _mk()
        src = self._tmp_src(['readme.txt'])
        try:
            err = p._precheck_source_pattern(src, '{ep}.mp4', '某剧')
            self.assertIsNotNone(err)
        finally:
            import shutil
            shutil.rmtree(src, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
