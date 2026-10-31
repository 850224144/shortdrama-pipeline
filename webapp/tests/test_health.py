# -*- coding: utf-8 -*-
"""R5 配置与健康检查：环境变量覆盖 + 启动检查分类。

验收（设计文档 R5）：
- /api/health 可用；缺依赖时启动后立即能看出问题；配置可被环境变量覆盖。
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from health import (apply_env_overrides, run_health_checks, _check_dir,
                    _check_file, _check_disk)


def _base_config(tmp):
    """构造一个依赖齐全的配置：ffmpeg/ffprobe 指向本 python（-version 通过）。"""
    return {
        'ffmpeg_path': sys.executable,
        'ffprobe_path': sys.executable,
        'ffmpeg_dir': os.path.dirname(sys.executable),
        'baidupcs_path': os.path.join(tmp, 'BaiduPCS-Go.exe'),
        'asr_script': os.path.join(tmp, 'transcribe_dir.py'),
        'download_base_dir': os.path.join(tmp, '素材'),
        'output_dir': os.path.join(tmp, 'output'),
        'data_dir': os.path.join(tmp, 'data'),
        'log_dir': os.path.join(tmp, 'logs'),
        'resource': {'min_disk_gb': 0},
    }


class EnvOverrideTests(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.pop(k, None)
                     for k in ('SHORTDRAMA_FFMPEG', 'SHORTDRAMA_OUTPUT_DIR',
                               'SHORTDRAMA_FFPROBE', 'SHORTDRAMA_DATA_DIR')}

    def tearDown(self):
        for k, v in self._env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_env_overrides_apply_paths(self):
        cfg = {'ffmpeg_dir': r'X:\\bin', 'ffmpeg_path': 'orig'}
        os.environ['SHORTDRAMA_FFMPEG'] = r'C:\\custom\\ffmpeg.exe'
        os.environ['SHORTDRAMA_OUTPUT_DIR'] = r'D:\\custom\\out'
        applied = apply_env_overrides(cfg)
        self.assertEqual(applied, 2)
        self.assertEqual(cfg['ffmpeg_path'], r'C:\\custom\\ffmpeg.exe')
        self.assertEqual(cfg['output_dir'], r'D:\\custom\\out')

    def test_ffprobe_derived_when_not_overridden(self):
        cfg = {'ffmpeg_dir': r'X:\\bin', 'ffmpeg_path': 'ff', 'output_dir': 'o',
               'data_dir': 'd', 'log_dir': 'l', 'download_base_dir': 'm'}
        apply_env_overrides(cfg)
        self.assertEqual(cfg['ffprobe_path'], os.path.join(r'X:\\bin', 'ffprobe.exe'))

    def test_env_overrides_idempotent(self):
        cfg = {}
        os.environ['SHORTDRAMA_FFMPEG'] = 'f'
        first = apply_env_overrides(cfg)
        second = apply_env_overrides(cfg)
        self.assertEqual(first, 1)
        self.assertEqual(second, 1, '重复应用不累积计数')


class HealthCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='health_')
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_missing_ffmpeg_is_failed(self):
        cfg = _base_config(self.tmp)
        cfg['ffmpeg_path'] = os.path.join(self.tmp, '不存在_ffmpeg.exe')
        cfg['ffprobe_path'] = os.path.join(self.tmp, '不存在_ffprobe.exe')
        with mock.patch.dict(os.environ, {}, clear=False):
            res = run_health_checks(cfg)
        self.assertEqual(res['status'], 'failed')
        names = {c['name'] for c in res['checks']}
        self.assertIn('FFMPEG', names)
        self.assertIn('FFPROBE', names)
        ffmpeg = next(c for c in res['checks'] if c['name'] == 'FFMPEG')
        self.assertEqual(ffmpeg['level'], 'failed')

    def test_health_reports_all_check_names(self):
        cfg = _base_config(self.tmp)
        # 造依赖文件
        for p in (cfg['baidupcs_path'], cfg['asr_script']):
            open(p, 'w').close()
        for d in ('download_base_dir', 'output_dir', 'data_dir', 'log_dir'):
            os.makedirs(cfg[d], exist_ok=True)

        def fake_run(cmd, **kw):
            m = mock.MagicMock()
            m.returncode = 0
            args = ' '.join(str(a) for a in cmd)
            if 'encoders' in args:
                m.stdout = b'h264_nvenc ...'
            else:
                m.stdout = b'ffmpeg version n6.0'
            m.stderr = b''
            return m

        with mock.patch('health.subprocess.run', side_effect=fake_run), \
             mock.patch.dict(os.environ, {}, clear=False):
            res = run_health_checks(cfg)
        names = {c['name'] for c in res['checks']}
        for expected in ('数据目录', '输出目录', '日志目录', '素材目录',
                         'FFMPEG', 'FFPROBE', 'BaiduPCS-Go', 'ASR 脚本',
                         '磁盘空间', 'NVENC 编码器', 'AI 文案密钥'):
            self.assertIn(expected, names, f'缺少检查项: {expected}')
        # 依赖齐全（mock nvenc 可用）但无 AI 密钥 → 至少 degraded，绝不 failed
        self.assertNotEqual(res['status'], 'failed')
        self.assertIn(res['status'], ('ok', 'degraded'))

    def test_check_dir_create_when_required(self):
        target = os.path.join(self.tmp, 'new_dir')
        r = _check_dir('测试目录', target, create=True)
        self.assertEqual(r['level'], 'ok')
        self.assertTrue(os.path.isdir(target))

    def test_check_file_missing_is_degraded_when_optional(self):
        r = _check_file('测试文件', os.path.join(self.tmp, 'nope.exe'),
                        required=False)
        self.assertEqual(r['level'], 'degraded')
        self.assertTrue(r['suggestion'])

    def test_check_disk_reports_free_space(self):
        cfg = _base_config(self.tmp)
        os.makedirs(cfg['output_dir'], exist_ok=True)
        cfg['resource']['min_disk_gb'] = 0
        r = _check_disk(cfg)
        self.assertEqual(r['level'], 'ok')
        self.assertIn('GB', r['detail'])


if __name__ == '__main__':
    unittest.main()
