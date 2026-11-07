# -*- coding: utf-8 -*-
"""批量上传域：队列状态、入队、草稿箱、账号/剧目列表、启停清理联动。"""
import os
import re
import json
from flask import Blueprint, request, jsonify, Response


def create_upload_bp(deps):
    bp = Blueprint('upload', __name__)
    CONFIG = deps['CONFIG']
    processor = deps['processor']
    upload_manager = deps['upload_manager']
    artifact_cleanup = deps['artifact_cleanup']

    @bp.route('/api/upload/status')
    def get_upload_status():
        """获取上传状态"""
        try:
            return jsonify(upload_manager.get_status())
        except Exception as e:
            return jsonify({'status': 'failed', 'error': str(e)}), 500

    @bp.route('/api/upload/history')
    def get_upload_history():
        """获取上传历史"""
        try:
            limit = request.args.get('limit', 100, type=int)
            return jsonify(upload_manager.get_history(limit))
        except Exception as e:
            return jsonify({'status': 'failed', 'error': str(e)}), 500

    @bp.route('/api/upload/add', methods=['POST'])
    def add_upload_task():
        """添加上传任务"""
        try:
            data = request.get_json()

            required = ['drama_name', 'video_path', 'version']
            for field in required:
                if field not in data:
                    return jsonify({'status': 'failed', 'error': f'缺少字段：{field}'}), 400

            result = upload_manager.add_to_queue(
                drama_name=data['drama_name'],
                video_path=data['video_path'],
                cover_path=data.get('cover_path'),
                title=data.get('title'),
                description=data.get('description'),
                topics=data.get('topics'),
                account=data.get('account'),
                version=data['version'],
                batch_id=data.get('batch_id'),
                source_dir=data.get('source_dir'),
                output_dir=data.get('output_dir'),
                expected_versions=data.get('expected_versions'),
            )

            # 登记批次（幂等），供清理计划器按 batch_id 聚合
            if result.get('status') == 'success' and data.get('batch_id'):
                artifact_cleanup._record_batch(
                    batch_id=data['batch_id'],
                    drama_name=data['drama_name'],
                    account=data.get('account'),
                    source_dir=data.get('source_dir'),
                    output_dir=data.get('output_dir'),
                    expected_versions=data.get('expected_versions'),
                )

            # 直接返回结果，使用Response对象避免jsonify问题
            return Response(json.dumps(result, ensure_ascii=False), mimetype='application/json')
        except Exception as e:
            return Response(json.dumps({'status': 'failed', 'error': str(e)}, ensure_ascii=False), mimetype='application/json', status=500)

    @bp.route('/api/upload/batches', methods=['POST'])
    def add_upload_batch():
        """原子批量添加上传任务（R4）：一次请求创建整批，无半批次。

        请求体：{items: [{drama_name, video_path, version, account?, ...}], batch_id?}
        服务端一次校验（必填/文件存在/重复）+ 一个事务创建全部记录；
        返回 accepted/duplicates/missing/rejected 结构化明细。
        """
        try:
            data = request.get_json(force=True) or {}
            items = data.get('items')
            if not isinstance(items, list) or not items:
                return Response(json.dumps({'status': 'failed', 'error': '缺少 items（非空数组）'},
                                           ensure_ascii=False), mimetype='application/json', status=400)

            result = upload_manager.add_batch(items)

            # 登记批次元数据（幂等，按 batch_id 聚合），供清理计划器使用
            if result.get('status') == 'success' and result.get('accepted'):
                for detail in result.get('accepted_details', []):
                    if detail.get('batch_id'):
                        artifact_cleanup._record_batch(
                            batch_id=detail['batch_id'],
                            drama_name=detail['drama_name'],
                            account=detail.get('account'),
                            source_dir=detail.get('source_dir'),
                            output_dir=detail.get('output_dir'),
                            expected_versions=detail.get('expected_versions'),
                        )

            return Response(json.dumps(result, ensure_ascii=False), mimetype='application/json')
        except Exception as e:
            return Response(json.dumps({'status': 'failed', 'error': str(e)}, ensure_ascii=False), mimetype='application/json', status=500)

    @bp.route('/api/upload/title', methods=['POST'])
    def update_upload_title():
        """手动覆盖任务短标题（dashboard 上传前编辑）。

        请求体：{id: int, title: str}。仅 pending 任务可改；同步内存队列与
        upload_records，覆盖值在 _execute_upload 生效并入库留痕。
        """
        try:
            data = request.get_json(force=True) or {}
            record_id = data.get('id')
            title = data.get('title')
            if record_id is None or not title:
                return Response(json.dumps({'status': 'failed', 'error': '缺少 id 或 title'},
                                           ensure_ascii=False), mimetype='application/json', status=400)
            result = upload_manager.update_task_title(int(record_id), str(title))
            return Response(json.dumps(result, ensure_ascii=False), mimetype='application/json')
        except Exception as e:
            return Response(json.dumps({'status': 'failed', 'error': str(e)},
                                        ensure_ascii=False), mimetype='application/json', status=500)

    @bp.route('/api/upload/open-draft', methods=['POST'])
    def open_draft_box():
        """打开草稿箱浏览器窗口"""
        try:
            data = request.get_json(force=True) or {}
            account = data.get('account')
            if not account:
                return jsonify({'status': 'failed', 'error': '缺少账号参数'}), 400
            result = upload_manager.open_draft_box(account)
            return Response(json.dumps(result, ensure_ascii=False), mimetype='application/json')
        except Exception as e:
            return Response(json.dumps({'status': 'failed', 'error': str(e)}, ensure_ascii=False), mimetype='application/json', status=500)

    @bp.route('/api/upload/close-draft', methods=['POST'])
    def close_draft_box():
        """关闭草稿箱浏览器窗口"""
        try:
            data = request.get_json() or {}
            account = data.get('account')
            if not account:
                return jsonify({'status': 'failed', 'error': '缺少账号参数'}), 400
            result = upload_manager.close_draft_box(account)
            return Response(json.dumps(result, ensure_ascii=False), mimetype='application/json')
        except Exception as e:
            return Response(json.dumps({'status': 'failed', 'error': str(e)}, ensure_ascii=False), mimetype='application/json', status=500)

    @bp.route('/api/upload/draft-status')
    def get_draft_status():
        """获取草稿箱打开状态"""
        try:
            return jsonify(upload_manager.get_draft_status())
        except Exception as e:
            return jsonify({'status': 'failed', 'error': str(e)}), 500

    @bp.route('/api/upload/start', methods=['POST'])
    def start_upload():
        """开始上传"""
        try:
            data = request.get_json(force=True) or {}
            auto_cleanup = bool(data.get('auto_cleanup', False))
            return jsonify(upload_manager.start_upload(auto_cleanup=auto_cleanup))
        except Exception as e:
            return jsonify({'status': 'failed', 'error': str(e)}), 500

    @bp.route('/api/upload/pause', methods=['POST'])
    def pause_upload():
        """暂停上传"""
        try:
            return jsonify(upload_manager.pause_upload())
        except Exception as e:
            return jsonify({'status': 'failed', 'error': str(e)}), 500

    @bp.route('/api/upload/resume', methods=['POST'])
    def resume_upload():
        """恢复上传"""
        return jsonify(upload_manager.resume_upload())

    @bp.route('/api/upload/stop', methods=['POST'])
    def stop_upload():
        """停止上传"""
        return jsonify(upload_manager.stop_upload())

    @bp.route('/api/upload/clear', methods=['POST'])
    def clear_upload_queue():
        """清除队列"""
        try:
            data = request.get_json() or {}
            status = data.get('status', 'pending')
            result = upload_manager.clear_queue(status)
            return Response(json.dumps(result, ensure_ascii=False), mimetype='application/json')
        except Exception as e:
            return Response(json.dumps({'status': 'failed', 'error': str(e)}, ensure_ascii=False), mimetype='application/json', status=500)

    @bp.route('/api/upload/accounts')
    def get_upload_accounts():
        """返回实际存在的上传账号目录 + 每个账号下可上传剧数。

        供上传页账号下拉使用（不再前端硬编码账号集合）。
        账号来自 output 目录实际子目录，保证与切片结果目录一致。
        """
        output_dir = CONFIG.get('output_dir', 'output')
        accounts = []
        if os.path.exists(output_dir):
            for account in sorted(os.listdir(output_dir)):
                account_dir = os.path.join(output_dir, account)
                if not os.path.isdir(account_dir):
                    continue
                count = sum(
                    1 for drama in os.listdir(account_dir)
                    if os.path.isdir(os.path.join(account_dir, drama)))
                accounts.append({'account': account, 'drama_count': count})
        return jsonify({'accounts': accounts})

    @bp.route('/api/upload/dramas')
    def get_uploadable_dramas():
        """获取可上传的剧列表 - 支持按账号过滤，带可解释的空状态诊断。

        除成品列表外，为每个账号返回：
        - plan_account: 后端 assign_account 计划归属（与切片决策一致）
        - edl_pending:  已有 EDL 方案但成品缺失（切片完成后尚未渲染/后处理）
        - stuck_tmp:    成品仍卡在 .mp4.tmp 临时文件（后处理中断）
        - unparsed:     存在 mp4 但版本号解析失败（文件名不符合 _N_ 约定）
        前端据此解释"为什么没有剧集"，而不是只显示空文案。
        """
        output_dir = CONFIG.get('output_dir', 'output')
        account_filter = request.args.get('account')
        submit_order = processor.get_submit_order()
        dramas = []
        diagnostics = []

        if os.path.exists(output_dir):
            _order = submit_order
            for account in sorted(os.listdir(output_dir)):
                account_dir = os.path.join(output_dir, account)
                if not os.path.isdir(account_dir):
                    continue

                for drama in sorted(os.listdir(account_dir)):
                    drama_dir = os.path.join(account_dir, drama)
                    if not os.path.isdir(drama_dir):
                        continue

                    # 交叉轮询：每个剧递增序号，模拟「下一个会切到的剧」的归属
                    plan_account = processor.assign_account(drama, _order)
                    _order += 1
                    versions = []
                    unparsed = []
                    stuck_tmp = []
                    edl_pending = False

                    for file in os.listdir(drama_dir):
                        lower = file.lower()
                        if lower.endswith('.mp4.tmp'):
                            stuck_tmp.append(file)
                            continue
                        if not lower.endswith('.mp4') or 'test' in lower:
                            continue
                        match = re.search(r'_(\d+)_', file)
                        if match:
                            versions.append(int(match.group(1)))
                        else:
                            unparsed.append(file)

                    # EDL 方案存在但无成品 → 规划了但未渲染完成
                    if not versions and not unparsed:
                        edl_pending = any(
                            name.startswith('edl_config_v') and name.endswith('.json')
                            for name in os.listdir(drama_dir))

                    if versions or edl_pending or stuck_tmp or unparsed:
                        entry = {
                            'account': account,
                            'name': drama,
                            'versions': sorted(set(versions)),
                            'version_count': len(set(versions)),
                            'plan_account': plan_account,
                            'path': drama_dir,
                            'edl_pending': edl_pending,
                            'stuck_tmp_count': len(stuck_tmp),
                            'unparsed_count': len(unparsed),
                        }
                        # 仅在请求账号范围内返回；诊断信息随每项附带
                        if not account_filter or account == account_filter:
                            dramas.append(entry)

                    # 全局诊断：计划账号与实际所在目录不一致
                    if account_filter and account == account_filter:
                        if plan_account and plan_account != account:
                            diagnostics.append({
                                'drama': drama,
                                'actual_account': account,
                                'plan_account': plan_account,
                                'reason': f'该剧计划归属 {plan_account}，实际成片在 {account} 目录',
                            })

        return jsonify({'dramas': dramas, 'diagnostics': diagnostics})

    return bp
