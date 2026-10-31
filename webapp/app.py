# -*- coding: utf-8 -*-
"""
短剧工坊 Web服务
功能：消息处理、状态查询、历史记录、实时日志、日志清理
"""
import os
import sys
import time
import threading
import re
import tempfile
import atexit
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit

# 导入处理器模块
from processor import PipelineProcessor, Logger, StateManager, CONFIG

# 导入上传管理器
from upload_manager import UploadManager

# 批量切片队列独立于 Flask 初始化，便于隔离测试
from cut_manager import BatchCutManager

# 主处理任务控制器（真实停止 + 统一资源闸门）
from process_job import ProcessJobController

# 导入成品清理服务
from artifact_cleanup import ArtifactCleanup, STRATEGY_NONE, STRATEGY_RECYCLE

# 导入剧级档案（下载/切片状态追踪 + 防重 + 搜索）
from archive import init_database as _archive_init, search_archive, get_archive, should_download

# 配置环境变量覆盖 + 健康检查（R5）。须在任何组件读 CONFIG 前应用。
from health import apply_env_overrides, run_health_checks

apply_env_overrides(CONFIG)

app = Flask(__name__, template_folder='templates', static_folder='static')
socketio = SocketIO(app, cors_allowed_origins="*")

# 初始化目录
for dir_path in [CONFIG['data_dir'], CONFIG['output_dir'], CONFIG['log_dir']]:
    os.makedirs(dir_path, exist_ok=True)

# 初始化组件
logger = Logger(CONFIG['log_dir'], socketio)
state_manager = StateManager(CONFIG['state_file'])
processor = PipelineProcessor(logger, state_manager)

# 初始化剧级档案表（下载/切片状态追踪）
_archive_init()

# 全局错误处理器 - 确保所有错误都返回JSON
@app.errorhandler(500)
def internal_error(error):
    return jsonify({'status': 'failed', 'error': f'服务器内部错误: {str(error)}'}), 500

@app.errorhandler(404)
def not_found(error):
    return jsonify({'status': 'failed', 'error': '资源未找到'}), 404

@app.errorhandler(405)
def method_not_allowed(error):
    return jsonify({'status': 'failed', 'error': '方法不允许'}), 405

@app.errorhandler(415)
def unsupported_media_type(error):
    return jsonify({'status': 'failed', 'error': '不支持的媒体类型，请使用application/json'}), 415

# 初始化上传管理器
upload_manager = UploadManager(CONFIG['data_dir'])
atexit.register(upload_manager.close_all_draft_boxes)

# 初始化成品清理服务
artifact_cleanup = ArtifactCleanup(CONFIG['data_dir'])
# 批次自然结束时由后端 worker 触发自动清理（前端只展示结果）。
# 回调运行在 upload worker 线程的自然结束路径，仅当 start 时启用了
# auto_cleanup 才被 upload_manager 调用（见 _finalize_batches）。
upload_manager.set_cleanup_callback(
    lambda batch_id: artifact_cleanup.execute_cleanup(batch_id, strategy=STRATEGY_RECYCLE)
)

# 初始化批量切片管理器
batch_cut_manager = BatchCutManager(processor, CONFIG['data_dir'],
                                    output_root=CONFIG['output_dir'])
# 启动状态对账：恢复中断任务、修复错误 running、标记缺成片/可恢复现场
def _startup_cut_reconcile():
    try:
        report = batch_cut_manager.reconcile()
        if report['changed']:
            logger.log('system', 'INFO', f'切片对账：{report}')
    except Exception as e:
        logger.log('system', 'ERROR', f'切片对账失败: {e}')


_reconcile_thread = threading.Thread(target=_startup_cut_reconcile, daemon=True)
_reconcile_thread.start()

# 主消息处理任务控制器；processing_state 保留为兼容别名。
process_controller = ProcessJobController(processor, on_status=socketio.emit)
processing_state = process_controller.state

# ===== API路由 =====

@app.route('/')
def index():
    """首页"""
    return send_from_directory('templates', 'dashboard.html')

@app.route('/api/status')
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
    })

