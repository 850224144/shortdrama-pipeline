# -*- coding: utf-8 -*-
"""集中配置加载（阶段一 4.1 配置外置）。

分层优先级（高 → 低）：
  1. 环境变量 ``SHORTDRAMA_*``（机器路径 / 可执行文件，换机器改这里）
  2. ``config.yml``（业务参数，嵌套结构；文件缺失或解析失败时回退默认）
  3. ``DEFAULT_CONFIG``（内置默认值，与重构前 ``processor.CONFIG`` 逐键一致）

对外暴露模块级单例 ``CONFIG``（dict），同时保留现有两种读法：
``CONFIG['key']`` 下标访问与 ``CONFIG.get('key', default)``，调用方零改动。

AI 密钥（SENSENOVA / ZHIPU 等）仍由 ``ai_copy_generator`` 读取项目根 ``.env``，
本模块不接管密钥加载，避免双重解析。
"""
import copy
import os

try:
    import yaml  # PyYAML
except ImportError:  # pragma: no cover - 环境已装，保留兜底
    yaml = None

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_YML_PATH = os.path.join(_PROJECT_ROOT, 'config.yml')

# 环境变量 → CONFIG key（搬自 health.ENV_OVERRIDES；值非空才覆盖）。
# 机器相关路径集中在此映射，业务参数不在此列。
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

# 内置默认配置（与重构前 processor.py 内 CONFIG 逐键一致；缺省开箱行为不变）。
DEFAULT_CONFIG = {
    'baidupcs_path': r'D:\短剧工坊\素材\BaiduPCS-Go\BaiduPCS-Go-v4.0.2-windows-x64\BaiduPCS-Go.exe',
    'download_base_dir': r'E:\短剧素材',
    'scripts_dir': r'D:\短剧工坊\scripts',
    'output_dir': r'D:\短剧工作区\output',
    'data_dir': r'D:\短剧工坊\data',
    'log_dir': r'D:\短剧工坊\logs',
    'cache_path': r'D:\短剧工坊\data\dramaid_cache.json',
    'state_file': r'D:\短剧工坊\data\pipeline_state.json',
    'temp_dir': os.environ.get('TEMP', r'C:\Users\86159\AppData\Local\Temp'),
    'ffmpeg_path': r'D:\ffmpeg-master-latest-win64-gpl-shared\bin\ffmpeg.exe',
    'ffmpeg_dir': r'D:\ffmpeg-master-latest-win64-gpl-shared\bin',
    'asr_script': r'D:\短剧工坊\webapp\transcribe_dir.py',

    'download': {
        'max_workers': 2,  # 同一百度账号CLI并发不宜过高，5会冲突
        'max_retries': 3,
        'timeout': 1800,
        'stall_timeout': 300,  # P1: 无新文件产出达此秒数判卡顿，杀进程并重试
        'transfer_timeout': 120,
        'delete_timeout': 60,
        'default_pwd': '9999',  # 默认提取码
        'min_manual_files': 2,  # 手动素材最少文件数
        'min_download_files': 10,  # 已下载最少文件数
        'min_completeness': 0.5,  # P7: 完成度低于此比例视为残缺，跳过切片并重试
        'resume_on_startup': True,  # P3: 启动时恢复崩溃遗留的下载任务
    },

    'edl': {
        'min_duration': 180,
        'max_duration': 300,
        'count': 5,
        'retry': 2,
        'segment_duration': [30, 75],
        # 非重叠分段 + 跨版本 (集,起点) 去重 + 素材容量版本数。
        # 独立素材不足时减少版本数，不用高比例复用硬凑 6/8 版。
        'non_overlap': True,      # 每集切互不重叠段（替代 LLM 重叠窗口）
        'dedup_segments': True,   # 跨版本禁止复用同一 (集,起点)
        'segment_dur': 20.0,      # 非重叠段长（s）
        'target_dur': 200.0,      # 每版目标时长（s）
        'min_versions': 1,
        'max_versions': 8,
        # 默认禁止跨版本复用；只有显式开启 allow_controlled_reuse 才使用
        # reuse_ratio。历史实现默认 0.9，会把同剧后段版本变成近似复刻。
        'allow_controlled_reuse': False,
        'reuse_ratio': 0.0,
    },

    # 成品时长：剧情驱动 + 随机（替代固定 target_dur）。
    # enabled=False → 退回 edl.target_dur 固定行为。min_final/max_final 是
    # 成片秒数下限/上限（上限 1200 = 20 分钟）。剧情量 = 导演方案叙事单元数
    # × per_unit + 弧线跨度 × per_spread + 角度加成，每版加 jitter 随机抖动，
    # 避免所有版本同长（对账号/平台不友好）。渲染会 atempo=1.05 轻微压缩，
    # 规划预算需 ×1.05 并在预算下限预留 MIN_SEG_DUR 停手间隙。
    'duration': {
        'enabled': True,
        'min_final': 210,      # 成片下限（s），对齐原业务窗口下界
        'max_final': 1200,     # 成片上限（s）= 20 分钟
        'per_unit': 32,        # 每叙事单元（集）基础秒数
        'per_spread': 8,       # 每集弧线跨度加成（s）
        'jitter': (20, 55),    # 每版随机抖动范围（s）
        'angle_bonus': {'高潮': 30, '反转': 45, '虐心': 15,
                        '逆袭': 40, '甜宠': 10, '平淡': 0},
    },

    # 叙事逻辑因果链校验（报告提案3）：对每版片段拼接顺序做因果链/时间线
    # 校验，排查逻辑断层/前后矛盾，自动替换破坏连贯性的片段。fail-open：
    # LLM 失败/无效 → 原样放行，绝不阻塞切片（与 R10 保守放行哲学一致）。
    'causal_check': {
        'enabled': True,
        'max_replace': 2,   # 每版最多自动替换段数（限制成本/改动幅度）
        'retry': 1,         # LLM 失败重试次数
    },

    # 上传计划发表时间：入队时按版本号自动计算（当天+day_offset 日，times 槽位循环）。
    # V1→times[0]，V2→times[1]…，同批多版本错峰发表；仅计算/记录/展示，不自动定时发表。
    'upload': {
        'publish_schedule': {
            'day_offset': 1,   # 发表日期 = 当天 + 1 天
            'times': ['06:00', '09:00', '12:00', '15:00', '18:00', '21:00'],  # 错峰槽位循环
        },
    },

    # A1 镜头边界吸附：切点若落在镜头中间（半帧切/转写 start 带前镜头），
    # 吸附到最近的真实镜头边界（ffmpeg scene 检测）。enabled=False 或检测
    # 失败 → 保守放行（不吸附），绝不因吸附故障阻塞切片。
    'scene_snap': {
        'enabled': True,
        'threshold': 0.30,   # scene 阈值（0-1，越大越不敏感）
        'tol': 0.15,         # 吸附容差（秒）：距边界 > tol 不吸附
    },

    'quality': {
        'min_duration': 180,
        # 上限放宽到 1260s（20min×1.05 余量）：剧情驱动时长允许长片，
        # 严格 20min 上限由 _select_best_videos 按 duration.max_final 执行。
        'max_duration': 1260,
        'min_size_mb': 10
    },

    'resource': {
        'min_disk_gb': 100,
        'max_concurrent': 2
    }
}


