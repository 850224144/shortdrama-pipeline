# -*- coding: utf-8 -*-
"""单剧级持久任务：每部剧一条 jobs.db 记录（``job_type='drama'``）。

为什么需要
----------
批次目前是“一个线程串行跑 N 部剧”，崩溃/重启后只能靠 archive 与
pending_batch.json 事后对账，无法直接回答“这 12 部里哪几部跑完了、哪部卡在
哪一步”。本模块给每部剧一条独立任务行，用确定性 id 做幂等键：

- **幂等**：同一条链接（或同名手动切片）永远映射到同一条记录，重复提交不
  会产生重复档案（等价于 Temporal/Celery 里的 idempotency key）；
- **失败隔离**：某部剧抛异常只把该行置 failed，批次循环继续跑下一部；
- **可恢复**：重启后按行查未完成剧，不依赖内存队列；
- **可观测**：每部剧的阶段与耗时可直接被 ``/api/metrics`` 聚合。

状态口径：pending → running → completed | failed（cancelled 不入库，保持
pending 等重启重跑，与 job_store 的“停止不写业务结果”约定一致）。
"""
import hashlib

JOB_TYPE = 'drama'

STATUS_PENDING = 'pending'
STATUS_RUNNING = 'running'
STATUS_COMPLETED = 'completed'
STATUS_FAILED = 'failed'


def idempotency_key(link_id=None, drama_name=None):
    """返回剧级幂等键：优先 link_id（同一链接永远同一部剧），否则 manual:剧名。"""
    key = str(link_id or '').strip()
    if key:
        return key
    name = str(drama_name or '').strip()
    if not name:
        return ''
    return f'manual:{name}'


def job_id_for(link_id=None, drama_name=None):
    """确定性任务 id（同键恒等），空键返回空串。"""
    key = idempotency_key(link_id, drama_name)
    if not key:
        return ''
    return 'drama-' + hashlib.md5(key.encode('utf-8')).hexdigest()[:12]


def find_by_key(store, link_id=None, drama_name=None):
    """按幂等键查已有剧任务；不存在返回 None。"""
    job_id = job_id_for(link_id, drama_name)
    if not job_id:
        return None
    try:
        return store.get(job_id)
    except Exception:
        return None


def start(store, drama_name, link_id=None, link=None, account=None,
          watermark=None, resume=False, batch_id=None, extra=None):
    """标记某部剧开始切片，返回 job_id。

    已存在的记录：保留 created_at，重写 stage/status 为 running；``attempt``
    累加，便于识别“反复失败”的剧。
    """
    job_id = job_id_for(link_id, drama_name)
    if not job_id:
        return None
    prev = None
    try:
        prev = store.get(job_id)
    except Exception:
        prev = None
    params = {
        'drama_name': drama_name,
        'link_id': link_id,
        'link': link,
        'account': account,
        'watermark': watermark,
        'resume': bool(resume),
        'batch_id': batch_id,
        'idempotency_key': idempotency_key(link_id, drama_name),
        'attempt': int(((prev or {}).get('params') or {}).get('attempt') or 0) + 1,
    }
    if extra:
        params.update(extra)
    job = {
        'id': job_id,
        'job_type': JOB_TYPE,
        'status': STATUS_RUNNING,
        'stage': 'cut',
        'progress': 0,
        'params': params,
        'result': None,
        'error': None,
        'created_at': (prev or {}).get('created_at'),
    }
    job['started_at'] = _now()
    job['heartbeat_at'] = job['started_at']
    store.put(job)
    return job_id


def stage(store, job_id, stage_name, progress=None):
    """更新阶段（失败只记日志，绝不影响切片主流程）。"""
    if not job_id:
        return
    fields = {'stage': stage_name, 'heartbeat_at': _now()}
    if progress is not None:
        fields['progress'] = progress
    try:
        store.update(job_id, **fields)
    except Exception:
        pass


def finish(store, job_id, status=STATUS_COMPLETED, result=None, error=None):
    """收尾：completed/failed 并写入结果摘要。"""
    if not job_id:
        return
    try:
        store.update(job_id,
                     status=status,
                     progress=100 if status == STATUS_COMPLETED else None,
                     result=result,
                     error=error,
                     finished_at=_now(),
                     heartbeat_at=_now())
        store.add_event(job_id, 'INFO' if status == STATUS_COMPLETED else 'ERROR',
                        status, (result or {}).get('summary') or (error or ''),
                        {'result': result, 'error': error})
    except Exception:
        pass


def list_unfinished(store):
    """列出未完成的剧任务（running/pending），按创建时间正序。"""
    try:
        jobs = store.list(JOB_TYPE, (STATUS_RUNNING, STATUS_PENDING))
    except Exception:
        return []
    return jobs


def _now():
    from datetime import datetime
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')
