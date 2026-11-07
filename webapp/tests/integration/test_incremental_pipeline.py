# -*- coding: utf-8 -*-
"""增量流水线回归：下载目录、源文件名、交付报告与锁。"""

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))), 'scripts'))

from processor import CONFIG, PipelineProcessor
from download_baidu import _flatten as flatten_cli_download


def _mk():
    return PipelineProcessor(mock.MagicMock(), mock.MagicMock())


def _sparse(path, size=6 * 1024 * 1024):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.seek(size - 1)
        f.write(b'x')


class TestDownloadNormalization(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='incremental_')
        self.old_data = CONFIG['data_dir']
        self.old_base = CONFIG['download_base_dir']
        CONFIG['data_dir'] = self.tmp
        CONFIG['download_base_dir'] = self.tmp

    def tearDown(self):
        CONFIG['data_dir'] = self.old_data
        CONFIG['download_base_dir'] = self.old_base
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_normalize_multiple_wrappers_and_preserve_business_dirs(self):
        p = _mk()
        target = os.path.join(self.tmp, '剧名')
        staging = os.path.join(target, '.incoming')
        nested = os.path.join(staging, '剧名', '剧名', '剧名')
        _sparse(os.path.join(nested, '第1集.mp4'))
        _sparse(os.path.join(nested, '第2集.mp4'))
        with open(os.path.join(nested, '封面图.jpg'), 'wb') as f:
            f.write(b'cover')
        transcribe = os.path.join(target, 'transcribe')
        os.makedirs(transcribe)
        with open(os.path.join(transcribe, 'cache.json'), 'w') as f:
            f.write('{}')

        with mock.patch.object(p, '_clean_partial_downloads', return_value=0):
            moved, quarantined = p._normalize_download_tree(
                staging, target, '剧名')

        self.assertEqual(moved, 3)
        self.assertEqual(quarantined, 0)
        self.assertTrue(os.path.isfile(os.path.join(target, '第1集.mp4')))
        self.assertTrue(os.path.isfile(os.path.join(target, '第2集.mp4')))
        self.assertTrue(os.path.isfile(os.path.join(target, '封面图.jpg')))
        self.assertTrue(os.path.isfile(os.path.join(transcribe, 'cache.json')))
        self.assertFalse(os.path.exists(staging))

    def test_duplicate_episode_is_quarantined_not_overwritten(self):
        p = _mk()
        target = os.path.join(self.tmp, '剧名')
        staging = os.path.join(target, '.incoming')
        os.makedirs(target)
        keep = os.path.join(target, '1.mp4')
        _sparse(keep)
        duplicate = os.path.join(staging, '剧名', '1.mp4')
        _sparse(duplicate, 7 * 1024 * 1024)

        with mock.patch.object(p, '_clean_partial_downloads', return_value=0):
            moved, quarantined = p._normalize_download_tree(
                staging, target, '剧名')

        self.assertEqual((moved, quarantined), (0, 1))
        self.assertEqual(os.path.getsize(keep), 6 * 1024 * 1024)
        qroot = os.path.join(self.tmp, 'download_quarantine')
        found = []
        for root, _, files in os.walk(qroot):
            found.extend(os.path.join(root, f) for f in files)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].endswith('1.mp4'))

    def test_baidu_sidecar_removes_partial_payload(self):
        p = _mk()
        d = os.path.join(self.tmp, '剧名')
        partial = os.path.join(d, '38.mp4')
        marker = partial + '.BaiduPCS-Go-downloading'
        _sparse(partial, 1024 * 1024)
        with open(marker, 'wb') as f:
            f.write(b'meta')
        complete = os.path.join(d, '39.mp4')
        _sparse(complete)

        removed = p._clean_partial_downloads(d)

        self.assertEqual(removed, 2)
        self.assertFalse(os.path.exists(partial))
        self.assertFalse(os.path.exists(marker))
        self.assertTrue(os.path.exists(complete))

    def test_scan_excludes_sidecar_and_staging(self):
        p = _mk()
        d = os.path.join(self.tmp, '剧名')
        partial = os.path.join(d, '38.mp4')
        _sparse(partial)
        with open(partial + '.BaiduPCS-Go-downloading', 'wb') as f:
            f.write(b'meta')
        _sparse(os.path.join(d, '.incoming', '39.mp4'))
        complete = os.path.join(d, '40.mp4')
        _sparse(complete)
        self.assertEqual(p._scan_files_recursive(d), [complete])

    def test_layout_validator_rejects_nested_media_and_markers(self):
        p = _mk()
        d = os.path.join(self.tmp, '剧名')
        _sparse(os.path.join(d, '剧名', '1.mp4'))
        marker = os.path.join(d, '2.mp4.BaiduPCS-Go-downloading')
        with open(marker, 'wb') as f:
            f.write(b'meta')
        errors = p._validate_download_layout(d)
        self.assertTrue(any('子目录' in e for e in errors))
        self.assertTrue(any('临时标记' in e for e in errors))

    def test_flatten_only_same_name_wrapper(self):
        p = _mk()
        target = os.path.join(self.tmp, '剧名')
        os.makedirs(os.path.join(target, '剧名', '剧名'))
        _sparse(os.path.join(target, '剧名', '剧名', '1.mp4'))
        transcribe = os.path.join(target, 'transcribe')
        os.makedirs(transcribe)
        with open(os.path.join(transcribe, 'EP1.json'), 'w') as f:
            f.write('[]')
        self.assertTrue(p._flatten_local_dir(target, '剧名'))
        self.assertTrue(os.path.isfile(os.path.join(target, '1.mp4')))
        self.assertTrue(os.path.isfile(os.path.join(transcribe, 'EP1.json')))

    def test_download_command_uses_staging_and_final_is_flat(self):
        p = _mk()
        link_info = {'link': 'https://pan.baidu.com/s/abc123?pwd=9999',
                     'link_id': 'abc123', 'pwd': '9999'}
        download_cmd = []

        def run_cmd(cmd, **kwargs):
            if cmd[1] == 'transfer':
                return SimpleNamespace(returncode=0,
                                       stdout='成功 保存了测试剧到当前目录', stderr='')
            if cmd[1] == 'download':
                download_cmd[:] = cmd
                save = cmd[cmd.index('--saveto') + 1]
                _sparse(os.path.join(save, '测试剧', '测试剧', '1.mp4'))
                _sparse(os.path.join(save, '测试剧', '测试剧', '2.mp4'))
                return SimpleNamespace(returncode=0, stdout='', stderr='')
            raise AssertionError(f'意外命令: {cmd}')

        def run_subprocess(cmd, **kwargs):
            if cmd[1] == 'ls' and cmd[-1] == '/短剧素材/测试剧':
                rows = [
                    '1            1  2026-11-02 10:00:00 1.mp4',
                    '1            1  2026-11-02 10:00:00 2.mp4',
                ]
                return SimpleNamespace(returncode=0, stdout='\n'.join(rows), stderr='')
            return SimpleNamespace(returncode=0, stdout='', stderr='')

        with mock.patch('archive.should_download', return_value=(True, 'ok')), \
             mock.patch.object(p, '_run_command_cancellable', side_effect=run_cmd), \
             mock.patch('processor.subprocess.run', side_effect=run_subprocess):
            result = p._download_once(link_info)

        self.assertTrue(result['success'], result)
        self.assertIn('--saveto', download_cmd)
        save = download_cmd[download_cmd.index('--saveto') + 1]
        self.assertEqual(os.path.basename(save), '.incoming')
        target = os.path.join(self.tmp, '测试剧')
        self.assertEqual(sorted(os.listdir(target)), ['1.mp4', '2.mp4'])

    def test_cli_flatten_detects_partial_marker(self):
        local = os.path.join(self.tmp, 'cli_target')
        staging = os.path.join(self.tmp, 'cli_staging')
        partial = os.path.join(staging, '剧名', '2.mp4')
        _sparse(partial, 1024 * 1024)
        with open(partial + '.BaiduPCS-Go-downloading', 'wb') as f:
            f.write(b'meta')
        complete = os.path.join(staging, '剧名', '1.mp4')
        _sparse(complete)

        moved, partial_count = flatten_cli_download(staging, local)

        self.assertEqual((moved, partial_count), (1, 1))
        self.assertTrue(os.path.isfile(os.path.join(local, '1.mp4')))
        self.assertFalse(os.path.exists(os.path.join(local, '2.mp4')))