@app.route('/api/health')
def get_health():
    """健康检查（R5）：ok/degraded/failed + 每项结果与建议。"""
    try:
        result = run_health_checks(CONFIG)
        from flask import Response
        import json
        return Response(json.dumps(result, ensure_ascii=False),
                        mimetype='application/json')
    except Exception as e:
        from flask import Response
        import json
        return Response(json.dumps({'status': 'failed',
                                    'error': f'健康检查异常: {e}'},
                                   ensure_ascii=False),
                        mimetype='application/json', status=500)

@app.route('/api/history')
def get_history():
    """获取历史记录"""
    limit = request.args.get('limit', 50, type=int)
    return jsonify(state_manager.get_history(limit))

@app.route('/api/logs')
def get_logs():
    """获取日志"""
    limit = request.args.get('limit', 100, type=int)
    return jsonify(logger.get_recent_logs(limit))

@app.route('/api/process', methods=['POST'])
def process_message():
    """处理消息"""
    data = request.get_json()
    message = data.get('message', '')
    
    if not message:
        return jsonify({'status': 'failed', 'error': '消息为空'}), 400
    
    body, status_code = process_controller.start(message)
    return jsonify(body), status_code

@app.route('/api/cleanup-logs', methods=['POST'])
def cleanup_logs():
    """清理日志"""
    days = request.json.get('days', 30)
    deleted = logger.cleanup_logs(days)
    return jsonify({'status': 'success', 'deleted': deleted})

@app.route('/api/stop', methods=['POST'])
def stop_processing():
    """请求停止主处理任务。"""
    body, status_code = process_controller.stop()
    return jsonify(body), status_code

@app.route('/api/demo')
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

