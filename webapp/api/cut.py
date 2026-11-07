# -*- coding: utf-8 -*-
"""批量切片域：队列状态、剧目扫描、预览/洞察、入队启停、重试、事件、对账、切制参数。"""
import os
import json
from flask import Blueprint, request, jsonify, Response

from cut_preview import build_cut_preview
from cut_insights import build_cut_insights
from processor import DEFAULT_CUT_CONFIG


def create_cut_bp(deps):
    bp = Blueprint('cut', __name__)
    CONFIG = deps['CONFIG']
    processor = deps['processor']
    batch_cut_manager = deps['batch_cut_manager']

    @bp.route('/api/cut/status')
    def get_cut_status():
        """获取切片状态"""
        try:
            return jsonify(batch_cut_manager.get_status())
        except Exception as e:
            return jsonify({'status': 'failed', 'error': str(e)}), 500

    @bp.route('/api/cut/dramas')
    def get_cuttable_dramas():
        """获取可切片的剧列表 - 扫描素材目录"""
        try:
            source_dir = CONFIG.get('download_base_dir', r'E:\短剧素材')
            dramas = []
            submit_order = processor.get_submit_order()

            if os.path.exists(source_dir):
                for drama in sorted(os.listdir(source_dir)):
                    drama_dir = os.path.join(source_dir, drama)
                    if not os.path.isdir(drama_dir):
                        continue

                    # 统计素材集数：递归扫描 + 按集号去重，排除 BaiduPCS 半成品。
                    # 直接 os.listdir 会在历史嵌套素材上少报集数。
                    files = processor._scan_files_recursive(drama_dir)
                    mp4_files = [f for f in processor._dedupe_episodes_by_number(files)
                                 if f.lower().endswith('.mp4')]

                    if len(mp4_files) >= 2:
                        # 交叉轮询：基于已入选剧数递增序号（而非目录位置），
                        # 保证列表里严格「一个剧小桃、下一个福桃」循环。
                        planned_account = processor.assign_account(
                            drama, submit_order + len(dramas))
                        dramas.append({
                            'name': drama,
                            'episodes': len(mp4_files),
                            'path': drama_dir,
                            'account': processor.get_output_subdir(
                                drama, submit_order, account=planned_account),
                        })

            return jsonify({'dramas': dramas})
        except Exception as e:
            return jsonify({'dramas': [], 'error': str(e)}), 500

    @bp.route('/api/cut/preview')
    def get_cut_preview():
        """读取已写入 EDL 的版本方案；纯只读，不启动切片/渲染/上传。"""
        drama_name = request.args.get('drama_name', '')
        try:
            preview = build_cut_preview(
                CONFIG['output_dir'], drama_name,
                source_root=CONFIG.get('download_base_dir'))
        except ValueError as exc:
            return jsonify({'status': 'failed', 'error': str(exc)}), 400
        except Exception as exc:
            return jsonify({'status': 'failed', 'error': str(exc)}), 500
        if preview is None:
            return jsonify({'status': 'not_found', 'error': '尚无切片方案'}), 404
        return Response(json.dumps(preview, ensure_ascii=False),
                        mimetype='application/json')

    @bp.route('/api/cut/insights')
    def get_cut_insights():
        """版本差异、来源覆盖和内容风险只读报告。"""
        drama_name = request.args.get('drama_name', '')
        try:
            insights = build_cut_insights(
                CONFIG['output_dir'], drama_name,
                source_root=CONFIG.get('download_base_dir'),
                db_path=os.path.join(CONFIG.get('data_dir', 'data'), 'upload.db'))
        except ValueError as exc:
            return jsonify({'status': 'failed', 'error': str(exc)}), 400
        except Exception as exc:
            return jsonify({'status': 'failed', 'error': str(exc)}), 500
        if insights is None:
            return jsonify({'status': 'not_found', 'error': '尚无切片方案'}), 404
        return Response(json.dumps(insights, ensure_ascii=False),
                        mimetype='application/json')

    @bp.route('/api/cut/add', methods=['POST'])
    def add_cut_task():
        """添加切片任务"""
        try:
            data = request.get_json()

            drama_name = data.get('drama_name')
            account = data.get('account', '小桃追剧中')
            force = data.get('force', False)
            config = data.get('config') or None

            if not drama_name:
                return jsonify({'status': 'failed', 'error': '缺少剧名'}), 400

            result = batch_cut_manager.add_to_queue(
                drama_name, account, force=force, config=config)

            return Response(json.dumps(result, ensure_ascii=False), mimetype='application/json')
        except Exception as e:
            return Response(json.dumps({'status': 'failed', 'error': str(e)}, ensure_ascii=False), mimetype='application/json', status=500)

    @bp.route('/api/cut/start', methods=['POST'])
    def start_cutting():
        """开始切片"""
        try:
            return jsonify(batch_cut_manager.start_cutting())
        except Exception as e:
            return jsonify({'status': 'failed', 'error': str(e)}), 500

    @bp.route('/api/cut/pause', methods=['POST'])
    def pause_cutting():
        """暂停切片"""
        try:
            return jsonify(batch_cut_manager.pause_cutting())
        except Exception as e:
            return jsonify({'status': 'failed', 'error': str(e)}), 500

    @bp.route('/api/cut/resume', methods=['POST'])
    def resume_cutting():
        """恢复切片"""
        try:
            return jsonify(batch_cut_manager.resume_cutting())
        except Exception as e:
            return jsonify({'status': 'failed', 'error': str(e)}), 500

    @bp.route('/api/cut/stop', methods=['POST'])
    def stop_cutting():
        """停止切片"""
        try:
            return jsonify(batch_cut_manager.stop_cutting())
        except Exception as e:
            return jsonify({'status': 'failed', 'error': str(e)}), 500

    @bp.route('/api/cut/clear', methods=['POST'])
    def clear_cut_queue():
        """清除切片队列"""
        try:
            data = request.get_json() or {}
            status = data.get('status', 'pending')
            result = batch_cut_manager.clear_queue(status)

            return Response(json.dumps(result, ensure_ascii=False), mimetype='application/json')
        except Exception as e:
            return Response(json.dumps({'status': 'failed', 'error': str(e)}, ensure_ascii=False), mimetype='application/json', status=500)

    @bp.route('/api/cut/retry', methods=['POST'])
    def retry_cut_task():
        """失败续跑：重新入队失败剧（只重渲染保留的 edl_config，不重新生成 EDL）。

        与 /api/cut/add 的区别：add 走全量（重新 ASR + 生成 EDL），
        retry 标记 resume=True，manual_cut 读 out_dir 保留的 edl_config_v*.json
        直接重渲染；无保留 config 时自动回退全量重切。
        用户可点「继续」重试失败任务，无需从头来。
        """
        try:
            data = request.get_json() or {}
            drama_name = data.get('drama_name')

            if not drama_name:
                return jsonify({'status': 'failed', 'error': '缺少剧名'}), 400

            account = (data.get('account') or '').strip()
            # 历史失败记录不一定保存账号。优先从已有输出目录推断，避免福桃
            # 的剧被错误对账到小桃目录；没有现场时再走正常题材分类。
            if not account and os.path.isdir(CONFIG['output_dir']):
                for _account_dir in os.listdir(CONFIG['output_dir']):
                    if os.path.isdir(os.path.join(
                            CONFIG['output_dir'], _account_dir, drama_name)):
                        account = _account_dir
                        break
            if not account:
                account = processor.assign_account(drama_name)
            # 历史“重试”默认执行完整重切：旧失败的 EDL 配置可能本身不完整，
            # 或成功版本配置已按设计删除，单纯续跑无法补齐整批。
            resume = bool(data.get('resume', False))
            force = bool(data.get('force', True))

            result = batch_cut_manager.add_to_queue(
                drama_name, account, resume=resume, force=force)
            # 自动开始（若未在切且入队成功）
            if result.get('status') == 'success':
                start = batch_cut_manager.start_cutting()
                if start.get('status') != 'success':
                    result['started'] = False
                    result['start_error'] = start.get('error')
                else:
                    result['started'] = True

            return Response(json.dumps(result, ensure_ascii=False), mimetype='application/json')
        except Exception as e:
            return Response(json.dumps({'status': 'failed', 'error': str(e)}, ensure_ascii=False), mimetype='application/json', status=500)

    @bp.route('/api/cut/events')
    def get_cut_events():
        """查询切片任务持久化事件（R2）。job_id 可选；不带则返回全部（含队列级）。"""
        try:
            job_id = request.args.get('job_id') or None
            try:
                limit = max(1, min(500, int(request.args.get('limit', 100))))
            except (TypeError, ValueError):
                limit = 100
            events = batch_cut_manager.get_events(job_id=job_id, limit=limit)
            return Response(json.dumps({'events': events}, ensure_ascii=False),
                            mimetype='application/json')
        except Exception as e:
            return Response(json.dumps({'status': 'failed', 'error': str(e)}, ensure_ascii=False), mimetype='application/json', status=500)

    @bp.route('/api/cut/reconcile', methods=['POST'])
    def reconcile_cut_queue():
        """手动触发切片状态对账（R3），返回对账报告。"""
        try:
            report = batch_cut_manager.reconcile()
            return Response(json.dumps({'status': 'success', 'report': report},
                                       ensure_ascii=False), mimetype='application/json')
        except Exception as e:
            return Response(json.dumps({'status': 'failed', 'error': str(e)}, ensure_ascii=False), mimetype='application/json', status=500)

    # ── 本剧切制参数（cut_config）────────────────────────────
    @bp.route('/api/cut/config', methods=['GET'])
    def get_cut_config():
        """读取本剧切制参数。

        ?drama=<剧名> → 该剧生效配置（默认合并覆盖，供 UI 回显）
        无 drama → 所有已设置覆盖的列表 + 代码默认值。
        """
        try:
            drama = (request.args.get('drama') or '').strip()
            _store = getattr(batch_cut_manager, 'store', None)
            if not _store or not hasattr(_store, 'get_cut_config'):
                return Response(json.dumps({'status': 'error',
                                            'error': '任务库不支持 cut_config'},
                                           ensure_ascii=False),
                                mimetype='application/json', status=500)
            if drama:
                row = _store.get_cut_config(drama)
                # 合并代码默认：前端总是拿到完整生效值
                effective = batch_cut_manager.processor._resolve_cut_config(row)
                return Response(json.dumps({'status': 'success',
                                            'drama': drama,
                                            'config': effective,
                                            'override': row or None},
                                           ensure_ascii=False),
                                mimetype='application/json')
            rows = _store.list_cut_configs()
            return Response(json.dumps({'status': 'success',
                                        'configs': rows,
                                        'defaults': dict(DEFAULT_CUT_CONFIG)},
                                       ensure_ascii=False),
                            mimetype='application/json')
        except Exception as e:
            return Response(json.dumps({'status': 'failed', 'error': str(e)}, ensure_ascii=False), mimetype='application/json', status=500)

    @bp.route('/api/cut/config', methods=['POST'])
    def post_cut_config():
        """写入/覆盖本剧切制参数。

        body: {drama_name, version_count?, min_dur?, max_dur?, reuse_material?,
               watermark?}。只写非 None 字段（保持旧值）。
        """
        try:
            data = request.get_json() or {}
            drama = (data.get('drama_name') or '').strip()
            if not drama:
                return jsonify({'status': 'failed', 'error': '缺少剧名'}), 400
            _store = getattr(batch_cut_manager, 'store', None)
            if not _store or not hasattr(_store, 'upsert_cut_config'):
                return jsonify({'status': 'failed',
                                'error': '任务库不支持 cut_config'}), 500
            _store.upsert_cut_config(
                drama,
                version_count=data.get('version_count'),
                min_dur=data.get('min_dur'),
                max_dur=data.get('max_dur'),
                reuse_material=data.get('reuse_material'),
                watermark=data.get('watermark'))
            effective = batch_cut_manager.processor._resolve_cut_config(
                _store.get_cut_config(drama))
            return Response(json.dumps({'status': 'success',
                                        'drama': drama,
                                        'config': effective},
                                       ensure_ascii=False),
                            mimetype='application/json')
        except Exception as e:
            return Response(json.dumps({'status': 'failed', 'error': str(e)}, ensure_ascii=False), mimetype='application/json', status=500)

    @bp.route('/api/cut/config', methods=['DELETE'])
    def delete_cut_config():
        """删除本剧切制参数（恢复代码默认）。"""
        try:
            drama = (request.args.get('drama') or '').strip()
            if not drama:
                return jsonify({'status': 'failed', 'error': '缺少剧名'}), 400
            _store = getattr(batch_cut_manager, 'store', None)
            if not _store or not hasattr(_store, 'delete_cut_config'):
                return jsonify({'status': 'failed',
                                'error': '任务库不支持 cut_config'}), 500
            _store.delete_cut_config(drama)
            effective = batch_cut_manager.processor._resolve_cut_config(None)
            return Response(json.dumps({'status': 'success',
                                        'drama': drama,
                                        'config': effective},
                                       ensure_ascii=False),
                            mimetype='application/json')
        except Exception as e:
            return Response(json.dumps({'status': 'failed', 'error': str(e)}, ensure_ascii=False), mimetype='application/json', status=500)

    return bp
