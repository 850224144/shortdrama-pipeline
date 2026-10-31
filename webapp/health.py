# -*- coding: utf-8 -*-
"""配置加载与健康检查（R5）。

- `apply_env_overrides(config)`：用环境变量覆盖 CONFIG 路径/密钥（进程内原地修改，
  所有后续读取 CONFIG['x'] 的调用立即生效）。
- `run_health_checks(config)`：启动/手动验证目录、ffmpeg/ffprobe、ASR、BaiduPCS-Go、
  磁盘和 NVENC 探测；每项返回 {name, level(ok/degraded/failed), detail, suggestion}。
- `GET /api/health` 区分 ok/degraded/failed。
"""

import os
import shutil
import subprocess
import sys

# 环境变量 → (CONFIG key, 描述)。值非空才覆盖。
ENV_OVERRIDES = {
    'SHORTDRAMA_FFMPEG': ('ffmpeg_path', 'ffmpeg 可执行路径'),
    'SHORTDRAMA_FFPROBE': ('ffprobe_path', 'ffprobe 可执行路径'),
    'SHORTDRAMA_DOWNLOAD_DIR': ('download_base_dir', '素材根目录'),
    'SHORTDRAMA_OUTPUT_DIR': ('output_dir', '成片输出根目录'),
    'SHORTDRAMA_DATA_DIR': ('data_dir', '数据目录'),
    'SHORTDRAMA_LOG_DIR': ('log_dir', '日志目录'),
    'SHORTDRAMA_BAIDUPCS': ('baidupcs_path', 'BaiduPCS-Go 可执行路径'),
    'SHORTDRAMA_ASR_SCRIPT': ('asr_script', 'ASR 脚本路径'),
    'SHORTDRAMA_SCRIPTS_DIR': ('scripts_dir', '脚本目录'),
}

# 关键路径映射：从 CONFIG key 派生需要检查的文件
_PATH_KEYS = ['baidupcs_path', 'ffmpeg_path', 'ffprobe_path', 'asr_script']


def apply_env_overrides(config):
    """应用环境变量覆盖（返回覆盖数量；幂等）。"""
    applied = 0
    for env_key, (cfg_key, _desc) in ENV_OVERRIDES.items():
        value = os.environ.get(env_key)
        if value:
            config[cfg_key] = value
            applied += 1
    # ffprobe 未显式覆盖时由 ffmpeg_dir 推导
    if not config.get('ffprobe_path') and config.get('ffmpeg_dir'):
        config['ffprobe_path'] = os.path.join(config['ffmpeg_dir'], 'ffprobe.exe')
    return applied


def _probe_exe(path, args=None, timeout=10):
    """返回 (ok, detail)。执行可执行并捕获输出/错误。"""
    if not path or not os.path.isfile(path):
        return False, f'文件不存在: {path}'
    cmd = [path] + (args or ['-version'])
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        out = (proc.stdout or b'').decode('utf-8', 'replace').strip()
        err = (proc.stderr or b'').decode('utf-8', 'replace').strip()
        first = (out or err).splitlines()[0] if (out or err) else ''
        if proc.returncode == 0:
            return True, (first or f'可执行正常（rc=0）')
        return False, f'执行失败 rc={proc.returncode}: {first}'
    except FileNotFoundError:
        return False, f'无法启动: {path}'
    except subprocess.TimeoutExpired:
        return False, f'执行超时: {path}'
    except Exception as exc:  # noqa: BLE001
        return False, f'执行异常: {exc}'


def _check_dir(name, path, required=True, create=False):
    ok = bool(path)
    detail = str(path)
    if ok and not os.path.exists(path):
        if create:
            try:
                os.makedirs(path, exist_ok=True)
                detail = f'{path}（已创建）'
            except OSError as exc:
                ok, detail = False, f'{path} 创建失败: {exc}'
        elif required:
            ok, detail = False, f'{path} 不存在'
    elif ok and not os.path.isdir(path):
        ok, detail = False, f'{path} 不是目录'
    level = 'ok' if ok else ('failed' if required else 'degraded')
    return {'name': name, 'level': level, 'detail': detail,
            'suggestion': '' if ok else f'检查/创建目录: {path}'}


def _check_file(name, path, required=True):
    ok = bool(path) and os.path.isfile(path)
    level = 'ok' if ok else ('failed' if required else 'degraded')
    return {'name': name, 'level': level, 'detail': str(path),
            'suggestion': '' if ok else f'确认文件存在: {path}'}