@app.route('/api/baidupcs/login', methods=['POST'])
def login_baidupcs():
    """启动BaiduPCS-Go登录"""
    import subprocess
    baidupcs = CONFIG['baidupcs_path']
    if not os.path.exists(baidupcs):
        return jsonify({'status': 'failed', 'error': 'BaiduPCS-Go未找到'}), 404
    
    try:
        # 启动BaiduPCS-Go登录
        proc = subprocess.Popen([baidupcs, 'login'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        logger.log('system', 'INFO', '已启动BaiduPCS-Go登录')
        return jsonify({'status': 'success', 'message': '登录窗口已启动'})
    except Exception as e:
        return jsonify({'status': 'failed', 'error': str(e)}), 500

@app.route('/api/baidupcs/status')
def get_baidupcs_status():
    """检查BaiduPCS-Go登录状态"""
    import subprocess
    baidupcs = CONFIG['baidupcs_path']
    if not os.path.exists(baidupcs):
        return jsonify({'status': 'failed', 'error': 'BaiduPCS-Go未找到'}), 404
    
    try:
        # 执行命令检查登录状态 (who命令)
        result = subprocess.run([baidupcs, 'who'], capture_output=True, text=True, timeout=10)
        output = result.stdout + result.stderr
        
        # 如果who命令返回账号信息，说明已登录
        if result.returncode == 0 and ('uid' in output.lower() or '账号' in output):
            return jsonify({'status': 'logged_in', 'info': output[:500]})
        else:
            return jsonify({'status': 'not_logged_in', 'info': output[:500]})
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 500

@app.route('/api/baidupcs/logout', methods=['POST'])
def logout_baidupcs():
    """退出BaiduPCS-Go登录"""
    import subprocess
    baidupcs = CONFIG['baidupcs_path']
    if not os.path.exists(baidupcs):
        return jsonify({'status': 'failed', 'error': 'BaiduPCS-Go未找到'}), 404
    
    try:
        # 执行退出命令
        result = subprocess.run([baidupcs, 'logout'], capture_output=True, text=True, timeout=10)
        logger.log('system', 'INFO', 'BaiduPCS-Go已退出登录')
        return jsonify({'status': 'success', 'message': '已退出登录'})
    except Exception as e:
        return jsonify({'status': 'failed', 'error': str(e)}), 500

# ===== 剧级档案（下载/切片/上传状态追踪）=====

@app.route('/api/archive/search')
def api_archive_search():
    """搜索剧档案：q 剧名模糊 + 状态筛选 + 聚合上传状态。"""
    q = request.args.get('q', '').strip() or None
    dl = request.args.get('download_state', '').strip() or None
    sl = request.args.get('slice_state', '').strip() or None
    up = request.args.get('upload_state', '').strip() or None
    limit = request.args.get('limit', 50, type=int)
    try:
        results = search_archive(q=q, download_state=dl, slice_state=sl,
                                 upload_state=up, limit=min(limit, 200))
        return jsonify({'status': 'success', 'results': results, 'count': len(results)})
    except Exception as e:
        return jsonify({'status': 'failed', 'error': str(e)}), 500

@app.route('/api/archive/get')
def api_archive_get():
    """获取单剧档案（含聚合上传状态）。"""
    name = request.args.get('drama_name', '').strip()
    if not name:
        return jsonify({'status': 'failed', 'error': '缺少 drama_name'}), 400
    try:
        d = get_archive(name)
        return jsonify({'status': 'success', 'result': d})
    except Exception as e:
        return jsonify({'status': 'failed', 'error': str(e)}), 500

@app.route('/api/archive/download-check')
def api_archive_download_check():
    """防重下载检查：本地目录已存在 或 历史已下载成功 → 不下载。"""
    name = request.args.get('drama_name', '').strip()
    if not name:
        return jsonify({'status': 'failed', 'error': '缺少 drama_name'}), 400
    local_dir = os.path.join(r'E:\短剧素材', name)
    try:
        ok, reason = should_download(name, local_dir)
        return jsonify({'status': 'success', 'can_download': ok, 'reason': reason,
                        'local_dir': local_dir})
    except Exception as e:
        return jsonify({'status': 'failed', 'error': str(e)}), 500

# WebSocket事件
@socketio.on('connect')
def handle_connect():
    """客户端连接"""
    socketio.emit('status_update', processing_state)

@socketio.on('disconnect')
def handle_disconnect():
    """客户端断开"""
    pass

# ===== 登录API路由 =====

@app.route('/api/login/check')
def check_login_status():
    """检查登录状态"""
    account = request.args.get('account')
    if not account:
        return jsonify({'status': 'failed', 'error': '缺少账号参数'}), 400
    
    try:
        from wechat_uploader import WeChatChannelsUploader
        # 登录进程活着时 check_login 不会开浏览器（profile 锁互斥），
        # 但前端需要这个标志才能停掉轮询——否则登录窗口关了之后还会一直轮。
        in_progress = WeChatChannelsUploader.is_login_in_progress(account)
        uploader = WeChatChannelsUploader(account)
        is_logged_in = uploader.check_login()
        return jsonify({
            'logged_in': is_logged_in,
            'login_in_progress': in_progress,
            'account': account,
        })
    except Exception as e:
        return jsonify({'logged_in': False, 'login_in_progress': False, 'error': str(e)})

@app.route('/api/login/start', methods=['POST'])
def start_login():
    """启动登录流程"""
    data = request.get_json(silent=True) or {}
    account = (data.get('account') or '').strip()

    if not account:
        return jsonify({'status': 'failed', 'error': '缺少账号参数'}), 400

    try:
        # P1-B3：原实现把 account 直接拼进临时 .py 脚本源码——一个引号即可注入
        # 执行任意代码；且 finally 里紧接着 os.unlink(script_path)：Windows 上脚本
        # 正被子进程占用会 PermissionError，让 API 返回 500（登录进程其实已启动）；
        # 删掉成功则子进程读到空文件，登录静默不发生却报 success。
        # 现改为：账号走 argv 传入（不进入源码，天然免疫注入）、不写临时文件、
        # cwd 用绝对路径、stdout/stderr 落到日志文件便于排查。
        import subprocess

        from wechat_uploader import WeChatChannelsUploader

        # 已有同账号登录进程在跑时不重复弹窗口（会互相抢 profile）
        if WeChatChannelsUploader.is_login_in_progress(account):
            return jsonify({
                'status': 'failed',
                'error': f'{account} 已有登录窗口在等待扫码，请先完成它'
            })

        child_script = (
            "import sys\n"
            "from wechat_uploader import WeChatChannelsUploader\n"
            "WeChatChannelsUploader(sys.argv[1]).login()\n"
        )
        webapp_dir = os.path.dirname(os.path.abspath(__file__))
        log_dir = os.path.join(webapp_dir, '..', 'logs')
        os.makedirs(log_dir, exist_ok=True)
        safe_name = re.sub(r'[^0-9A-Za-z\u4e00-\u9fff]+', '_', account)[:40] or 'unknown'
        log_path = os.path.join(
            log_dir, f'login_{safe_name}_{datetime.now():%Y%m%d_%H%M%S}.log')

        with open(log_path, 'ab') as logf:
            subprocess.Popen(
                [sys.executable, '-c', child_script, account],
                cwd=webapp_dir,
                stdout=logf,
                stderr=logf,
            )

        return jsonify({
            'status': 'success',
            'message': f'{account} 登录流程已启动',
            'log': os.path.relpath(log_path, webapp_dir),
        })
    except Exception as e:
        return jsonify({'status': 'failed', 'error': str(e)}), 500

# ===== 上传API路由 =====

@app.route('/api/upload/status')
def get_upload_status():
    """获取上传状态"""
    try:
        return jsonify(upload_manager.get_status())
    except Exception as e:
        return jsonify({'status': 'failed', 'error': str(e)}), 500

@app.route('/api/upload/history')
def get_upload_history():
    """获取上传历史"""
    try:
        limit = request.args.get('limit', 100, type=int)
        return jsonify(upload_manager.get_history(limit))
    except Exception as e:
        return jsonify({'status': 'failed', 'error': str(e)}), 500

@app.route('/api/upload/add', methods=['POST'])
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
        from flask import Response
        import json
        return Response(json.dumps(result, ensure_ascii=False), mimetype='application/json')
    except Exception as e:
        from flask import Response
        import json
        return Response(json.dumps({'status': 'failed', 'error': str(e)}, ensure_ascii=False), mimetype='application/json', status=500)

@app.route('/api/upload/batches', methods=['POST'])
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
            from flask import Response
            import json
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

        from flask import Response
        import json
        return Response(json.dumps(result, ensure_ascii=False), mimetype='application/json')
    except Exception as e:
        from flask import Response
        import json
        return Response(json.dumps({'status': 'failed', 'error': str(e)}, ensure_ascii=False), mimetype='application/json', status=500)

@app.route('/api/upload/open-draft', methods=['POST'])
def open_draft_box():
    """打开草稿箱浏览器窗口"""
    try:
        data = request.get_json(force=True) or {}
        account = data.get('account')
        if not account:
            return jsonify({'status': 'failed', 'error': '缺少账号参数'}), 400
        result = upload_manager.open_draft_box(account)
        from flask import Response
        import json as _json
        return Response(_json.dumps(result, ensure_ascii=False), mimetype='application/json')
    except Exception as e:
        from flask import Response
        import json as _json
        return Response(_json.dumps({'status': 'failed', 'error': str(e)}, ensure_ascii=False), mimetype='application/json', status=500)

@app.route('/api/upload/close-draft', methods=['POST'])
def close_draft_box():
    """关闭草稿箱浏览器窗口"""
    try:
        data = request.get_json() or {}
        account = data.get('account')
        if not account:
            return jsonify({'status': 'failed', 'error': '缺少账号参数'}), 400
        result = upload_manager.close_draft_box(account)
        from flask import Response
        import json as _json
        return Response(_json.dumps(result, ensure_ascii=False), mimetype='application/json')
    except Exception as e:
        from flask import Response
        import json as _json
        return Response(_json.dumps({'status': 'failed', 'error': str(e)}, ensure_ascii=False), mimetype='application/json', status=500)

@app.route('/api/upload/draft-status')
def get_draft_status():
    """获取草稿箱打开状态"""
    try:
        return jsonify(upload_manager.get_draft_status())
    except Exception as e:
        return jsonify({'status': 'failed', 'error': str(e)}), 500

@app.route('/api/upload/start', methods=['POST'])
def start_upload():
    """开始上传"""
    try:
        data = request.get_json(force=True) or {}
        auto_cleanup = bool(data.get('auto_cleanup', False))
        return jsonify(upload_manager.start_upload(auto_cleanup=auto_cleanup))
    except Exception as e:
        return jsonify({'status': 'failed', 'error': str(e)}), 500

@app.route('/api/upload/pause', methods=['POST'])
def pause_upload():
    """暂停上传"""
    try:
        return jsonify(upload_manager.pause_upload())
    except Exception as e:
        return jsonify({'status': 'failed', 'error': str(e)}), 500

@app.route('/api/upload/resume', methods=['POST'])
def resume_upload():
    """恢复上传"""
    return jsonify(upload_manager.resume_upload())

@app.route('/api/upload/stop', methods=['POST'])
def stop_upload():
    """停止上传"""
    return jsonify(upload_manager.stop_upload())

@app.route('/api/upload/clear', methods=['POST'])
def clear_upload_queue():
    """清除队列"""
    from flask import Response
    import json
    try:
        data = request.get_json() or {}
        status = data.get('status', 'pending')
        result = upload_manager.clear_queue(status)
        return Response(json.dumps(result, ensure_ascii=False), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({'status': 'failed', 'error': str(e)}, ensure_ascii=False), mimetype='application/json', status=500)

# ===== 成品清理 API =====
@app.route('/api/cleanup/plan', methods=['POST'])
def cleanup_plan():
    """生成清理计划（默认 dry-run，不移动任何文件）。
    body: {batch_id, strategy?}  strategy 默认 recycle_on_full_batch；
    API 直调若不带 auto_cleanup=true 视为 none（避免误删）。
    """
    from flask import Response
    import json
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

@app.route('/api/cleanup/execute', methods=['POST'])
def cleanup_execute():
    """执行清理（写 manifest → 移动回收站 → 更新 DB）。
    body: {batch_id, strategy?, auto_cleanup?}  仅 auto_cleanup=true 才实际移动。
    """
    from flask import Response
    import json
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

@app.route('/api/cleanup/jobs')
def cleanup_jobs():
    """查询清理任务（可按 batch_id 过滤）。"""
    from flask import Response
    import json
    try:
        batch_id = request.args.get('batch_id')
        jobs = artifact_cleanup.list_jobs(batch_id)
        return Response(json.dumps({'status': 'success', 'jobs': jobs},
                                   ensure_ascii=False), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({'status': 'failed', 'error': str(e)},
                                   ensure_ascii=False), mimetype='application/json', status=500)

@app.route('/api/upload/dramas')
def get_uploadable_dramas():
    """获取可上传的剧列表 - 支持按账号过滤"""
    # 从output目录扫描剧目录
    output_dir = CONFIG.get('output_dir', 'output')
    account_filter = request.args.get('account')  # 获取账号参数
    dramas = []
    
    if os.path.exists(output_dir):
        for account in os.listdir(output_dir):
            # 如果指定了账号，只扫描该账号
            if account_filter and account != account_filter:
                continue
            
            account_dir = os.path.join(output_dir, account)
            if not os.path.isdir(account_dir):
                continue
            
            for drama in os.listdir(account_dir):
                drama_dir = os.path.join(account_dir, drama)
                if not os.path.isdir(drama_dir):
                    continue
                
                # 统计版本数量 - 只统计实际存在的视频文件
                versions = []
                for file in os.listdir(drama_dir):
                    if file.endswith('.mp4') and 'test' not in file.lower():
                        # 提取版本号
                        match = re.search(r'_(\d+)_', file)
                        if match:
                            versions.append(int(match.group(1)))
                
                if versions:
                    dramas.append({
                        'account': account,
                        'name': drama,
                        'versions': sorted(versions),
                        'version_count': len(versions),
                        'path': drama_dir
                    })
    
    return jsonify({'dramas': dramas})

@app.route('/api/files')
def get_files():
    """获取目录中的文件列表 - 限制只能访问output目录"""
    try:
        dir_path = request.args.get('dir')
        
        # 调试：打印收到的参数
        print(f"[DEBUG] get_files 收到 dir_path: {dir_path}")
        print(f"[DEBUG] 原始查询字符串: {request.query_string.decode()}")
        
        if not dir_path:
            return jsonify({'files': [], 'error': '缺少dir参数'})
        
        if not os.path.exists(dir_path):
            return jsonify({'files': [], 'error': f'目录不存在: {dir_path}'})
        
        # 路径遍历安全检查 - 只允许访问output目录（用 realpath + 分隔符边界，防 output_备份 这类兄弟目录绕过）
        allowed_dir = os.path.realpath(CONFIG['output_dir'])
        requested_dir = os.path.realpath(dir_path)
        if requested_dir != allowed_dir and not requested_dir.startswith(allowed_dir + os.sep):
            return jsonify({'files': [], 'error': f'无权访问: {requested_dir} 不在 {allowed_dir} 中'}), 403
        
        files = os.listdir(dir_path)
        return jsonify({'files': files})
    except Exception as e:
        return jsonify({'files': [], 'error': str(e)}), 500

# ===== 批量切片API路由 =====

@app.route('/api/cut/status')
def get_cut_status():
    """获取切片状态"""
    try:
        return jsonify(batch_cut_manager.get_status())
    except Exception as e:
        return jsonify({'status': 'failed', 'error': str(e)}), 500

@app.route('/api/cut/dramas')
def get_cuttable_dramas():
    """获取可切片的剧列表 - 扫描素材目录"""
    try:
        source_dir = CONFIG.get('download_base_dir', r'E:\短剧素材')
        dramas = []
        
        if os.path.exists(source_dir):
            for drama in os.listdir(source_dir):
                drama_dir = os.path.join(source_dir, drama)
                if not os.path.isdir(drama_dir):
                    continue
                
                # 统计素材集数
                files = os.listdir(drama_dir)
                mp4_files = [f for f in files if f.endswith('.mp4')]
                
                if len(mp4_files) >= 2:
                    dramas.append({
                        'name': drama,
                        'episodes': len(mp4_files),
                        'path': drama_dir
                    })
        
        return jsonify({'dramas': dramas})
    except Exception as e:
        return jsonify({'dramas': [], 'error': str(e)}), 500

@app.route('/api/cut/add', methods=['POST'])
def add_cut_task():
    """添加切片任务"""
    try:
        data = request.get_json()
        
        drama_name = data.get('drama_name')
        account = data.get('account', '小桃追剧中')
        
        if not drama_name:
            return jsonify({'status': 'failed', 'error': '缺少剧名'}), 400
        
        result = batch_cut_manager.add_to_queue(drama_name, account)
        
        from flask import Response
        import json
        return Response(json.dumps(result, ensure_ascii=False), mimetype='application/json')
    except Exception as e:
        from flask import Response
        import json
        return Response(json.dumps({'status': 'failed', 'error': str(e)}, ensure_ascii=False), mimetype='application/json', status=500)

@app.route('/api/cut/start', methods=['POST'])
def start_cutting():
    """开始切片"""
    try:
        return jsonify(batch_cut_manager.start_cutting())
    except Exception as e:
        return jsonify({'status': 'failed', 'error': str(e)}), 500

@app.route('/api/cut/pause', methods=['POST'])
def pause_cutting():
    """暂停切片"""
    try:
        return jsonify(batch_cut_manager.pause_cutting())
    except Exception as e:
        return jsonify({'status': 'failed', 'error': str(e)}), 500

@app.route('/api/cut/resume', methods=['POST'])
def resume_cutting():
    """恢复切片"""
    try:
        return jsonify(batch_cut_manager.resume_cutting())
    except Exception as e:
        return jsonify({'status': 'failed', 'error': str(e)}), 500

@app.route('/api/cut/stop', methods=['POST'])
def stop_cutting():
    """停止切片"""
    try:
        return jsonify(batch_cut_manager.stop_cutting())
    except Exception as e:
        return jsonify({'status': 'failed', 'error': str(e)}), 500

@app.route('/api/cut/clear', methods=['POST'])
def clear_cut_queue():
    """清除切片队列"""
    try:
        data = request.get_json() or {}
        status = data.get('status', 'pending')
        result = batch_cut_manager.clear_queue(status)
        
        from flask import Response
        import json
        return Response(json.dumps(result, ensure_ascii=False), mimetype='application/json')
    except Exception as e:
        from flask import Response
        import json
        return Response(json.dumps({'status': 'failed', 'error': str(e)}, ensure_ascii=False), mimetype='application/json', status=500)

@app.route('/api/cut/retry', methods=['POST'])
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
        account = data.get('account', '小桃追剧中')
        
        if not drama_name:
            return jsonify({'status': 'failed', 'error': '缺少剧名'}), 400
        
        result = batch_cut_manager.add_to_queue(drama_name, account, resume=True)
        # 自动开始（若未在切且入队成功）
        if result.get('status') == 'success':
            start = batch_cut_manager.start_cutting()
            if start.get('status') != 'success':
                result['started'] = False
                result['start_error'] = start.get('error')
            else:
                result['started'] = True
        
        from flask import Response
        import json
        return Response(json.dumps(result, ensure_ascii=False), mimetype='application/json')
    except Exception as e:
        from flask import Response
        import json
        return Response(json.dumps({'status': 'failed', 'error': str(e)}, ensure_ascii=False), mimetype='application/json', status=500)

@app.route('/api/cut/events')
def get_cut_events():
    """查询切片任务持久化事件（R2）。job_id 可选；不带则返回全部（含队列级）。"""
    try:
        job_id = request.args.get('job_id') or None
        try:
            limit = max(1, min(500, int(request.args.get('limit', 100))))
        except (TypeError, ValueError):
            limit = 100
        from flask import Response
        import json
        events = batch_cut_manager.get_events(job_id=job_id, limit=limit)
        return Response(json.dumps({'events': events}, ensure_ascii=False),
                        mimetype='application/json')
    except Exception as e:
        from flask import Response
        import json
        return Response(json.dumps({'status': 'failed', 'error': str(e)}, ensure_ascii=False), mimetype='application/json', status=500)

@app.route('/api/cut/reconcile', methods=['POST'])
def reconcile_cut_queue():
    """手动触发切片状态对账（R3），返回对账报告。"""
    try:
        from flask import Response
        import json
        report = batch_cut_manager.reconcile()
        return Response(json.dumps({'status': 'success', 'report': report},
                                   ensure_ascii=False), mimetype='application/json')
    except Exception as e:
        from flask import Response
        import json
        return Response(json.dumps({'status': 'failed', 'error': str(e)}, ensure_ascii=False), mimetype='application/json', status=500)

# 后台任务
def cleanup_logs_background():
    """后台清理日志"""
    while True:
        time.sleep(3600 * 24)  # 每小时检查一次
        deleted = logger.cleanup_logs(30)
        if deleted > 0:
            logger.log('system', 'INFO', f'清理了{deleted}个过期日志文件')

# 启动后台任务
cleanup_thread = threading.Thread(target=cleanup_logs_background, daemon=True)
cleanup_thread.start()


def startup_cleanup_sweep():
    """服务启动补扫：上次进程结束前已标记完成且启用自动清理、但清理未落地的批次。

    处理「服务重启后自动清理不会执行」的缺口（#4）：后端 worker 在批次自然结束时
    同步执行清理，正常不会遗留；仅当进程在「标记完成」与「清理执行」之间崩溃时，
    此补扫在下一次启动时补跑。execute_cleanup 幂等（已回收的路径不会重复移动）。
    """
    try:
        for batch_id in artifact_cleanup.list_startup_cleanup_batches():
            try:
                logger.log('system', 'INFO', f'启动补扫：批次 {batch_id} 补跑自动清理')
                artifact_cleanup.execute_cleanup(batch_id, strategy=STRATEGY_RECYCLE)
            except Exception as e:
                logger.log('system', 'ERROR', f'启动补扫：批次 {batch_id} 清理失败: {e}')
    except Exception as e:
        logger.log('system', 'ERROR', f'启动补扫失败: {e}')


_sweep_thread = threading.Thread(target=startup_cleanup_sweep, daemon=True)
_sweep_thread.start()

if __name__ == '__main__':
    print(f'短剧工坊 Web服务启动在 http://localhost:8765')
    print(f'仪表盘：http://localhost:8765')
    print(f'演示：http://localhost:8765/api/demo')
    socketio.run(app, host='127.0.0.1', port=8765, debug=False, allow_unsafe_werkzeug=True)
