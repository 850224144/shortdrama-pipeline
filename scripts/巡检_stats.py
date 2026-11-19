# -*- coding: utf-8 -*-
"""巡检用统计：输出 JSON（队列状态 + 今日失败任务数），供巡检脚本调用。"""
import json
import os
import sqlite3
import sys

ROOT = r'D:\短剧工坊'
sys.path.insert(0, os.path.join(ROOT, 'webapp'))

from pending_queue import load_pending  # noqa: E402

batch = load_pending() or {}
links = [{'status': x.get('status'), 'name': x.get('drama_name') or x.get('link_id'),
          'link_id': x.get('link_id')} for x in batch.get('links', [])]

fail_today = 0
db = os.path.join(ROOT, 'data', 'jobs.db')
try:
    conn = sqlite3.connect(db)
    fail_today = conn.execute(
        "select count(1) from jobs where status='failed' "
        "and date(updated_at)=date('now','localtime')").fetchone()[0]
    running = conn.execute(
        "select count(1) from jobs where status='running'").fetchone()[0]
    conn.close()
except Exception:
    running = 0

print(json.dumps({'links': links, 'fail_today': fail_today, 'running': running},
                 ensure_ascii=False))
