# -*- coding: utf-8 -*-
"""2026-11-19 档案数据修复（一次性维护脚本）。

修复内容：
1. 合并“分享码当剧名”的重复档案行：旧 cut_batch 用 link_id 写 slice_state，
   生成 1txjKms... 这类行，真实剧名行永远停在 none。把状态并回真实行后删重复行。
2. local_dir 纠偏：记录指向不存在目录（素材被清理/误写成输出目录）时，
   若 E:\\短剧素材\\{剧名} 存在则改指它。
3. 已交付但 slice_state 仍是 none 的剧（她断/我的家产）按输出目录回填 done。
4. downloaded_at 为空的 done 行用 updated_at 补齐。

用法：
    python scripts/repair_archive_20261119.py          # 只打印，不改库
    python scripts/repair_archive_20261119.py --apply  # 执行（先自动备份）
"""
import glob
import json
import os
import re
import sqlite3
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEBAPP = os.path.join(ROOT, 'webapp')
sys.path.insert(0, WEBAPP)
from config import CONFIG

DB_PATH = os.path.join(ROOT, 'data', 'upload.db')
JOBS_DB = os.path.join(ROOT, 'data', 'jobs.db')
SHARE_ID_RE = re.compile(r'^[0-9A-Za-z_\-]{20,}$')
CJK_RE = re.compile(r'[\u4e00-\u9fff]')


def is_share_id(name):
    return bool(SHARE_ID_RE.match(name or '')) and not CJK_RE.search(name or '')


def material_count(path):
    if not path or not os.path.isdir(path):
        return 0
    n = 0
    for _r, _d, files in os.walk(path):
        for f in files:
            if f.lower().endswith('.mp4') and '_去重版' not in f \
                    and '_REJECTED' not in f and '封面' not in f \
                    and '.BaiduPCS-Go-downloading' not in f:
                n += 1
    return n


def load_job_name_map():
    """jobs.db 的 link_id → 真实剧名映射（下载任务参数里有 drama_guess/target_dir）。"""
    mapping = {}
    jobs_db = JOBS_DB
    if not os.path.isfile(jobs_db):
        return mapping
    try:
        conn = sqlite3.connect(jobs_db)
        cur = conn.cursor()
        for (params_json,) in cur.execute('SELECT params_json FROM jobs'):
            try:
                p = json.loads(params_json or '{}')
            except Exception:
                continue
            link_id = p.get('link_id')
            name = (p.get('drama_guess') or
                    os.path.basename((p.get('target_dir') or '').rstrip('/\\')))
            if link_id and name and not is_share_id(name):
                mapping[link_id] = name
        conn.close()
    except Exception as e:
        print(f'WARN: 读取 jobs.db 映射失败：{e}')
    return mapping


def main(apply=False):
    if not os.path.isfile(DB_PATH):
        print(f'档案库不存在：{DB_PATH}')
        return 1
    if apply:
        backup_dir = os.path.join(os.path.dirname(DB_PATH), 'backup')
        os.makedirs(backup_dir, exist_ok=True)
        bak = os.path.join(backup_dir,
                           f'upload_before_repair_{datetime.now():%Y%m%d_%H%M%S}.db')
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(bak)
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
        print(f'已备份：{bak}')

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = [dict(r) for r in cur.execute('SELECT * FROM drama_archive')]
    job_names = load_job_name_map()
    actions = []

    # 1) 合并分享码行 → 真实剧名行（优先 source_url 精确匹配，其次 jobs.db 映射）
    for row in rows:
        name = row['drama_name']
        if not is_share_id(name):
            continue
        src_url = row['source_url']
        real = None
        if src_url:
            for cand in rows:
                if cand is row or cand['source_url'] != src_url:
                    continue
                if is_share_id(cand['drama_name']):
                    continue
                if real is None or cand['download_state'] == 'done':
                    real = cand
        if real is None:
            mapped = job_names.get(name)
            if mapped:
                real = next((c for c in rows
                             if c['drama_name'] == mapped), None)
        if not real:
            actions.append(('keep', f'分享码行无对应真实行，保留：{name}'))
            continue
        fields = {}
        if row['slice_state'] in ('done', 'cutting', 'failed') \
                and real['slice_state'] != 'done':
            if row['slice_state'] == 'cutting':
                # 崩溃遗留的“切片中”不是最终状态：并回 none 并留痕，允许重切
                fields['slice_state'] = 'none'
                fields['slice_error'] = '历史切片中断（分享码重复行合并，待重切）'
            else:
                fields['slice_state'] = row['slice_state']
                fields['slice_error'] = row['slice_error']
            fields['slice_version_count'] = row['slice_version_count']
            fields['sliced_at'] = row['sliced_at']
        if row['download_state'] == 'done' and real['download_state'] != 'done':
            fields['download_state'] = 'done'
        actions.append(('merge', name, real['drama_name'], fields))
        if apply:
            if fields:
                sets = ', '.join(f'{k}=?' for k in fields)
                cur.execute(
                    f'UPDATE drama_archive SET {sets}, '
                    f'updated_at=CURRENT_TIMESTAMP WHERE drama_name=?',
                    list(fields.values()) + [real['drama_name']])
            cur.execute('DELETE FROM drama_archive WHERE drama_name=?', (name,))

    # 2)(3)(4) 逐行校正
    base = CONFIG['download_base_dir']
    out_root = CONFIG['output_dir']
    for row in rows:
        if is_share_id(row['drama_name']):
            continue
        name = row['drama_name']
        sets = {}
        local = row['local_dir'] or ''
        if material_count(local) == 0:
            cand = os.path.join(base, name)
            cnt = material_count(cand)
            if cnt > 0:
                sets['local_dir'] = cand
                sets['episode_count'] = cnt
                actions.append(('local_dir', name, local or '空', f'{cand}（{cnt}集）'))
        # 已交付但状态 none → done
        if row['slice_state'] in ('none', '', None):
            dirs = glob.glob(os.path.join(out_root, '*', name))
            for d in dirs:
                if not os.path.isdir(d):
                    continue
                n = len([f for f in os.listdir(d)
                         if f.endswith('_去重版.mp4')])
                if n > 0 and (os.path.isfile(os.path.join(d, f'{name}_交付报告.md'))
                              or os.path.isfile(os.path.join(d, f'{name}_QC报告.md')
                                                )):
                    sets['slice_state'] = 'done'
                    sets['slice_version_count'] = n
                    sets['sliced_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    actions.append(('slice_done', name, 'none', f'done（{n}版，{os.path.basename(os.path.dirname(d))}）'))
                    break
        if row['download_state'] == 'done' and not row.get('downloaded_at'):
            sets['downloaded_at'] = row['updated_at']
            actions.append(('downloaded_at', name, '空', row['updated_at']))
        if sets and apply:
            keys = list(sets.keys())
            cur.execute(
                'UPDATE drama_archive SET ' + ', '.join(f'{k}=?' for k in keys) +
                ', updated_at=CURRENT_TIMESTAMP WHERE drama_name=?',
                [sets[k] for k in keys] + [name])

    if apply:
        conn.commit()
    conn.close()

    print('\n计划/已执行动作：')
    for a in actions:
        print('  ' + ' | '.join(str(x) for x in a))
    if not apply:
        print('\n（演练模式，未改库；确认后加 --apply）')
    return 0


if __name__ == '__main__':
    sys.exit(main(apply='--apply' in sys.argv))