class TestIncrementalReportsAndSourcePattern(unittest.TestCase):
    def test_source_pattern_for_common_names(self):
        p = _mk()
        cases = {
            '第10集.mp4': '第{ep}集.mp4',
            '第01集.mp4': '第{ep:02d}集.mp4',
            # 2026-11-05 修复：前缀用样例文件真实字面量（而非 {name} 占位符），
            # 剧名带序号前缀而文件名不带时不再失配；匹配结果与旧行为一致。
            '剧名 - 第10集.mp4': '剧名 - 第{ep}集.mp4',
            '剧名-第10集.mp4': '剧名-第{ep}集.mp4',
            '剧名第10集.mp4': '剧名第{ep}集.mp4',
            '剧名第01集.mp4': '剧名第{ep:02d}集.mp4',
            '10.mp4': '{ep}.mp4',
            '01.mp4': '{ep:02d}.mp4',
            '剧名-10.mp4': '剧名-{ep}.mp4',
            '剧名-01.mp4': '剧名-{ep:02d}.mp4',
            '剧名_10.mp4': '剧名_{ep}.mp4',
        }
        for filename, expected in cases.items():
            self.assertEqual(p._detect_source_pattern(filename), expected,
                             filename)

    def test_resume_repairs_stale_source_pattern(self):
        p = _mk()
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, 'src')
            out = os.path.join(d, 'out')
            os.makedirs(src)
            os.makedirs(out)
            _sparse(os.path.join(src, '第1集.mp4'))
            cfg_path = os.path.join(out, 'edl_config_v1.json')
            cfg = {
                'drama_name': '测试剧',
                'source_dir': src,
                'source_pattern': '{name}第{ep}集.mp4',
                'videos': [{'segments': []}],
            }
            with open(cfg_path, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False)
            verdict = {'status': 'render_failed', 'reason': 'other'}
            with mock.patch.object(
                    p, '_run_render_one',
                    return_value=(None, 'other', verdict)):
                p._re_render_from_configs('测试剧', out)
            with open(cfg_path, encoding='utf-8') as f:
                repaired = json.load(f)
            self.assertEqual(repaired['source_pattern'], '第{ep}集.mp4')
            self.assertEqual(repaired['source_dir'], src)

    def test_expected_count_allows_incomplete_batch_retry(self):
        p = _mk()
        p.state_manager.load.return_value = {'completed': []}
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, 'src')
            out = os.path.join(d, 'out')
            os.makedirs(src)
            os.makedirs(out)
            for i in range(1, 7):
                open(os.path.join(out, f'样例剧_{i}_真相_去重版.mp4'), 'wb').close()
            should, reason = p.should_process(
                '样例剧', src, out, expected_count=8)
            self.assertTrue(should, reason)
            should, reason = p.should_process(
                '样例剧', src, out, expected_count=6)
            self.assertFalse(should, reason)

    def test_resume_render_report_contains_verdicts(self):
        p = _mk()
        with tempfile.TemporaryDirectory() as d:
            cfg = {'videos': [{'segments': []}]}
            with open(os.path.join(d, 'edl_config_v1.json'), 'w',
                      encoding='utf-8') as f:
                json.dump(cfg, f)
            verdict = {'status': 'accepted', 'reason': ''}
            with mock.patch.object(p, '_run_render_one',
                                   return_value=(None, 'other', verdict)):
                p._re_render_from_configs('测试剧', d)
            report = open(os.path.join(d, '测试剧_渲染报告.md'),
                          encoding='utf-8').read()
        self.assertIn('V1 | ✅ ACCEPTED', report)
        self.assertIn('计划版本数: 1', report)

    def test_quality_rejection_does_not_stop_following_versions(self):
        p = _mk()
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, 'src')
            out = os.path.join(d, 'out')
            os.makedirs(src)
            os.makedirs(out)
            eps = []
            for n in (1, 2, 3):
                path = os.path.join(src, f'第{n}集.mp4')
                _sparse(path)
                eps.append(path)
            hl = [{'episode': n, 'path': path, 'dur': 60.0,
                   'cands': [{'score': 9, 'start': 0, 'dur': 20,
                              'src': 'llm', 'hook_type': '逆袭'}]}
                  for n, path in enumerate(eps, 1)]
            ai = mock.MagicMock()
            ai.enabled = True
            ai.generate_copy.side_effect = [
                {'title': f'标题{i}', 'description': f'描述{i}',
                 'hashtags': ['#测试']} for i in range(1, 4)]
            renders = [
                (None, 'quality', {'status': 'rejected', 'reason': '时长过短'}),
                ({'path': 'v2.mp4', 'final_path': 'v2.mp4',
                  'filename': '测试剧_2_真相_去重版.mp4', 'type': 'edl'},
                 None, {'status': 'accepted', 'reason': ''}),
                ({'path': 'v3.mp4', 'final_path': 'v3.mp4',
                  'filename': '测试剧_3_逆袭_去重版.mp4', 'type': 'edl'},
                 None, {'status': 'accepted', 'reason': ''}),
            ]
            plan = [
                {'opening_ep': 1, 'angle': '逆袭', 'order_type': 'reverse'},
                {'opening_ep': 2, 'angle': '真相', 'order_type': 'reverse'},
                {'opening_ep': 3, 'angle': '打脸', 'order_type': 'reverse'},
            ]
            with mock.patch('processor.AICopyGenerator', return_value=ai), \
                 mock.patch.object(p, '_load_episode_highlights', return_value=hl), \
                 mock.patch.object(p, '_analyze_drama_arc', return_value=plan), \
                 mock.patch.object(
                     p, '_generate_reverse_segments',
                     side_effect=lambda _eps, idx, **kw: [
                         {'episode': idx + 1, 'start': 0, 'duration': 20}]), \
                 mock.patch.object(p, '_run_render_one', side_effect=renders) as mr:
                videos = p._generate_edl_versions(
                    '测试剧', src, eps, 3, out, use_reverse=True)
            self.assertEqual(len(videos), 2)
            self.assertEqual(mr.call_count, 3, 'V1 拒收后仍应执行 V2/V3')
            report = open(os.path.join(out, '测试剧_渲染报告.md'),
                          encoding='utf-8').read()
        self.assertIn('V1 | ⛔ REJECTED（时长过短）', report)
        self.assertIn('V2 | ✅ ACCEPTED', report)
        self.assertIn('V3 | ✅ ACCEPTED', report)

    def test_render_report_lists_unattempted_versions(self):
        p = _mk()
        with tempfile.TemporaryDirectory() as d:
            p._write_render_report(d, '测试剧', {
                0: {'status': 'accepted', 'reason': ''},
                1: {'status': 'rejected', 'reason': '时长过短'},
            }, expected_count=3)
            report = open(os.path.join(d, '测试剧_渲染报告.md'),
                          encoding='utf-8').read()
        self.assertIn('V1 | ✅ ACCEPTED', report)
        self.assertIn('V2 | ⛔ REJECTED（时长过短）', report)
        self.assertIn('V3 | ⚪ NOT_RENDERED', report)

    def test_delivery_report_uses_only_real_cover(self):
        p = _mk()
        with tempfile.TemporaryDirectory() as d:
            covers = os.path.join(d, 'covers')
            os.makedirs(covers)
            real_cover = os.path.join(covers, '测试剧_1_封面.jpg')
            with open(real_cover, 'wb') as f:
                f.write(b'jpg')
            cfg = {
                'artifacts': {'cover_path': os.path.join(covers, '不存在.jpg')},
                'videos': [{'title': 't', 'description': 'd', 'hashtags': []}],
            }
            with open(os.path.join(d, 'edl_config_v1.json'), 'w',
                      encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False)
            video = {'filename': '测试剧_1_打脸_去重版.mp4'}
            p._write_report(d, '测试剧', [video], '测试')
            report = open(os.path.join(d, '测试剧_交付报告.md'),
                          encoding='utf-8').read()
        self.assertIn('covers/测试剧_1_封面.jpg', report)
        self.assertNotIn('不存在.jpg', report)

    def test_rejected_video_survives_cleanup(self):
        p = _mk()
        with tempfile.TemporaryDirectory() as d:
            accepted = os.path.join(d, '测试剧_1_打脸_去重版.mp4')
            rejected = os.path.join(d, '测试剧_2_真相_去重版_REJECTED.mp4')
            open(accepted, 'wb').close()
            open(rejected, 'wb').close()
            p._clean_partial_outputs(
                d, keep_filenames={os.path.basename(accepted)},
                include_configs=False)
            self.assertTrue(os.path.exists(accepted))
            self.assertTrue(os.path.exists(rejected))

    def test_lock_release_removes_file(self):
        p = _mk()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, '.processing.lock')
            lock = p._acquire_lock(path)
            self.assertIsNotNone(lock)
            self.assertTrue(os.path.exists(path))
            p._release_lock(lock)
            self.assertFalse(os.path.exists(path))


if __name__ == '__main__':
    unittest.main()