def _check_disk(config):
    out_dir = config.get('output_dir')
    min_gb = config.get('resource', {}).get('min_disk_gb', 100)
    if not out_dir or not os.path.isdir(out_dir):
        return {'name': '磁盘空间', 'level': 'degraded',
                'detail': f'输出目录不存在: {out_dir}',
                'suggestion': '输出目录不存在，无法评估磁盘空间'}
    try:
        free_gb = shutil.disk_usage(os.path.abspath(out_dir)).free / (1024 ** 3)
    except OSError as exc:
        return {'name': '磁盘空间', 'level': 'degraded', 'detail': str(exc),
                'suggestion': '检查输出目录所在盘符'}
    ok = free_gb >= min_gb
    return {'name': '磁盘空间', 'level': 'ok' if ok else 'degraded',
            'detail': f'可用 {free_gb:.1f} GB（阈值 {min_gb} GB）',
            'suggestion': '' if ok else f'释放空间至 {min_gb} GB 以上'}


def _check_nvenc(config):
    ffmpeg = config.get('ffmpeg_path')
    if not ffmpeg or not os.path.isfile(ffmpeg):
        return {'name': 'NVENC 编码器', 'level': 'degraded',
                'detail': 'ffmpeg 不可用，无法探测', 'suggestion': '安装 ffmpeg'}
    try:
        proc = subprocess.run([ffmpeg, '-hide_banner', '-encoders'],
                              capture_output=True, timeout=20)
        text = (proc.stdout or b'').decode('utf-8', 'replace')
        has = 'nvenc' in text.lower()
        # 渲染可回退 libx264，故 NVENC 缺失仅是 degraded
        return {'name': 'NVENC 编码器', 'level': 'ok' if has else 'degraded',
                'detail': '可用' if has else '未检测到 NVENC（将回退 libx264）',
                'suggestion': '' if has else 'GPU 渲染不可用，将使用 CPU 编码'}
    except Exception as exc:  # noqa: BLE001
        return {'name': 'NVENC 编码器', 'level': 'degraded',
                'detail': f'探测失败: {exc}', 'suggestion': '检查 ffmpeg'}


def _check_ai_keys(config):
    """AI 文案密钥：从环境变量读取，缺失仅 degraded（模板兜底）。"""
    keys = [os.environ.get('ZHIPU_API_KEY'),
            os.environ.get('GLM_API_KEY'),
            os.environ.get('SENSENOVA_API_KEY'),
            os.environ.get('SENSENOVA_ACCESS_KEY')]
    has = any(k for k in keys if k)
    return {'name': 'AI 文案密钥', 'level': 'ok' if has else 'degraded',
            'detail': '已配置' if has else '未检测到 ZHIPU/SenseNova 密钥（将使用模板兜底）',
            'suggestion': '' if has else '配置 ZHIPU_API_KEY 或 SENSENOVA_API_KEY'}


def run_health_checks(config):
    """运行全部健康检查，返回 {status, checks}。"""
    checks = [
        _check_dir('数据目录', config.get('data_dir'), create=True),
        _check_dir('输出目录', config.get('output_dir'), create=True),
        _check_dir('日志目录', config.get('log_dir'), create=True),
        _check_dir('素材目录', config.get('download_base_dir'), create=True),
    ]
    # 可执行文件
    for key in ('ffmpeg_path', 'ffprobe_path'):
        ok, detail = _probe_exe(config.get(key))
        checks.append({'name': key.replace('_path', '').upper(),
                       'level': 'ok' if ok else 'failed',
                       'detail': detail,
                       'suggestion': '' if ok else f'确认 {key} 指向有效 ffmpeg'})
    # 关键脚本/二进制（下载与 ASR 依赖）
    for key, label in (('baidupcs_path', 'BaiduPCS-Go'),
                       ('asr_script', 'ASR 脚本')):
        checks.append(_check_file(label, config.get(key), required=False))
    # 磁盘 / NVENC / AI 密钥
    checks.append(_check_disk(config))
    checks.append(_check_nvenc(config))
    checks.append(_check_ai_keys(config))

    # 汇总：有 failed → failed；否则有 degraded → degraded；否则 ok
    levels = [c['level'] for c in checks]
    if 'failed' in levels:
        status = 'failed'
    elif 'degraded' in levels:
        status = 'degraded'
    else:
        status = 'ok'
    return {'status': status, 'checks': checks}


def apply_env_overrides_and_health(config):
    """便捷入口：应用环境覆盖并运行健康检查。"""
    applied = apply_env_overrides(config)
    health = run_health_checks(config)
    health['env_overrides_applied'] = applied
    return health
