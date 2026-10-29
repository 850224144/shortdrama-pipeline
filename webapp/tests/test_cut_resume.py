# -*- coding: utf-8 -*-
"""失败可继续处理（续跑）回归测试。

需求（2026-10-29 用户）：「建议增加一个失败可以继续处理的功能，比如今天
失败了，你修复后，我想继续没办法继续」。

根因：失败路径 _clean_partial_outputs 默认 include_configs=True 会删除
edl_config_v*.json（失败现场），修复后只能全量重切（重新 ASR + 生成 EDL），
无法只重渲染失败的版本。

修复：
- 失败路径 / run 开始清理 / 交付路径 一律 include_configs=False 保留 edl_config；
- 新增 _re_render_from_configs：读保留 config 直接重渲染，不重新生成 EDL；
- manual_cut(resume=True) / _cut_once(resume=True) 续跑，无 config 回退全量；
- /api/cut/retry API 入口。
"""
import os
import sys
import json
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from processor import PipelineProcessor


def _mk():
    logger = mock.MagicMock()
    sm = mock.MagicMock()
    return PipelineProcessor(logger, sm), logger


def _write_config(out_dir, vid, segments=None):
    cfg = {
        'drama_name': '测试剧',
        'source_dir': r'X:\素材',
        'source_pattern': '{name} - 第{ep}集.mp4',
        'output_dir': out_dir,
        'artifacts': {'video_path': os.path.join(out_dir, f'测试剧_{vid}_打脸_去重版.mp4')},
        'videos': [{'id': vid, 'hook_keyword': '打脸', 'title': 't', 'description': 'd',
                    'hashtags': [], 'segments': segments or [{'episode': 1, 'start': 0.2, 'duration': 43.0}]}],
    }
    p = os.path.join(out_dir, f'edl_config_v{vid}.json')
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    # _run_render_one 成功分支按 '_{vid}_' 在 out_dir 里找渲染产物，测试需造出来
    mp4 = os.path.join(out_dir, f'测试剧_{vid}_打脸_去重版.mp4')
    open(mp4, 'w').close()
    return p


class TestCleanPartialOutputsKeepConfig(unittest.TestCase):
    def test_include_configs_false_keeps_config_deletes_mp4(self):
        p, _ = _mk()
        with tempfile.TemporaryDirectory() as d:
            cfg = _write_config(d, 1)
            mp4 = os.path.join(d, '测试剧_1_打脸_去重版.mp4')
            open(mp4, 'w').close()
            removed = p._clean_partial_outputs(d, include_configs=False)
            self.assertEqual(removed, 1)
            self.assertTrue(os.path.exists(cfg), 'edl_config 应保留')
            self.assertFalse(os.path.exists(mp4), 'mp4 半成品应删除')

    def test_default_still_deletes_config(self):
        """默认 include_configs=True 保持原语义（调用方必须显式传 False）"""
        p, _ = _mk()
        with tempfile.TemporaryDirectory() as d:
            cfg = _write_config(d, 1)
            removed = p._clean_partial_outputs(d)
            self.assertEqual(removed, 2)  # config + 测试 mp4
            self.assertFalse(os.path.exists(cfg))


class TestReRenderFromConfigs(unittest.TestCase):
    def test_no_config_returns_empty(self):
        p, _ = _mk()
        with tempfile.TemporaryDirectory() as d:
            videos = p._re_render_from_configs('测试剧', d)
            self.assertEqual(videos, [])

    def test_rerenders_each_config(self):
        p, _ = _mk()
        with tempfile.TemporaryDirectory() as d:
            _write_config(d, 1)
            _write_config(d, 2)
            # 渲染成功：mock subprocess.run 返回 rc=0
            with mock.patch('processor.subprocess.run') as mr, \
                 mock.patch.object(p, '_update_config_artifacts'):
                mr.return_value = mock.MagicMock(returncode=0, stderr='')
                videos = p._re_render_from_configs('测试剧', d)
            self.assertEqual(len(videos), 2)
            self.assertEqual(videos[0]['type'], 'edl')

    def test_partial_failure_keeps_success(self):
        p, _ = _mk()
        with tempfile.TemporaryDirectory() as d:
            _write_config(d, 1)
            _write_config(d, 2)
            def fake_run(cmd, **kw):
                m = mock.MagicMock()
                m.returncode = 1 if 'edl_config_v1' in cmd[-1] else 0
                m.stderr = 'boom'
                return m
            with mock.patch('processor.subprocess.run', side_effect=fake_run), \
                 mock.patch.object(p, '_update_config_artifacts'):
                videos = p._re_render_from_configs('测试剧', d)
            self.assertEqual(len(videos), 1, 'V1 失败应跳过，V2 成功')

    def test_manual_cut_resume_falls_back_to_full(self):
        """无保留 config → manual_cut(resume=True) 回退全量（不抛）"""
        p, logger = _mk()
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, '测试剧')
            os.makedirs(src)
            # 素材 2 个假 mp4（manual_cut 前置校验：>=2 集）
            for ep in (1, 2):
                open(os.path.join(src, f'测试剧 - 第{ep}集.mp4'), 'w').close()
            # 素材目录需在 CONFIG['download_base_dir'] 下 —— mock CONFIG
            with mock.patch('processor.CONFIG', {
                'download_base_dir': d,
                'output_dir': os.path.join(d, 'output'),
                'scripts_dir': os.path.dirname(os.path.dirname(__file__)),
            }), mock.patch.object(p, 'cut_batch', return_value=[]) as mcut:
                # 直接验证 cut_batch 收到 resume=True 且不抛（回退发生在 _cut_once 内，
                # 这里用 mock cut_batch 只验证透传）
                r = p.manual_cut('测试剧', resume=True)
            mcut.assert_called_once()
            self.assertEqual(mcut.call_args.kwargs.get('resume'), True)


class TestEpisodeRealDuration(unittest.TestCase):
    def test_probe_wins(self):
        """probe 成功 → 用 probe（权威），不用转写时长"""
        p, _ = _mk()
        with mock.patch.object(p, '_get_video_duration', return_value=36.56):
            d = p._episode_real_duration('x.mp4', [{'end': 59.98}, {'end': 12.0}])
        self.assertAlmostEqual(d, 36.56)

    def test_probe_fail_falls_back_to_transcript(self):
        """probe 失败（返回 0）→ 回退转写 last end，保底不丢集"""
        p, _ = _mk()
        with mock.patch.object(p, '_get_video_duration', return_value=0):
            d = p._episode_real_duration('x.mp4', [{'end': 59.98}, {'end': 12.0}])
        self.assertAlmostEqual(d, 59.98)


if __name__ == '__main__':
    unittest.main()
