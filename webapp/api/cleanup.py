# -*- coding: utf-8 -*-
"""成品清理域：清理计划、执行、任务查询。"""
import json
from flask import Blueprint, request, Response

from artifact_cleanup import STRATEGY_NONE, STRATEGY_RECYCLE


def create_cleanup_bp(deps):
    bp = Blueprint('cleanup', __name__)
    artifact_cleanup = deps['artifact_cleanup']

    @bp.route('/api/cleanup/plan', methods=['POST'])
    def cleanup_plan():
        """生成清理计划（默认 dry-run，不移动任何文件）。
        body: {batch_id, strategy?}  strategy 默认 recycle_on_full_batch；
        API 直调若不带 auto_cleanup=true 视为 none（避免误删）。
        """
        try:
            data = request.get_json(force=True) or {}
            batch_id = data.get('batch_id')
            if not batch_id:
                return Response(json.dumps({'status': 'failed', 'error': '缺少 batch_id'},
                                           ensure_ascii=False), mimetype='application/json', status=400)
            # 仅当前端明确勾选自动清理才启用；API 直调默认 none
            strategy = data.get('strategy', STRATEGY_RECYCLE)
            if strategy not in (STRATEGY_RECYCLE, STRATEGY_NONE):
                strategy = STRATEGY_RECYCLE  # P2-17: 白名单校验，客户端非法值回落默认
            if not data.get('auto_cleanup'):
                strategy = STRATEGY_NONE
            plan = artifact_cleanup.plan_cleanup(batch_id, strategy=strategy, dry_run=True)
            return Response(json.dumps(plan, ensure_ascii=False), mimetype='application/json')
        except Exception as e:
            return Response(json.dumps({'status': 'failed', 'error': str(e)},
                                       ensure_ascii=False), mimetype='application/json', status=500)

    @bp.route('/api/cleanup/execute', methods=['POST'])
    def cleanup_execute():
        """执行清理（写 manifest → 移动回收站 → 更新 DB）。
        body: {batch_id, strategy?, auto_cleanup?}  仅 auto_cleanup=true 才实际移动。
        """
        try:
            data = request.get_json(force=True) or {}
            batch_id = data.get('batch_id')
            if not batch_id:
                return Response(json.dumps({'status': 'failed', 'error': '缺少 batch_id'},
                                           ensure_ascii=False), mimetype='application/json', status=400)
            if not data.get('auto_cleanup'):
                return Response(json.dumps({'status': 'failed', 'error': '自动清理未启用（auto_cleanup=false）',
                                            'strategy': STRATEGY_NONE},
                                           ensure_ascii=False), mimetype='application/json')
            strategy = data.get('strategy', STRATEGY_RECYCLE)
            if strategy not in (STRATEGY_RECYCLE, STRATEGY_NONE):
                strategy = STRATEGY_RECYCLE  # P2-17: 白名单校验，客户端非法值回落默认
            result = artifact_cleanup.execute_cleanup(batch_id, strategy=strategy)
            return Response(json.dumps(result, ensure_ascii=False), mimetype='application/json')
        except Exception as e:
            return Response(json.dumps({'status': 'failed', 'error': str(e)},
                                       ensure_ascii=False), mimetype='application/json', status=500)

    @bp.route('/api/cleanup/jobs')
    def cleanup_jobs():
        """查询清理任务（可按 batch_id 过滤）。"""
        try:
            batch_id = request.args.get('batch_id')
            jobs = artifact_cleanup.list_jobs(batch_id)
            return Response(json.dumps({'status': 'success', 'jobs': jobs},
                                       ensure_ascii=False), mimetype='application/json')
        except Exception as e:
            return Response(json.dumps({'status': 'failed', 'error': str(e)},
                                       ensure_ascii=False), mimetype='application/json', status=500)

    return bp
