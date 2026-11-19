# -*- coding: utf-8 -*-
"""按 archive 实况对账 pending_batch.json（修“回标漏标”留下的 pending）。

规则：能被 jobs.db 映射或 source_url 匹配到真实剧名，且 archive 的
slice_state=done、sliced_at 不早于本批次创建时间 → 标 done。
早于批次时间的 done（如秘书的坏账 11/01）说明本批次还没重切，保持 pending。
"""
import json
import os
import sqlite3
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'data', 'upload.db')
JOBS = os.path.join(ROOT, 'data', 'jobs.db')
QUEUE = os.path.join(ROOT, 'data', 'pending_batch.json')


def link_to_name():
    m = {}
    con = sqlite3.connect(JOBS)
    for (pj,) in con.execute('SELECT params_json FROM jobs'):
        try:
            p = json.loads(pj or '{}')
        except Exception:
            continue
        lid = p.get('link_id')
        name = p.get('drama_guess') or os.path.basename(
            (p.get('target_dir') or '').rstrip('/\\'))
        if lid and name:
            m[lid] = name
    con.close()
    return m


def main(apply=False):
    data = json.load(open(QUEUE, encoding='utf-8'))
    created = data.get('created_at') or ''
    names = link_to_name()
    con = sqlite3.connect(DB)
    cur = con.cursor()

    changed = []
    for e in data['links']:
        if e['status'] == 'done':
            continue
        name = names.get(e['link_id']) or e.get('drama_name')
        if not name:
            row = cur.execute(
                'SELECT drama_name FROM drama_archive WHERE source_url=?',
                (e['link'],)).fetchone()
            name = row[0] if row else None
        if not name:
            continue
        row = cur.execute(
            'SELECT slice_state, slice_version_count, sliced_at '
            'FROM drama_archive WHERE drama_name=?', (name,)).fetchone()
        if not row:
            continue
        state, cnt, sliced_at = row
        if state == 'done' and sliced_at and sliced_at >= created:
            e['status'] = 'done'
            e['drama_name'] = name
            e['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            changed.append((name, cnt, sliced_at))
    con.close()

    for n, c, t in changed:
        print(f'done | {n} | {c}版 | {t}')
    if changed and apply:
        tmp = QUEUE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, QUEUE)
        print(f'已写入 {QUEUE}（{len(changed)} 条改 done）')
    elif changed:
        print('（演练，未改文件；加 --apply 写入）')
    return 0


if __name__ == '__main__':
    sys.exit(main('--apply' in sys.argv))
