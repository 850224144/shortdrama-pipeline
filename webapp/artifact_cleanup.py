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

    def __init__(self, data_dir: Optional[str] = None, material_root=None,
                 output_root: Optional[str] = None, recycle_root: Optional[str] = None):
        if CONFIG is not None:
            data_dir = data_dir or CONFIG.get('data_dir', r'D:\短剧工坊\data')
            if material_root is None:
                material_root = (CONFIG.get('material_roots') or
                                 CONFIG.get('download_base_dir', r'E:\短剧素材'))
            output_root = output_root or CONFIG.get('output_dir', r'D:\短剧工作区\output')
            recycle_root = recycle_root or r'D:\短剧工坊\回收站'
        self.data_dir = data_dir
        # 素材根：支持单个字符串或多个合法根（配置里可能存在多套素材目录）
        if isinstance(material_root, (list, tuple)):
            roots = list(material_root)
        else:
            roots = [material_root or r'E:\短剧素材']
        self.material_roots = [os.path.normpath(r) for r in roots if r]
        # 已知历史素材根 D:\短剧素材（若真实存在）也视为合法，避免旧 EDL 配置
        # 里的 D:\短剧素材\... 被判为 source_dir_unsafe 而无法自动清理。
        legacy = r'D:\短剧素材'
        if legacy not in self.material_roots and os.path.isdir(legacy):
            self.material_roots.append(legacy)
        if not self.material_roots:
            self.material_roots = [os.path.normpath(r'E:\短剧素材')]
        # 兼容属性：第一个根（旧代码 / 测试仍引用 self.material_root）
        self.material_root = self.material_roots[0]
        self.output_root = os.path.normpath(output_root or r'D:\短剧工作区\output')
        self.recycle_root = os.path.normpath(recycle_root or r'D:\短剧工坊\回收站')
        # P2-13: 校验回收站根与输出/素材根不重叠（防把输出/素材当回收站互移误删）
        for _r in ([self.output_root] + list(self.material_roots)):
            if _r and (_r == self.recycle_root
                       or self.recycle_root.startswith(_r + os.sep)
                       or _r.startswith(self.recycle_root + os.sep)):
                print(f'WARN: 回收站根 {self.recycle_root} 与 {_r} 重叠，重置为默认')
                self.recycle_root = os.path.normpath(r'D:\短剧工坊\回收站')
                break
        self.db_path = os.path.join(self.data_dir or r'D:\短剧工坊\data', 'upload.db')
        # 清理任务唯一约束：同一批次同一作业类型同一目标路径只允许一条 job。
        # 失败后重复执行清理计划不再插入重复 job（幂等 upsert）。
        try:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM artifact_cleanup_jobs WHERE id NOT IN "
                    "(SELECT MIN(id) FROM artifact_cleanup_jobs "
                    " GROUP BY batch_id, job_type, target_path)")
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_cleanup_jobs "
                    "ON artifact_cleanup_jobs(batch_id, job_type, target_path)")
        except Exception:
            pass

    # ── 路径安全 ──────────────────────────────────────────────
    @staticmethod
    def _norm(path):
        """归一化路径用于比较：大小写不敏感 + 绝对路径 + 斜杠统一。空值返回 ''。"""
        if not path:
            return ''
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
        try:
            rel = os.path.relpath(p_abs, r_abs)
        except ValueError:
            # 跨盘（如 path 在 C:、root 在 D:/E:）——不在该根下，不是逃逸
            return False, f'路径不在允许根目录内: {path}'
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
        """素材目录必须是某个合法素材根下的一级或二级子目录。

        支持多个素材根（material_roots），并允许嵌套两级——部分剧目的素材
        目录布局为 根/分类/剧名（二级），旧的“仅一级子目录”会把它们误判为
        source_dir_unsafe，导致自动清理无法覆盖。
        """
        if not source_dir:
            return False, '素材目录为空'
        p_abs = os.path.abspath(source_dir)
        for root in self.material_roots:
            ok, err = self._safe_under(source_dir, root)
            if not ok:
                continue  # 不在该根下（或就是根本身），试下一个合法根
            rel = os.path.relpath(p_abs, os.path.abspath(root))
            depth = len([p for p in rel.split(os.sep) if p])
            if depth in (1, 2):
                return True, ''
            return False, f'素材目录深度异常（应为根下1-2级）: {rel}'
        return False, f'素材目录不在任何合法素材根内: {source_dir}'

    def _validate_file_under_output(self, file_path):
        """版本级目标文件必须位于 output 根内。"""
        return self._safe_under(file_path, self.output_root)

    # ── DB 辅助 ───────────────────────────────────────────────
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
        except Exception:
            pass  # WAL 失败不致命，回落默认 rollback journal
        return conn

    def _get_batch_records(self, batch_id):
        """读取某批次的所有上传记录。"""
        with self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM upload_records WHERE batch_id=? ORDER BY version',
                (batch_id,)).fetchall()
        return [dict(r) for r in rows]

    def _record_batch(self, batch_id, drama_name, account, source_dir, output_dir,
                      expected_versions=None):
        """批量上传前登记批次（无则创建），并记录该组应上传的版本清单。

        expected_versions: 该组（批次/账号/剧名/目录）本次应上传的全部版本号列表。
        目录级清理前以此判定“全量是否成功”；缺版本则禁止清理整个目录。

        source_dir/output_dir 统一存空字符串而非 NULL：dashboard 不上送 source_dir，
        若存 NULL，SQLite 的 `=` 永不匹配 NULL（且 UNIQUE 约束对 NULL 列失效，
        同组会插入多条重复记录）。空串在查询侧按通配处理（见 _recorded_expected_versions）。
        """
        source_dir = source_dir or ''
        output_dir = output_dir or ''
        with self._connect() as conn:
            # 批次行：同 batch_id 可能含多部剧——drama_name 存逗号合并去重列表，
            # 使批次行的剧名/计数反映全批次（计数在清理写回时按 records 更新）。
            row = conn.execute(
                "SELECT drama_name FROM upload_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if row:
                names = [n for n in (row['drama_name'] or '').split(',') if n]
                if drama_name not in names:
                    names.append(drama_name)
                conn.execute(
                    "UPDATE upload_batches SET account=?, drama_name=?, source_dir=?, output_dir=?, "
                    "status='running' WHERE batch_id=?",
                    (account, ','.join(names), source_dir, output_dir, batch_id))
            else:
                conn.execute(
                    '''INSERT INTO upload_batches
                       (batch_id, account, drama_name, source_dir, output_dir, status)
                       VALUES (?, ?, ?, ?, ?, 'running')''',
                    (batch_id, account, drama_name, source_dir, output_dir))
            if expected_versions:
                import json as _json
                conn.execute(
                    '''INSERT INTO upload_batch_expected
                       (batch_id, account, drama_name, source_dir, output_dir,
                        expected_versions, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                       ON CONFLICT(batch_id, account, drama_name, source_dir, output_dir)
                       DO UPDATE SET expected_versions=excluded.expected_versions,
                                     updated_at=datetime('now')''',
                    (batch_id, account, drama_name, source_dir, output_dir,
                     _json.dumps(sorted(int(v) for v in expected_versions),
                                 ensure_ascii=False)))

    def _recorded_expected_versions(self, batch_id, account, drama_name,
                                    source_dir, output_dir):
        """读取提交时记录的应上传版本清单（JSON 数组）；无记录返回空集。

        存储侧空串（或 NULL）视作通配：dashboard 不上送 source_dir 时该列为空，
        查询用真实素材路径也应能命中（edl_config 第一优先级缺失时的兜底）。
        """
        import json as _json
        with self._connect() as conn:
            row = conn.execute(
                '''SELECT expected_versions FROM upload_batch_expected
                   WHERE batch_id=? AND account=? AND drama_name=? AND output_dir=?
                     AND (COALESCE(source_dir,'') = COALESCE(?,'')
                          OR COALESCE(source_dir,'') = '')''',
                (batch_id, account, drama_name, output_dir, source_dir)).fetchone()
        if not row or not row['expected_versions']:
            return set()
        try:
            return {int(v) for v in _json.loads(row['expected_versions'])}
        except Exception:
            return set()

    def _drama_target_versions(self, out_dir, batch_id, account, drama_name, src_dir):
        """该剧应具备的完整版本集（目录级清理的“全量”基准）。

        优先级：
          1) 成品目录内 edl_config_vN.json 的实际版本集（权威——每个版本切片都写配置）；
          2) 成品目录内 *_V{N}_*_去重版.mp4 的实际版本集；
          3) 提交时记录的 upload_batch_expected；
        都取不到 → 返回空集（调用方应保守地禁止目录级清理）。
        """
        import re
        versions = set()
        if out_dir and os.path.isdir(out_dir):
            try:
                entries = os.listdir(out_dir)
            except OSError as e:
                # P2-12: 目录不可读（权限/占用/脱机）→ 保守返回空集，调用方禁止目录级清理
                print(f'WARN: 读取成品目录失败 {out_dir}: {e}')
                return set()
            for fn in entries:
                m = re.match(r'^edl_config_v(\d+)\.json$', fn, re.I)
                if m:
                    versions.add(int(m.group(1)))
            if not versions:
                for fn in entries:
                    m = re.search(r'_V(\d+)_', fn, re.I)
                    if m and fn.lower().endswith('.mp4'):
                        versions.add(int(m.group(1)))
        if not versions:
            versions = self._recorded_expected_versions(
                batch_id, account, drama_name, src_dir, out_dir)
        return versions

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
            # 全量版本门槛：只有该剧全部目标版本都成功才允许目录级清理。
            # 只提交部分版本（如仅 V1）或某版本文件缺失 → 禁止清理整个目录。
            target_versions = self._drama_target_versions(
                out_dir, batch_id, account, drama_name, src_dir)
            group_versions = {g['version'] for g in grp}
            if target_versions:
                missing = sorted(target_versions - group_versions)
                if missing:
                    skipped.append({'account': account, 'drama_name': drama_name,
                                    'reason': 'expected_versions_incomplete',
                                    'detail': f'目标版本 {sorted(target_versions)} 中缺失/未成功: {missing}'})
                    continue
            else:
                skipped.append({'account': account, 'drama_name': drama_name,
                                'reason': 'expected_versions_unknown',
                                'detail': '无法确定该剧目标版本集（无 edl_config/无提交记录），保守禁止目录级清理'})
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
                    '''INSERT OR IGNORE INTO artifact_cleanup_jobs
                       (batch_id, account, drama_name, job_type, version,
                        target_path, recycle_path, strategy, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')''',
                    (j['batch_id'], j['account'], j['drama_name'], j['job_type'],
                     j['version'], j['target_path'], j['recycle_path'],
                     j.get('strategy', STRATEGY_RECYCLE)))

    def _mark_job_done(self, job, status):
        """执行后回写 artifact_cleanup_jobs.status（防遗留 pending 下次重复执行）。"""
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE artifact_cleanup_jobs SET status=? "
                    "WHERE batch_id=? AND job_type=? AND version=? AND target_path=?",
                    (status, job.get('batch_id'), job.get('job_type'),
                     job.get('version'), job.get('target_path')))
                conn.commit()
        except Exception as e:
            print(f'WARN: 回写清理任务状态失败: {e}')

    def _recover_pending_jobs(self, batch_id):
        """崩溃恢复：执行本批次 artifact_cleanup_jobs 里仍 pending 的遗留任务。

        崩溃后 execute_cleanup 重跑时，上次持久化但未完成的 job 从 DB 恢复执行，
        而非只依赖内存 plan（防 pending 任务永不续跑）。仅限本批次，防跨批次
        测试/历史遗留任务被误执行。返回 (jobs, results)。
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM artifact_cleanup_jobs "
                    "WHERE status='pending' AND batch_id=?",
                    (batch_id,)).fetchall()
        except Exception as e:
            print(f'WARN: 读取遗留清理任务失败: {e}')
            return [], []
        jobs, results = [], []
        for r in rows:
            job = {
                'batch_id': r['batch_id'], 'account': r['account'],
                'drama_name': r['drama_name'], 'job_type': r['job_type'],
                'version': r['version'], 'target_path': r['target_path'],
                'recycle_path': r['recycle_path'],
                'strategy': r['strategy'] or STRATEGY_RECYCLE,
            }
            jobs.append(job)
            res = self._execute_job(job)
            results.append(res)
            self._mark_job_done(job, res.get('status', 'failed'))
        return jobs, results

    # ── 引用检查 ──────────────────────────────────────────────
    def _other_references_source(self, source_dir, exclude_batch_id, exclude_output_dir=None):
        """其他批次/其他组是否仍引用该素材目录。返回引用描述或 None。

        路径比较用 _norm 归一化（大小写不敏感 + 绝对路径 + 斜杠统一），
        否则 Windows 大小写/斜杠方向不同的历史记录会漏判，导致提前删目录。
        """
        target = self._norm(source_dir)
        with self._connect() as conn:
            rows = conn.execute(
                '''SELECT batch_id, account, drama_name, output_dir, source_dir
                   FROM upload_records WHERE status IN ('pending','uploading')''',
                ).fetchall()
            for r in rows:
                if self._norm(r['source_dir'] or '') != target:
                    continue  # 只比较归一化后的素材目录
                if r['batch_id'] == exclude_batch_id and \
                        (exclude_output_dir is None or
                         self._norm(r['output_dir'] or '') == self._norm(exclude_output_dir)):
                    continue  # 本组自己的待上传记录（正常不会有，保守跳过）
                return f"batch={r['batch_id']} 账号={r['account']}"
            # 其他批次已完成但未清理——保守起见也视为引用
            rows2 = conn.execute(
                '''SELECT DISTINCT batch_id, account, source_dir FROM upload_records
                   WHERE batch_id != ?''',
                (exclude_batch_id,)).fetchall()
            for r in rows2:
                if self._norm(r['source_dir'] or '') == target:
                    return f"其他batch={r['batch_id']}"
        return None

    def _other_references_output(self, output_dir, exclude_batch_id, exclude_output_dir):
        """其他批次是否仍有待上传/上传中视频位于该成品目录。返回引用描述或 None。

        关键闸门：整目录（剧集级）清理前，必须确认没有其他批次的待上传任务
        仍引用同一成品目录，否则会过早移走目录。比较走 _norm 归一化。
        """
        if not output_dir:
            return None
        target = self._norm(output_dir)
        with self._connect() as conn:
            rows = conn.execute(
                '''SELECT batch_id, account, drama_name, output_dir FROM upload_records
                   WHERE status IN ('pending','uploading')''',
                ).fetchall()
            for r in rows:
                if self._norm(r['output_dir'] or '') != target:
                    continue
                if r['batch_id'] == exclude_batch_id and \
                        self._norm(r['output_dir'] or '') == self._norm(exclude_output_dir):
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
        """非 dry-run：先计划，再逐任务执行（写 manifest → 移动 → 更新 DB）。

        总体状态由执行结果推导，绝不把有失败项的清理报告成 success：
          - 有 failed 且有 moved → 'partial'
          - 有 failed 且无 moved → 'failed'
          - 无 failed → 'success'
        """
        results = []
        jobs = []
        # P2-14: 崩溃恢复——先执行本批次上次遗留的 pending 任务，再执行本次计划
        try:
            rec_jobs, rec_results = self._recover_pending_jobs(batch_id)
            jobs.extend(rec_jobs)
            results.extend(rec_results)
        except Exception as e:
            print(f'WARN: 恢复遗留清理任务失败: {e}')

        plan = self.plan_cleanup(batch_id, strategy=strategy, dry_run=False)
        if plan['status'] != 'success':
            return plan
        for job in plan['jobs']:
            jobs.append(job)
            results.append(self._execute_job(job))
            self._mark_job_done(job, results[-1].get('status', 'failed'))
        moved = sum(1 for r in results if r['status'] == 'moved')
        failed = sum(1 for r in results if r['status'] == 'failed')
        if failed > 0 and moved > 0:
            overall = 'partial'
        elif failed > 0:
            overall = 'failed'
        else:
            overall = 'success'
        self._write_back_cleanup_state(batch_id, overall, results, jobs)
        return {'status': overall, 'strategy': strategy, 'dry_run': False,
                'jobs': plan['jobs'], 'results': results,
                'moved_count': moved, 'failed_count': failed,
                'skipped': plan['skipped'], 'reason': None}

    def _write_back_cleanup_state(self, batch_id, overall, results, jobs=None):
        """清理执行后写回：upload_records.cleanup_status/cleanup_error +
        upload_batches 的 success/failed/count 与 cleanup_status。

        results/jobs 按 index 对应；由 job 元数据映射记录：
          - version / version_cover 任务 → 该版本记录；
          - drama_output / drama_source 任务 → 该剧全部版本记录。
        """
        try:
            with self._connect() as conn:
                for i, r in enumerate(results):
                    status = r['status']
                    error = r.get('error')
                    job = (jobs[i] if jobs and i < len(jobs) else {}) or {}
                    batch = job.get('batch_id', batch_id)
                    job_type = job.get('job_type')
                    ver = job.get('version')
                    drama = job.get('drama_name')
                    _acc = job.get('account')
                    if job_type in ('version', 'version_cover') and ver is not None:
                        if _acc:
                            conn.execute(
                                "UPDATE upload_records SET cleanup_status=?, cleanup_error=? "
                                "WHERE batch_id=? AND version=? AND account=?",
                                (status, error, batch, ver, _acc))
                        else:
                            conn.execute(
                                "UPDATE upload_records SET cleanup_status=?, cleanup_error=? "
                                "WHERE batch_id=? AND version=?",
                                (status, error, batch, ver))
                    elif job_type in ('drama_output', 'drama_source'):
                        if _acc:
                            conn.execute(
                                "UPDATE upload_records SET cleanup_status=?, cleanup_error=? "
                                "WHERE batch_id=? AND drama_name=? AND account=?",
                                (status, error, batch, drama, _acc))
                        else:
                            conn.execute(
                                "UPDATE upload_records SET cleanup_status=?, cleanup_error=? "
                                "WHERE batch_id=? AND drama_name=?",
                                (status, error, batch, drama))
                # upload_batches 计数 + 整体状态
                cnt = conn.execute(
                    "SELECT COUNT(*), "
                    "SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END), "
                    "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) "
                    "FROM upload_records WHERE batch_id=?",
                    (batch_id,)).fetchone()
                conn.execute(
                    "UPDATE upload_batches SET version_count=?, success_count=?, "
                    "failed_count=?, cleanup_status=? WHERE batch_id=?",
                    (cnt[0] or 0, cnt[1] or 0, cnt[2] or 0, overall, batch_id))
        except Exception as e:
            logger.warning(f'清理状态写回失败: {e}')

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
