# -*- coding: utf-8 -*-
"""结构化日志入口（阶段一 4.2）。

统一条目字段：``timestamp / level / drama_name / account / task_id / batch_id / stage / message``。

兼容约束（不可破坏）：
- 旧四字段 ``timestamp / level / message / drama_name`` 保持不变，前端 ``log_update``
  渲染与时间戳去重逻辑依赖它们；
- ``account / task_id / batch_id / stage`` 为新增维度，缺省空串，前端不读取也不报错；
- 零新依赖：基于标准库 ``logging``，文件落盘与 socketio 推送仍由各 Logger 通道负责。
"""
import logging
from datetime import datetime

# 级别归一化：兼容 info/INFO/error/ERROR/success/warn/WARNING 等大小写与别名写法。
LEVEL_ALIASES = {
    'info': 'INFO',
    'warn': 'WARN',
    'warning': 'WARN',
    'error': 'ERROR',
    'success': 'INFO',
    'debug': 'DEBUG',
}

_LOG_TIME_FMT = '%Y-%m-%d %H:%M:%S'


def normalize_level(level):
    """把各种级别写法归一化为大写标准级别。"""
    return LEVEL_ALIASES.get(str(level).lower(), str(level).upper())


def make_entry(level, message, drama_name='', account='', task_id='',
               batch_id='', stage='', timestamp=None):
    """构造一条标准日志条目 dict。"""
    return {
        'timestamp': timestamp or datetime.now().strftime(_LOG_TIME_FMT),
        'level': normalize_level(level),
        'drama_name': drama_name or '',
        'account': account or '',
        'task_id': task_id or '',
        'batch_id': batch_id or '',
        'stage': stage or '',
        'message': message,
    }


def context_from_task(task):
    """从任务 dict 提取日志上下文字段；任务为空或非 dict 时返回空上下文。"""
    if not isinstance(task, dict):
        return {}
    return {
        'drama_name': task.get('drama_name', ''),
        'account': task.get('account', ''),
        'task_id': task.get('id', ''),
        'batch_id': task.get('batch_id', ''),
        'stage': task.get('stage', ''),
    }


def get_logger(name='shortdrama'):
    """返回标准库 logger（控制台兜底通道）；重复调用复用同一实例与 handler。"""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger
