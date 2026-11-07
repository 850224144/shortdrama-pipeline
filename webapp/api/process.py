# -*- coding: utf-8 -*-
"""主处理流程域：状态、历史、日志、消息提交、停止、健康检查、首页/演示。"""
from flask import Blueprint, request, jsonify, send_from_directory, Response
import json

from health import run_health_checks


def create_process_bp(deps):
    bp = Blueprint('process', __name__)
    CONFIG = deps['CONFIG']
    logger = deps['logger']
    state_manager = deps['state_manager']
    process_controller = deps['process_controller']
    runtime_started_at = deps['runtime_started_at']
    runtime_pid = deps['runtime_pid']

    @bp.route('/')
    def index():
        """首页"""
        return send_from_directory('templates', 'dashboard.html')

    @bp.route('/api/status')
    def get_status():
        """获取当前状态"""
        state = state_manager.load()
        current = process_controller.snapshot()
        return jsonify({
            'current_task': current,
            'completed_count': len(state.get('completed', [])),
            'failed_count': len(state.get('failed', [])),
            'processing': current['processing'],
            'stopping': current['stopping'],
            'job_id': current['job_id'],
            'runtime_started_at': runtime_started_at,
            'runtime_pid': runtime_pid,
        })

    @bp.route('/api/health')
    def get_health():
        """健康检查（R5）：ok/degraded/failed + 每项结果与建议。"""
        try:
            result = run_health_checks(CONFIG)
            return Response(json.dumps(result, ensure_ascii=False),
                            mimetype='application/json')
        except Exception as e:
            return Response(json.dumps({'status': 'failed',
                                        'error': f'健康检查异常: {e}'},
                                       ensure_ascii=False),
                            mimetype='application/json', status=500)

    @bp.route('/api/history')
    def get_history():
        """获取历史记录"""
        limit = request.args.get('limit', 50, type=int)
        return jsonify(state_manager.get_history(limit))

    @bp.route('/api/logs')
    def get_logs():
        """获取日志"""
        limit = request.args.get('limit', 100, type=int)
        return jsonify(logger.get_recent_logs(limit))

    @bp.route('/api/process', methods=['POST'])
    def process_message():
        """处理消息"""
        data = request.get_json()
        message = data.get('message', '')

        if not message:
            return jsonify({'status': 'failed', 'error': '消息为空'}), 400

        body, status_code = process_controller.start(message)
        return jsonify(body), status_code

    @bp.route('/api/cleanup-logs', methods=['POST'])
    def cleanup_logs():
        """清理日志"""
        days = request.json.get('days', 30)
        deleted = logger.cleanup_logs(days)
        return jsonify({'status': 'success', 'deleted': deleted})

    @bp.route('/api/stop', methods=['POST'])
    def stop_processing():
        """请求停止主处理任务。"""
        body, status_code = process_controller.stop()
        return jsonify(body), status_code

    @bp.route('/api/demo')
    def demo():
        """演示页面"""
        return '''
    <!DOCTYPE html>
    <html>
    <head><title>短剧工坊</title></head>
    <body>
        <h1>短剧工坊 Web服务</h1>
        <p>访问 <a href="/">仪表盘</a></p>
        <p>API接口：</p>
        <ul>
            <li>GET /api/status - 获取状态</li>
            <li>GET /api/history - 历史记录</li>
            <li>GET /api/logs - 日志</li>
            <li>POST /api/process - 处理消息</li>
            <li>POST /api/cleanup-logs - 清理日志</li>
        </ul>
    </body>
    </html>
    '''

    return bp
