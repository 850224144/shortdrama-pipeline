# -*- coding: utf-8 -*-
"""pytest 公共配置与 fixture（阶段一 4.5 测试分层）。

职责：
- 统一把 ``webapp`` 根目录加入 ``sys.path``，子目录（unit/integration）用例
  无需各自写 ``sys.path.insert``；各用例文件内既有的路径插入保留也无害。
- 提供跨层复用的 fixture：临时目录、内存 ``JobStore``、隔离配置单例。

分层约定：
- ``tests/unit/``：纯函数 / 内存 / 临时 sqlite / mock，不起浏览器、不跑 ffmpeg
  子进程、不依赖真实素材，可秒级跑完。
- ``tests/integration/``：manager 线程协作、子进程、完整链路、浏览器上传等。
- ``tests/manual/``：需人工配合（登录态、真实素材、浏览器手动操作）的冒烟脚本，
  不进 pytest 默认收集，直接 ``python tests/manual/xxx.py`` 运行。
"""
import os
import shutil
import sys
import tempfile

# webapp 根目录（本文件位于 webapp/tests/conftest.py）
_WEBAPP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WEBAPP_ROOT not in sys.path:
    sys.path.insert(0, _WEBAPP_ROOT)

# 项目根 scripts 目录（render_generic 等脚本工具，供 loudnorm / boundary 等用例导入）
_PROJECT_ROOT = os.path.dirname(_WEBAPP_ROOT)
_SCRIPTS_DIR = os.path.join(_PROJECT_ROOT, 'scripts')
if os.path.isdir(_SCRIPTS_DIR) and _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import pytest  # noqa: E402

from job_store import JobStore  # noqa: E402
from config import reset_config  # noqa: E402


@pytest.fixture
def tmp_dir():
    """独立临时目录，用例结束后整体清理。"""
    path = tempfile.mkdtemp(prefix='sd_test_')
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def memory_store():
    """进程内内存 sqlite ``JobStore``，不落盘、跨线程由内部锁串行化。"""
    store = JobStore(':memory:')
    yield store


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    """隔离配置单例：清空 ``SHORTDRAMA_*`` 环境变量、指向空 yml，用例结束复原。

    用例内可直接改返回的 dict，或再调 ``reset_config(yml_path)`` 切换；
    teardown 统一 ``reset_config()`` 回到默认单例，避免污染其他用例。
    """
    for env_key in list(os.environ):
        if env_key.startswith('SHORTDRAMA_'):
            monkeypatch.delenv(env_key, raising=False)
    yml = tmp_path / 'config.yml'
    yml.write_text('', encoding='utf-8')
    cfg = reset_config(str(yml))
    yield cfg
    reset_config()
