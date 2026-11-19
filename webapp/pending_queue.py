# -*- coding: utf-8 -*-
"""批次链接队列持久化。

批次开始前把全部链接落盘（data/pending_batch.json），下载/切片每推进一步
更新状态；进程崩溃后由启动钩子把未完成的链接重新提交，避免「14 条链接
只跑到第 11 条，剩下 3 条随内存队列一起消失」。

状态：pending（待处理）/ done（已交付）/ failed（失败待重试）
"""
import json
import os
import threading
import uuid
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUEUE_PATH = os.path.join(PROJECT_ROOT, 'data', 'pending_batch.json')

_lock = threading.Lock()


def _atomic_write(path, data):
    tmp = f'{path}.tmp'
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def create_batch(links):
    """批次开始：落盘全部链接。links 为 LinkExtractor 的 dict 列表。"""
    entries = []
    for li in links:
        link = li.get('link') or ''
        pwd = li.get('pwd') or ''
        msg = link if (not pwd or 'pwd=' in link) else f'{link}?pwd={pwd}'
        entries.append({
            'link': link,
            'link_id': li.get('link_id') or '',
            'pwd': pwd,
            'msg': msg,
            'status': 'pending',
            'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        })
    data = {
        'batch_id': uuid.uuid4().hex[:12],
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'links': entries,
    }
    with _lock:
        try:
            _atomic_write(QUEUE_PATH, data)
        except OSError:
            pass
    return data


def load_pending():
    try:
        with open(QUEUE_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get('links'):
            return data
    except Exception:
        pass
    return None


def mark(link_id=None, link=None, status='done', drama_name=None):
    """按 link_id / link 更新单条状态。找不到则忽略。"""
    with _lock:
        data = load_pending()
        if not data:
            return
        changed = False
        for entry in data.get('links', []):
            if (link_id and entry.get('link_id') == link_id) or \
                    (link and entry.get('link') == link):
                entry['status'] = status
                if drama_name:
                    entry['drama_name'] = drama_name
                entry['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                changed = True
        if changed:
            try:
                _atomic_write(QUEUE_PATH, data)
            except OSError:
                pass


def mark_by_drama(drama_name, status='done'):
    """切片完成后按剧名回标（下载结果里 drama_name 才是权威）。"""
    if not drama_name:
        return
    with _lock:
        data = load_pending()
        if not data:
            return
        changed = False
        for entry in data.get('links', []):
            if entry.get('drama_name') == drama_name:
                entry['status'] = status
                entry['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                changed = True
        if changed:
            try:
                _atomic_write(QUEUE_PATH, data)
            except OSError:
                pass


def add_pending(items):
    """追加未完成条目（重启后按单剧任务补排队）。

    items: [{link, link_id, pwd, drama_name, msg}]。按 link_id/link 去重，
    已存在 pending/failed 的不重复追加。返回实际新增条数。
    """
    if not items:
        return 0
    with _lock:
        data = load_pending() or {
            'batch_id': uuid.uuid4().hex[:12],
            'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'links': [],
        }
        links = data.setdefault('links', [])
        existing_ids = {x.get('link_id') for x in links if x.get('link_id')}
        existing_links = {x.get('link') for x in links if x.get('link')}
        added = 0
        for it in items:
            link_id = it.get('link_id') or ''
            link = it.get('link') or ''
            if (link_id and link_id in existing_ids) or (link and link in existing_links):
                continue
            pwd = it.get('pwd') or ''
            msg = it.get('msg') or (link if (not pwd or 'pwd=' in link)
                                    else f'{link}?pwd={pwd}')
            links.append({
                'link': link,
                'link_id': link_id,
                'pwd': pwd,
                'msg': msg,
                'drama_name': it.get('drama_name') or '',
                'status': 'pending',
                'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            })
            if link_id:
                existing_ids.add(link_id)
            if link:
                existing_links.add(link)
            added += 1
        if added:
            try:
                _atomic_write(QUEUE_PATH, data)
            except OSError:
                return 0
        return added


def clear_pending():
    with _lock:
        try:
            if os.path.exists(QUEUE_PATH):
                os.remove(QUEUE_PATH)
        except OSError:
            pass