def _deep_merge(base, override):
    """嵌套 dict 深合并：override 覆盖 base；同键为 dict 时递归合并。返回新 dict。"""
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _load_yaml(path):
    """读取 config.yml；文件缺失 / 无 PyYAML / 解析异常时返回空 dict（fail-open）。"""
    if not path or not os.path.isfile(path) or yaml is None:
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - 配置损坏不阻塞启动，回退默认
        return {}


def apply_env_overrides(config):
    """用环境变量覆盖配置（原地修改，幂等）。返回覆盖数量。

    自 health.py 迁入；ffprobe 未显式覆盖时由 ffmpeg_dir 推导。
    """
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


def load_config(yaml_path=None):
    """按 默认 ← config.yml ← 环境变量 顺序组装一份新配置 dict。"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg = _deep_merge(cfg, _load_yaml(yaml_path or _DEFAULT_YML_PATH))
    apply_env_overrides(cfg)
    return cfg


_CONFIG = None


def get_config():
    """返回进程内配置单例（首次调用时加载）。"""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    return _CONFIG


def reset_config(yaml_path=None):
    """重建单例（供测试切换配置后复原）。返回新配置 dict。"""
    global _CONFIG
    _CONFIG = load_config(yaml_path)
    return _CONFIG


# 模块级单例，兼容现有 ``from config import CONFIG`` / ``CONFIG['x']`` / ``CONFIG.get(...)``。
CONFIG = get_config()
