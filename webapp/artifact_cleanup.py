# -*- coding: utf-8 -*-
"""
成品清理服务（artifact_cleanup.py）
====================================
两级清理：
  - 版本级（version）：某版本草稿确认成功后，移动该版本成品 mp4 + 封面到回收站。
  - 剧集级（drama）：同一批次所有目标版本全部成功、且没有其他账号/任务引用该素材时，
    移动素材目录 + 整个成品目录到回收站。

默认策略 recycle_on_full_batch：仅当前端明确勾选自动清理才启用；API 直调默认 none，
避免外部调用误删文件。

删除默认「移动到回收站」，不是立即永久删除：
  D:\\短剧工坊\\回收站\\<日期>\\<batch_id>\\<账号>\\<剧名>\\
每个清理任务先写 manifest，再移动到唯一目标目录，最后更新 DB 状态。
目标已存在、路径异常、移动中断时都能靠 manifest 恢复。

路径安全：
  - 素材目录必须是素材根目录的直接子目录；
  - 成品目录必须位于配置的 output 根目录内（且为 账号/剧名 两级）；
  - 禁止删除根目录、账号目录或通过 ..、符号链接逃逸的路径；
  - 其他批次或账号仍引用同一素材时禁止删除。
"""
import os
import json
import time
import shutil
import sqlite3
import logging
from datetime import datetime
from typing import List, Dict, Optional

try:
    from processor import CONFIG
except Exception:
    # 独立运行（单元测试）时允许注入配置
    CONFIG = None

logger = logging.getLogger('ArtifactCleanup')

# 清理策略
STRATEGY_NONE = 'none'
STRATEGY_RECYCLE = 'recycle_on_full_batch'
STRATEGIES = (STRATEGY_NONE, STRATEGY_RECYCLE)

# 封面状态判定为「可清理」的值
CLEANABLE_COVER = ('applied', 'not_requested')


