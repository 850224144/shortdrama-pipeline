# -*- coding: utf-8 -*-
"""4.1 配置加载分层单测：默认回退 / yml 深合并 / 环境变量优先 / 坏 yml 兜底。"""
import os
import tempfile
import unittest
from unittest import mock

import config
from config import (
    DEFAULT_CONFIG,
    _deep_merge,
    _load_yaml,
    load_config,
    apply_env_overrides,
)


def _write_yaml(text):
    fd, path = tempfile.mkstemp(suffix='.yml')
    os.close(fd)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(text)
    return path


class ConfigEnvIsolation(unittest.TestCase):
    """所有用例清空 SHORTDRAMA_* 环境变量，避免本机环境干扰分层判定。"""

    def setUp(self):
        self._env_patch = mock.patch.dict(
            os.environ,
            {k: v for k, v in os.environ.items() if not k.startswith('SHORTDRAMA_')},
            clear=False,
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()


class TestDefaultFallback(ConfigEnvIsolation):
    def test_missing_yaml_keeps_defaults(self):
        cfg = load_config(r'D:\nonexistent-shortdrama-config.yml')
        # ffprobe_path 由 apply_env_overrides 运行时从 ffmpeg_dir 推导，属补充键。
        self.assertTrue(set(DEFAULT_CONFIG.keys()).issubset(set(cfg.keys())))
        self.assertEqual(cfg['download']['max_workers'], DEFAULT_CONFIG['download']['max_workers'])
        self.assertEqual(cfg['quality']['min_duration'], DEFAULT_CONFIG['quality']['min_duration'])

    def test_load_yaml_missing_file_returns_empty(self):
        self.assertEqual(_load_yaml(r'D:\no-such-config.yml'), {})


class TestYamlDeepMerge(ConfigEnvIsolation):
    def test_nested_merge_keeps_unspecified_defaults(self):
        yml = _write_yaml(
            "download:\n  max_workers: 5\n"
            "quality:\n  min_size_mb: 20\n"
        )
        try:
            cfg = load_config(yml)
            # 被覆盖的标量
            self.assertEqual(cfg['download']['max_workers'], 5)
            self.assertEqual(cfg['quality']['min_size_mb'], 20)
            # 同段未覆盖的默认键保留
            self.assertEqual(cfg['download']['timeout'], DEFAULT_CONFIG['download']['timeout'])
            self.assertEqual(cfg['quality']['min_duration'], DEFAULT_CONFIG['quality']['min_duration'])
            # 未触及的顶层段原样保留
            self.assertEqual(cfg['edl']['count'], DEFAULT_CONFIG['edl']['count'])
        finally:
            os.remove(yml)

    def test_scalar_replaces_dict_when_shadowed(self):
        yml = _write_yaml("edl: 123\n")
        try:
            cfg = load_config(yml)
            self.assertEqual(cfg['edl'], 123)
        finally:
            os.remove(yml)


class TestEnvOverridesWins(ConfigEnvIsolation):
    def test_env_beats_yaml_and_default(self):
        yml = _write_yaml("data_dir: D:/from-yml\n")
        try:
            with mock.patch.dict(os.environ, {'SHORTDRAMA_DATA_DIR': 'D:/from-env'}):
                cfg = load_config(yml)
            self.assertEqual(cfg['data_dir'], 'D:/from-env')
        finally:
            os.remove(yml)

    def test_apply_env_counts_applied(self):
        cfg = {'data_dir': 'default', 'ffmpeg_dir': r'D:\ffmpeg\bin'}
        with mock.patch.dict(os.environ, {'SHORTDRAMA_LOG_DIR': 'D:/logs'}):
            applied = apply_env_overrides(cfg)
        self.assertEqual(cfg['log_dir'], 'D:/logs')
        self.assertGreaterEqual(applied, 1)

    def test_ffprobe_inferred_from_ffmpeg_dir(self):
        cfg = {'ffmpeg_dir': r'D:\ffmpeg\bin', 'ffprobe_path': ''}
        apply_env_overrides(cfg)
        self.assertEqual(cfg['ffprobe_path'], os.path.join(r'D:\ffmpeg\bin', 'ffprobe.exe'))


class TestBadYamlFallback(ConfigEnvIsolation):
    def test_invalid_yaml_falls_back_to_default(self):
        yml = _write_yaml("download:\n  max_workers: [unclosed\n")
        try:
            cfg = load_config(yml)
            self.assertEqual(cfg['download']['max_workers'], DEFAULT_CONFIG['download']['max_workers'])
        finally:
            os.remove(yml)

    def test_non_dict_yaml_returns_empty(self):
        yml = _write_yaml("- 1\n- 2\n- 3\n")
        try:
            self.assertEqual(_load_yaml(yml), {})
        finally:
            os.remove(yml)


class TestDeepMergePure(unittest.TestCase):
    def test_does_not_mutate_base(self):
        base = {'a': 1, 'nested': {'x': 1, 'y': 2}}
        override = {'nested': {'x': 9}, 'b': 2}
        merged = _deep_merge(base, override)
        self.assertEqual(base['nested']['x'], 1)
        self.assertEqual(merged['nested']['x'], 9)
        self.assertEqual(merged['nested']['y'], 2)
        self.assertEqual(merged['b'], 2)

    def test_returns_new_dict(self):
        base = {'a': 1}
        merged = _deep_merge(base, {'a': 2})
        self.assertIsNot(merged, base)


class TestResetSingleton(ConfigEnvIsolation):
    def test_reset_config_rebuilds(self):
        config.reset_config()
        first = config.get_config()
        second = config.reset_config()
        self.assertIsNot(first, second)
        self.assertTrue(set(DEFAULT_CONFIG.keys()).issubset(set(second.keys())))
        config.reset_config()


if __name__ == '__main__':
    unittest.main()
