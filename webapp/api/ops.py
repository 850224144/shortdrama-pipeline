# -*- coding: utf-8 -*-
"""运维域接口：运行指标（P2-11）与 EDL 审批（P2-9）。

拆成独立蓝图的原因：这两类接口都是“读多写少”的运维面，不参与下载/切片
主流程；放在同一处便于后续加告警与日报，不污染 process/cut 蓝图。
"""
import glob
import json
import os

from flask import Blueprint, Response, request

import metrics as metrics_mod


def _json(data, status=200):
    return Response(json.dumps(data, ensure_ascii=False),
                    mimetype='application/json', status=status)


def _find_out_dir(output_root, drama_name, account=None):
    """在 output 根下找某剧的成片目录（account 优先，其次任意账号目录）。"""
    candidates = []
    if account:
        candidates.append(os.path.join(output_root, account, drama_name))
    candidates.append(os.path.join(output_root, drama_name))
    candidates.extend(sorted(glob.glob(
        os.path.join(output_root, '*', drama_name))))
    for path in candidates:
        if os.path.isdir(path):
            return path
    return None


def create_ops_bp(deps):
    bp = Blueprint('ops', __name__)
    CONFIG = deps['CONFIG']
    processor = deps['processor']
    jobs_db = os.path.join(CONFIG['data_dir'], 'jobs.db')

    @bp.route('/api/metrics')
    def get_metrics():
        """运行指标：按任务类型统计状态/耗时/成功率 + 失败清单 + 最慢任务。"""
        date = request.args.get('date')
        days = request.args.get('days', 1, type=int)
        try:
            data = metrics_mod.collect(jobs_db, date=date, days=days)
        except Exception as e:
            return _json({'status': 'failed', 'error': str(e)}, 500)
        return _json(data)

    @bp.route('/api/cut/edl/approve', methods=['POST'])
    def approve_edl():
        """批准/撤销某一版 EDL。body: drama_name, version, approved, account。"""
        data = request.get_json(silent=True) or {}
        drama_name = str(data.get('drama_name') or '').strip()
        if not drama_name:
            return _json({'status': 'failed', 'error': 'drama_name 必填'}, 400)
        try:
            version = int(data.get('version'))
        except (TypeError, ValueError):
            return _json({'status': 'failed', 'error': 'version 必须是整数'}, 400)
        approved = bool(data.get('approved', True))
        out_dir = _find_out_dir(CONFIG['output_dir'], drama_name,
                                data.get('account'))
        if not out_dir:
            return _json({'status': 'failed',
                          'error': f'未找到成片目录：{drama_name}'}, 404)
        ok, payload = processor.approve_edl(out_dir, version,
                                            approved=approved,
                                            by=str(data.get('by') or 'api'))
        return _json({'status': 'success' if ok else 'failed',
                      'out_dir': out_dir, **payload}, 200 if ok else 400)

    return bp