class ArtifactCleanup:
    """成品清理服务。"""

    def __init__(self, data_dir: str = None, material_root: str = None,
                 output_root: str = None, recycle_root: str = None):
        if CONFIG is not None:
            data_dir = data_dir or CONFIG.get('data_dir', r'D:\短剧工坊\data')
            material_root = material_root or CONFIG.get('download_base_dir', r'E:\短剧素材')
            output_root = output_root or CONFIG.get('output_dir', r'D:\短剧工作区\output')
            recycle_root = recycle_root or r'D:\短剧工坊\回收站'
        self.data_dir = data_dir
        self.material_root = os.path.normpath(material_root or r'E:\短剧素材')
        self.output_root = os.path.normpath(output_root or r'D:\短剧工作区\output')
        self.recycle_root = os.path.normpath(recycle_root or r'D:\短剧工坊\回收站')
        self.db_path = os.path.join(self.data_dir, 'upload.db')

    # ── 路径安全 ──────────────────────────────────────────────
    @staticmethod
    def _norm(path):
        return os.path.normcase(os.path.abspath(path))

    def _safe_component(self, s, field):
        """回收站路径单个组件校验：拒绝分隔符、绝对路径、..、非法字符。

        返回合法组件字符串；非法时返回 None（调用方应跳过该任务）。
        """
        if s is None:
            return '未分配' if field == 'account' else ('未知剧名' if field == 'drama_name' else '')
        s = str(s)
        if not s.strip():
            return '未分配' if field == 'account' else ('未知剧名' if field == 'drama_name' else '')
        # 路径分隔符 / 反斜杠 / 驱动器 / 通配 / 绝对路径 / 上级引用
        if any(c in s for c in '\\/:*?"<>|'):
            return None
        if s in ('.', '..') or s.startswith('..') or os.path.isabs(s):
            return None
        return s

    def _safe_under(self, path, root):
        """校验 path 在 root 之内且不是 root 本身、不含 ..、无符号链接/联接点逃逸。

        逐级检查每个目录组件是否为符号链接，并用 realpath 解析（能解析 Windows
        联接点/重解析点），确保解析后仍在 root 内。
        """
        if not path:
            return False, '路径为空'
        p_abs = os.path.abspath(path)
        r_abs = os.path.abspath(root)
        rel = os.path.relpath(p_abs, r_abs)
        if rel == '.':
            return False, '不能删除根目录'
        if rel.startswith('..' + os.sep) or rel == '..':
            return False, f'路径不在允许根目录内: {path}'
        if '..' in rel.split(os.sep):
            return False, '路径含 .. 逃逸'
        # 逐级检查符号链接 / Windows 联接点（每个已存在的目录组件）。
        # os.path.islink 在 Windows 上不识别联接点（junction），必须同时用
        # st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT 检测重解析点。
        FILE_ATTRIBUTE_REPARSE_POINT = 0x400
        cur = r_abs
        for comp in rel.split(os.sep):
            cur = os.path.join(cur, comp)
            if not os.path.exists(cur):
                break  # 尚未创建的组件无需检查
            try:
                st = os.lstat(cur)
                if os.path.islink(cur) or \
                        (getattr(st, 'st_file_attributes', 0) & FILE_ATTRIBUTE_REPARSE_POINT):
                    return False, f'路径组件是符号链接/联接点，拒绝删除: {comp}'
            except OSError:
                pass
        # realpath 解析（覆盖 Windows 联接点/重解析点）：解析后必须仍在根内
        try:
            real_p = os.path.realpath(p_abs)
            real_r = os.path.realpath(r_abs)
            if real_p != real_r and not real_p.startswith(real_r + os.sep):
                return False, f'路径经 realpath 解析后逃逸出根目录: {path}'
        except OSError as e:
            return False, f'realpath 检查失败: {e}'
        return True, ''

    def _validate_output_dir(self, output_dir):
        """成品目录必须位于 output 根内，且为 账号/剧名 两级结构。"""
        ok, err = self._safe_under(output_dir, self.output_root)
        if not ok:
            return False, err
        rel = os.path.relpath(os.path.abspath(output_dir), os.path.abspath(self.output_root))
        depth = len([p for p in rel.split(os.sep) if p])
        if depth != 2:
            return False, f'成品目录深度异常（应为账号/剧名两级）: {rel}'
        return True, ''

    def _validate_material_dir(self, source_dir):
        """素材目录必须是素材根目录的直接子目录。"""
        if not source_dir:
            return False, '素材目录为空'
        ok, err = self._safe_under(source_dir, self.material_root)
        if not ok:
            return False, err
        rel = os.path.relpath(os.path.abspath(source_dir), os.path.abspath(self.material_root))
        depth = len([p for p in rel.split(os.sep) if p])
        if depth != 1:
            return False, f'素材目录深度异常（应为直接子目录）: {rel}'
        return True, ''

    def _validate_file_under_output(self, file_path):
        """版本级目标文件必须位于 output 根内。"""
        return self._safe_under(file_path, self.output_root)

    # ── DB 辅助 ───────────────────────────────────────────────
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _get_batch_records(self, batch_id):
        """读取某批次的所有上传记录。"""
        with self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM upload_records WHERE batch_id=? ORDER BY version',
                (batch_id,)).fetchall()
        return [dict(r) for r in rows]

    def _record_batch(self, batch_id, drama_name, account, source_dir, output_dir):
        """批量上传前登记批次（无则创建）。"""
        with self._connect() as conn:
            conn.execute(
                '''INSERT OR IGNORE INTO upload_batches
                   (batch_id, account, drama_name, source_dir, output_dir, status)
                   VALUES (?, ?, ?, ?, ?, 'running')''',
                (batch_id, account, drama_name, source_dir, output_dir))

    # ── 计划器 ────────────────────────────────────────────────
    def plan_cleanup(self, batch_id: str, strategy: str = STRATEGY_RECYCLE,
                     dry_run: bool = True) -> Dict:
        """生成某批次的清理计划（默认 dry-run，不移动任何文件）。

        Returns:
            {'status': 'success'|'failed', 'strategy': ..., 'dry_run': bool,
             'jobs': [ {...}, ... ], 'skipped': [...], 'reason': str|None}
        """
        if strategy not in STRATEGIES:
            return {'status': 'failed', 'strategy': strategy, 'dry_run': dry_run,
                    'jobs': [], 'skipped': [], 'reason': f'未知策略: {strategy}'}
        if strategy == STRATEGY_NONE:
            return {'status': 'failed', 'strategy': strategy, 'dry_run': dry_run,
                    'jobs': [], 'skipped': [], 'reason': '策略为 none（自动清理未启用）'}

        records = self._get_batch_records(batch_id)
        if not records:
            return {'status': 'failed', 'strategy': strategy, 'dry_run': dry_run,
                    'jobs': [], 'skipped': [], 'reason': f'批次 {batch_id} 无记录'}

        jobs = []
        skipped = []
        needs_attention = []

        for r in records:
            ver = r['version']
            cover_status = r.get('cover_status') or 'not_requested'
            # 封面失败 → 该版本不清理（needs_attention）
            if cover_status == 'failed':
                needs_attention.append(ver)
                skipped.append({
                    'version': ver, 'reason': 'cover_failed',
                    'detail': r.get('error_message'),
                })
                continue
            # 版本级：草稿确认成功才清理成品 + 封面
            if r['status'] == 'completed' and r['draft_saved'] == 1:
                video_ok, video_err = self._validate_file_under_output(r['video_path'])
                if video_ok and os.path.exists(r['video_path']):
                    rp = self._build_recycle_path(
                        batch_id, r['account'], r['drama_name'],
                        os.path.basename(r['video_path']))
                    if not rp:
                        skipped.append({'version': ver, 'reason': 'recycle_path_invalid',
                                        'detail': '回收站路径组件非法（批次/账号/剧名）'})
                    else:
                        jobs.append({
                            'batch_id': batch_id,
                            'account': r['account'],
                            'drama_name': r['drama_name'],
                            'job_type': 'version',
                            'version': ver,
                            'target_path': r['video_path'],
                            'recycle_path': rp,
                        })
                else:
                    skipped.append({'version': ver, 'reason': 'video_missing_or_unsafe',
                                    'detail': video_err})
                cover_path = r.get('cover_path')
                if cover_path and cover_status in CLEANABLE_COVER:
                    cover_ok, cover_err = self._validate_file_under_output(cover_path)
                    if cover_ok and os.path.exists(cover_path):
                        rp = self._build_recycle_path(
                            batch_id, r['account'], r['drama_name'],
                            os.path.basename(cover_path))
                        if not rp:
                            skipped.append({'version': ver, 'reason': 'recycle_path_invalid',
                                            'detail': '回收站路径组件非法（批次/账号/剧名）'})
                        else:
                            jobs.append({
                                'batch_id': batch_id,
                                'account': r['account'],
                                'drama_name': r['drama_name'],
                                'job_type': 'version_cover',
                                'version': ver,
                                'target_path': cover_path,
                                'recycle_path': rp,
                            })
                    else:
                        skipped.append({'version': ver, 'reason': 'cover_missing_or_unsafe',
                                        'detail': cover_err})
            elif r['status'] == 'completed' and not r['draft_saved']:
                skipped.append({'version': ver, 'reason': 'draft_not_saved'})

        # 剧集级：按 (账号, 剧名, output_dir, source_dir) 分组，每组独立判断。
        # 不能只看 records[0] —— 一个批次可能含多部剧（多个成品目录）；
        # 也不能在未确认其他批次仍引用同一成品目录时移动整个目录。
        groups = {}
        for r in records:
            key = (r.get('output_dir') or '', r.get('source_dir') or '',
                   r.get('account') or '', r.get('drama_name') or '')
            groups.setdefault(key, []).append(r)

        for (out_dir, src_dir, account, drama_name), grp in groups.items():
            grp_done = all(g['status'] == 'completed' for g in grp)
            grp_draft = all(g['draft_saved'] == 1 for g in grp)
            grp_attention = any((g.get('cover_status') or 'not_requested') == 'failed' for g in grp)
            if not (grp_done and grp_draft and not grp_attention):
                skipped.append({'account': account, 'drama_name': drama_name,
                                'reason': 'group_not_ready'})
                continue
            if not out_dir and not src_dir:
                # 历史记录（无 output/source 路径）无法做目录级清理
                skipped.append({'account': account, 'drama_name': drama_name,
                                'reason': 'no_path'})
                continue
            # 输出目录级：其他批次仍有待上传视频引用同一成品目录 → 禁止移动
            if out_dir:
                ok, err = self._validate_output_dir(out_dir)
                if ok and os.path.exists(out_dir):
                    ref = self._other_references_output(out_dir, batch_id, out_dir)
                    if not ref:
                        rp = self._build_recycle_path(
                            batch_id, account, drama_name,
                            os.path.basename(out_dir), is_dir=True)
                        if not rp:
                            skipped.append({'account': account, 'drama_name': drama_name,
                                            'reason': 'recycle_path_invalid',
                                            'detail': '回收站路径组件非法（批次/账号/剧名）'})
                        else:
                            jobs.append({
                                'batch_id': batch_id,
                                'account': account, 'drama_name': drama_name,
                                'job_type': 'drama_output', 'version': None,
                                'target_path': out_dir,
                                'recycle_path': rp,
                            })
                    else:
                        skipped.append({'account': account, 'drama_name': drama_name,
                                        'reason': 'output_referenced', 'detail': ref})
                else:
                    skipped.append({'account': account, 'drama_name': drama_name,
                                    'reason': 'output_dir_missing_or_unsafe', 'detail': err})
            # 素材目录级：其他批次/其他组仍引用 → 禁止移动
            if src_dir:
                ok, err = self._validate_material_dir(src_dir)
                if ok and os.path.exists(src_dir):
                    ref = self._other_references_source(src_dir, batch_id, out_dir)
                    if not ref:
                        rp = self._build_recycle_path(
                            batch_id, account, drama_name,
                            os.path.basename(src_dir), is_dir=True)
                        if not rp:
                            skipped.append({'account': account, 'drama_name': drama_name,
                                            'reason': 'recycle_path_invalid',
                                            'detail': '回收站路径组件非法（批次/账号/剧名）'})
                        else:
                            jobs.append({
                                'batch_id': batch_id,
                                'account': account, 'drama_name': drama_name,
                                'job_type': 'drama_source', 'version': None,
                                'target_path': src_dir,
                                'recycle_path': rp,
                            })
                    else:
                        skipped.append({'account': account, 'drama_name': drama_name,
                                        'reason': 'source_referenced', 'detail': ref})
                else:
                    skipped.append({'account': account, 'drama_name': drama_name,
                                    'reason': 'source_dir_missing_or_unsafe', 'detail': err})

        # 非 dry-run 时持久化到 artifact_cleanup_jobs
        if not dry_run:
            self._persist_jobs(jobs)

        return {'status': 'success', 'strategy': strategy, 'dry_run': dry_run,
                'jobs': jobs, 'skipped': skipped, 'reason': None}

    def _persist_jobs(self, jobs):
        with self._connect() as conn:
            for j in jobs:
                conn.execute(
                    '''INSERT INTO artifact_cleanup_jobs
                       (batch_id, account, drama_name, job_type, version,
                        target_path, recycle_path, strategy, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')''',
                    (j['batch_id'], j['account'], j['drama_name'], j['job_type'],
                     j['version'], j['target_path'], j['recycle_path'],
                     j.get('strategy', STRATEGY_RECYCLE)))

    # ── 引用检查 ──────────────────────────────────────────────
    def _other_references_source(self, source_dir, exclude_batch_id, exclude_output_dir=None):
        """其他批次/其他组是否仍引用该素材目录。返回引用描述或 None。"""
        with self._connect() as conn:
            rows = conn.execute(
                '''SELECT batch_id, account, drama_name, output_dir FROM upload_records
                   WHERE source_dir=? AND status IN ('pending','uploading')''',
                (source_dir,)).fetchall()
            for r in rows:
                if r['batch_id'] == exclude_batch_id and \
                        (exclude_output_dir is None or r['output_dir'] == exclude_output_dir):
                    continue  # 本组自己的待上传记录（正常不会有，保守跳过）
                return f"batch={r['batch_id']} 账号={r['account']}"
            # 其他批次已完成但未清理——保守起见也视为引用
            rows2 = conn.execute(
                '''SELECT DISTINCT batch_id, account, output_dir FROM upload_records
                   WHERE source_dir=? AND batch_id != ?''',
                (source_dir, exclude_batch_id)).fetchall()
            if rows2:
                return f"其他batch={rows2[0]['batch_id']}"
        return None

    def _other_references_output(self, output_dir, exclude_batch_id, exclude_output_dir):
        """其他批次是否仍有待上传/上传中视频位于该成品目录。返回引用描述或 None。

        关键闸门：整目录（剧集级）清理前，必须确认没有其他批次的待上传任务
        仍引用同一成品目录，否则会过早移走目录。
        """
        if not output_dir:
            return None
        with self._connect() as conn:
            rows = conn.execute(
                '''SELECT batch_id, account, drama_name FROM upload_records
                   WHERE output_dir=? AND status IN ('pending','uploading')''',
                (output_dir,)).fetchall()
            for r in rows:
                if r['batch_id'] == exclude_batch_id and output_dir == exclude_output_dir:
                    continue  # 本组自身（正常不会）
                return f"batch={r['batch_id']} 账号={r['account']}"
        return None

    # ── 回收站路径 ────────────────────────────────────────────
    def _build_recycle_path(self, batch_id, account, drama_name, basename,
                            is_dir=False):
        """构造唯一回收站目标：回收站/<日期>/<batch_id>/<账号>/<剧名>/<basename>

        对每个组件做边界校验（_safe_component）：拒绝分隔符、..、绝对路径、
        非法字符。任一组件非法则返回 None，调用方应跳过该任务（绝不静默
        用未清洗的组件拼路径，防止 批次/账号/剧名 夹带 .. 或分隔符逃逸）。
        """
        batch = self._safe_component(batch_id, 'batch_id')
        acct = self._safe_component(account, 'account')
        drama = self._safe_component(drama_name, 'drama_name')
        base_name = self._safe_component(basename, 'basename')
        if not (batch and acct and drama and base_name):
            return None
        date_str = datetime.now().strftime('%Y%m%d')
        base = os.path.join(self.recycle_root, date_str, batch, acct, drama)
        return os.path.join(base, base_name)

    def _unique_dir(self, path):
        """目标已存在则追加序号，避免覆盖。"""
        if not os.path.exists(path):
            return path
        for i in range(1, 1000):
            cand = f'{path}_{i}'
            if not os.path.exists(cand):
                return cand
        return f'{path}_{int(time.time())}'

    def _write_manifest(self, job, recycle_path):
        """清理前写 manifest，记录来源与目标，供恢复。"""
        manifest = {
            'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'batch_id': job['batch_id'],
            'account': job['account'],
            'drama_name': job['drama_name'],
            'job_type': job['job_type'],
            'version': job['version'],
            'target_path': job['target_path'],
            'recycle_path': recycle_path,
            'strategy': job.get('strategy', STRATEGY_RECYCLE),
        }
        manifest_dir = os.path.dirname(recycle_path)
        os.makedirs(manifest_dir, exist_ok=True)
        # manifest 必须按任务唯一，否则同批次多任务共用 manifest.json 互相覆盖
        safe_basename = os.path.basename(job['target_path']).replace('.', '_')[:60]
        manifest_path = os.path.join(
            manifest_dir,
            f"manifest_{job['job_type']}_{job['version'] or 'drama'}_{safe_basename}.json")
        with open(manifest_path, 'w', encoding='utf-8') as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        return manifest_path

    # ── 执行 ──────────────────────────────────────────────────
    def execute_cleanup(self, batch_id: str, strategy: str = STRATEGY_RECYCLE) -> Dict:
        """非 dry-run：先计划，再逐任务执行（写 manifest → 移动 → 更新 DB）。"""
        plan = self.plan_cleanup(batch_id, strategy=strategy, dry_run=False)
        if plan['status'] != 'success':
            return plan
        results = []
        for job in plan['jobs']:
            results.append(self._execute_job(job))
        return {'status': 'success', 'strategy': strategy, 'dry_run': False,
                'jobs': plan['jobs'], 'results': results,
                'skipped': plan['skipped'], 'reason': None}

    def _execute_job(self, job: Dict) -> Dict:
        """执行单个清理任务：写 manifest → 移动 → 更新 DB。"""
        target = job['target_path']
        # 再次校验路径安全（执行前双保险）
        if job['job_type'] in ('drama_output', 'version', 'version_cover'):
            ok, err = self._validate_file_under_output(target) if not os.path.isdir(target) \
                else self._validate_output_dir(target)
        elif job['job_type'] == 'drama_source':
            ok, err = self._validate_material_dir(target)
        else:
            ok, err = False, f'未知任务类型: {job["job_type"]}'
        if not ok:
            self._mark_job(job, 'failed', err)
            return {'status': 'failed', 'target': target, 'error': err}

        if not os.path.exists(target):
            self._mark_job(job, 'skipped', '目标不存在，跳过')
            return {'status': 'skipped', 'target': target}

        recycle_path = self._unique_dir(job['recycle_path'])
        manifest_path = self._write_manifest(job, recycle_path)
        try:
            os.makedirs(os.path.dirname(recycle_path), exist_ok=True)
            shutil.move(target, recycle_path)
            self._mark_job(job, 'moved', None, recycle_path, manifest_path)
            logger.info(f'已移动 {target} → {recycle_path}')
            return {'status': 'moved', 'target': target,
                    'recycle_path': recycle_path, 'manifest': manifest_path}
        except Exception as e:
            self._mark_job(job, 'failed', str(e), recycle_path, manifest_path)
            logger.warning(f'移动失败 {target}: {e}')
            return {'status': 'failed', 'target': target, 'error': str(e)}

    def _mark_job(self, job, status, error=None, recycle_path=None, manifest_path=None):
        """更新 artifact_cleanup_jobs 状态。"""
        completed = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        try:
            with self._connect() as conn:
                conn.execute(
                    '''UPDATE artifact_cleanup_jobs
                       SET status=?, error=?, recycle_path=?,
                           manifest_path=?, completed_at=?
                       WHERE batch_id=? AND job_type=? AND target_path=?''',
                    (status, error, recycle_path, manifest_path, completed,
                     job['batch_id'], job['job_type'], job['target_path']))
        except Exception as e:
            logger.debug(f'更新清理任务状态失败: {e}')

    # ── 查询 ──────────────────────────────────────────────────
    def list_jobs(self, batch_id: str = None) -> List[Dict]:
        with self._connect() as conn:
            if batch_id:
                rows = conn.execute(
                    'SELECT * FROM artifact_cleanup_jobs WHERE batch_id=? ORDER BY id',
                    (batch_id,)).fetchall()
            else:
                rows = conn.execute(
                    'SELECT * FROM artifact_cleanup_jobs ORDER BY id DESC LIMIT 200'
                ).fetchall()
        return [dict(r) for r in rows]


# 导出
__all__ = ['ArtifactCleanup', 'STRATEGY_NONE', 'STRATEGY_RECYCLE']
