# -*- coding: utf-8 -*-
"""运行指标汇总（P2-11）：从 jobs.db 直接算，不改任何业务状态。

原先“今天跑了几部、慢在哪、失败都什么原因”只能靠人 grep 日志；这里把
jobs 与 job_events 聚合成可直接看的结构，供 ``GET /api/metrics`` 与日报使用。
"""
import os
import sqlite3
from datetime import datetime, timedelta


def _connect(db_path):
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_ts(text):
    if not text:
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(str(text)[:19], fmt)
        except ValueError:
            continue
    return None


def _dur_seconds(job):
    start = _parse_ts(job.get('started_at'))
    end = _parse_ts(job.get('finished_at'))
    if not start or not end:
        return None
    return max(0, int((end - start).total_seconds()))


def collect(db_path, date=None, days=1):
    """汇总指标。date=YYYY-MM-DD（默认今天），days=回溯天数。"""
    today = datetime.now()
    if date:
        try:
            end_day = datetime.strptime(date, '%Y-%m-%d')
        except ValueError:
            end_day = today
    else:
        end_day = today
    start_day = end_day - timedelta(days=max(1, int(days)) - 1)
    start_day = start_day.replace(hour=0, minute=0, second=0, microsecond=0)
    window_start = start_day.strftime('%Y-%m-%d %H:%M:%S')
    window_end = (end_day.replace(hour=23, minute=59, second=59)).strftime(
        '%Y-%m-%d %H:%M:%S')

    result = {
        'window': {'from': window_start, 'to': window_end},
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'job_types': {},
        'dramas': [],
        'slowest': [],
        'errors': [],
    }
    conn = _connect(db_path)
    if conn is None:
        result['error'] = f'jobs.db 不存在：{db_path}'
        return result
    try:
        rows = [dict(r) for r in conn.execute(
            'SELECT * FROM jobs ORDER BY created_at').fetchall()]
        events = [dict(r) for r in conn.execute(
            'SELECT * FROM job_events ORDER BY id').fetchall()]
    finally:
        conn.close()

    import json as _json

    def params_of(job):
        try:
            return _json.loads(job.get('params_json') or '{}') or {}
        except (ValueError, TypeError):
            return {}

    # 1) 按 job_type 汇总窗口内任务
    kept_rows = []
    for job in rows:
        created = _parse_ts(job.get('created_at'))
        if created and not (window_start <= created.strftime('%Y-%m-%d %H:%M:%S')
                            <= window_end):
            continue
        kept_rows.append(job)
        jt = job.get('job_type') or 'unknown'
        agg = result['job_types'].setdefault(jt, {
            'total': 0, 'completed': 0, 'failed': 0, 'running': 0,
            'pending': 0, 'cancelled': 0, 'duration_sum': 0, 'duration_n': 0})
        agg['total'] += 1
        status = (job.get('status') or '').lower()
        if status in ('completed', 'done'):
            agg['completed'] += 1
        elif status in ('failed',):
            agg['failed'] += 1
        elif status in ('running', 'cutting', 'downloading', 'uploading'):
            agg['running'] += 1
        elif status in ('pending',):
            agg['pending'] += 1
        if job.get('cancel_requested'):
            agg['cancelled'] += 1
        dur = _dur_seconds(job)
        if dur is not None:
            agg['duration_sum'] += dur
            agg['duration_n'] += 1
        if jt == 'drama':
            p = params_of(job)
            result['dramas'].append({
                'drama_name': p.get('drama_name') or '',
                'link_id': p.get('link_id') or '',
                'status': status,
                'stage': job.get('stage') or '',
                'attempt': p.get('attempt') or 0,
                'error': job.get('error') or '',
                'duration_seconds': dur,
                'finished_at': job.get('finished_at'),
            })
        if status in ('failed',) and job.get('error'):
            err = job.get('error') or ''
            # 续传器主动放弃的“素材已就绪/取消续传”不是真失败，剔除避免噪音
            if '取消续传' not in err and '素材已就绪' not in err:
                result['errors'].append({
                    'job_type': jt,
                    'drama': params_of(job).get('drama_name') or '',
                    'error': err[:300],
                    'finished_at': job.get('finished_at'),
                })

    for jt, agg in result['job_types'].items():
        agg['duration_avg'] = (round(agg['duration_sum'] / agg['duration_n'], 1)
                               if agg['duration_n'] else None)
        agg['success_rate'] = (round(agg['completed'] /
                                     max(1, agg['completed'] + agg['failed']), 3))

    # 2) 事件级别分布（窗口内）
    ev_by_level = {}
    for ev in events:
        created = _parse_ts(ev.get('created_at'))
        if created and not (window_start <= created.strftime('%Y-%m-%d %H:%M:%S')
                            <= window_end):
            continue
        lvl = (ev.get('level') or 'INFO').upper()
        ev_by_level[lvl] = ev_by_level.get(lvl, 0) + 1
    result['event_levels'] = ev_by_level

    # 3) 最慢任务（窗口内有时长的前 5 名）
    slow = [(dur, job) for job in kept_rows if (dur := _dur_seconds(job)) is not None]
    slow.sort(key=lambda x: x[0], reverse=True)
    for dur, job in slow[:5]:
        p = params_of(job)
        result['slowest'].append({
            'job_type': job.get('job_type'),
            'drama': p.get('drama_name') or p.get('drama_guess') or '',
            'seconds': dur,
            'finished_at': job.get('finished_at'),
        })
    return result
