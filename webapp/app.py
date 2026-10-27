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
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit

# 导入处理器模块
from processor import PipelineProcessor, Logger, StateManager, CONFIG

# 导入上传管理器
from upload_manager import UploadManager

app = Flask(__name__, template_folder='templates', static_folder='static')
socketio = SocketIO(app, cors_allowed_origins="*")

# 初始化目录
for dir_path in [CONFIG['data_dir'], CONFIG['output_dir'], CONFIG['log_dir']]:
    os.makedirs(dir_path, exist_ok=True)

# 初始化组件
logger = Logger(CONFIG['log_dir'], socketio)
state_manager = StateManager(CONFIG['state_file'])
processor = PipelineProcessor(logger, state_manager)

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

# ===== 批量切片管理器 =====
class BatchCutManager:
    """批量切片管理器"""
    
    def __init__(self, processor, data_dir):
        self.processor = processor
        self.data_dir = data_dir
        self.queue = []  # 切片队列
        self.current_task = None  # 当前任务
        self.cutting = False  # 是否正在切片
        self.paused = False  # 是否暂停
        self.completed = []  # 已完成任务
        self.failed = []  # 失败任务
        self.lock = threading.Lock()
        self.cut_thread = None
        self.logs = []
    
    def add_to_queue(self, drama_name, account='小桃追剧中'):
        """添加切片任务到队列"""
        with self.lock:
            # 检查是否已在队列中
            for task in self.queue:
                if task['drama_name'] == drama_name and task['status'] == 'pending':
                    return {'status': 'failed', 'error': f'{drama_name} 已在队列中'}
            
            task = {
                'id': len(self.queue) + 1,
                'drama_name': drama_name,
                'account': account,
                'status': 'pending',
                'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            self.queue.append(task)
            self._log('info', f'添加切片任务：{drama_name}')
            return {'status': 'success', 'id': task['id']}
    
    def start_cutting(self):
        """开始切片"""
        with self.lock:
            if self.cutting:
                return {'status': 'failed', 'error': '已在切片中'}
            
            pending = [t for t in self.queue if t['status'] == 'pending']
            if not pending:
                return {'status': 'failed', 'error': '队列为空'}
            
            self.cutting = True
            self.paused = False
            self._log('info', f'开始切片：{len(pending)}个任务')
            
            # 启动切片线程
            self.cut_thread = threading.Thread(target=self._cut_worker, daemon=True)
            self.cut_thread.start()
            return {'status': 'success'}
    
    def pause_cutting(self):
        """暂停切片"""
        with self.lock:
            if not self.cutting:
                return {'status': 'failed', 'error': '未在切片'}
            
            self.paused = True
            self._log('info', '切片已暂停')
            return {'status': 'success'}
    
    def resume_cutting(self):
        """恢复切片"""
        with self.lock:
            if not self.cutting:
                return {'status': 'failed', 'error': '未在切片'}
            
            self.paused = False
            self._log('info', '切片已恢复')
            return {'status': 'success'}
    
    def stop_cutting(self):
        """停止切片"""
        with self.lock:
            if not self.cutting:
                return {'status': 'failed', 'error': '未在切片'}
            
            self.paused = True
            self.cutting = False
            self.current_task = None
            self._log('info', '切片已停止')
            return {'status': 'success'}
    
    def clear_queue(self, status='pending'):
        """清除队列"""
        with self.lock:
            if status == 'all':
                cleared = len(self.queue)
                self.queue.clear()
                self.completed.clear()
                self.failed.clear()
            else:
                cleared = len([t for t in self.queue if t['status'] == status])
                self.queue = [t for t in self.queue if t['status'] != status]
            
            self._log('info', f'清除队列：{cleared}个任务')
            return {'status': 'success', 'cleared': cleared}
    
    def get_status(self):
        """获取切片状态"""
        with self.lock:
            return {
                'queue_length': len([t for t in self.queue if t['status'] == 'pending']),
                'cutting': self.cutting,
                'paused': self.paused,
                'current_task': self.current_task,
                'completed_count': len(self.completed),
                'failed_count': len(self.failed),
                'logs': self.logs[-50:]
            }
    
    def _cut_worker(self):
        """切片工作线程"""
        while True:
            with self.lock:
                if not self.cutting or self.paused:
                    if not self.cutting:
                        break
                    time.sleep(0.5)
                    continue
                
                # 获取下一个任务
                task = next((t for t in self.queue if t['status'] == 'pending'), None)
                if not task:
                    self.cutting = False
                    self._log('info', '切片完成：所有任务已处理')
                    break
                
                task['status'] = 'cutting'
                task['started_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                self.current_task = task
                self._log('info', f'开始切片：{task["drama_name"]}')
            
            # 执行切片
            try:
                result = self.processor.manual_cut(task['drama_name'])
                
                if result['status'] == 'success':
                    task['status'] = 'completed'
                    task['completed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    task['output_dirs'] = result.get('output_dirs', [])
                    self.completed.append(task['id'])
                    self._log('info', f'切片完成：{task["drama_name"]} - {result.get("cut", 0)}个版本')
                else:
                    task['status'] = 'failed'
                    task['failed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    task['error'] = result.get('error', '未知错误')
                    self.failed.append(task['id'])
                    self._log('error', f'切片失败：{task["drama_name"]} - {task["error"]}')
            except Exception as e:
                task['status'] = 'failed'
                task['failed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                task['error'] = str(e)
                self.failed.append(task['id'])
                self._log('error', f'切片异常：{task["drama_name"]} - {str(e)}')
            
            with self.lock:
                self.current_task = None
    
    def _log(self, level, message):
        """记录日志"""
        log_entry = {
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'level': level,
            'message': message
        }
        self.logs.append(log_entry)
        # 限制日志数量
        if len(self.logs) > 100:
            self.logs = self.logs[-100:]

# 初始化批量切片管理器
batch_cut_manager = BatchCutManager(processor, CONFIG['data_dir'])

# 全局状态
processing_state = {
    'current_task': None,
    'processing': False,
    'progress': 0,
    'message': '',
    'start_time': None
}

# ===== API路由 =====

@app.route('/')
def index():
    """首页"""
    return send_from_directory('templates', 'dashboard.html')

@app.route('/api/status')
def get_status():
    """获取当前状态"""
    state = state_manager.load()
    return jsonify({
        'current_task': processing_state,
        'completed_count': len(state.get('completed', [])),
        'failed_count': len(state.get('failed', [])),
        'processing': processing_state['processing']
    })

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
    
    # 异步处理
    thread = threading.Thread(target=background_process, args=(message,))
    thread.start()
    
    return jsonify({'status': 'started', 'message': '开始处理'})

def background_process(message):
    """后台处理消息"""
    processing_state['processing'] = True
    processing_state['start_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    processing_state['progress'] = 0
    processing_state['message'] = '开始处理'
    
    # 广播状态更新
    socketio.emit('status_update', processing_state)
    
    result = processor.process_message(message)
    
    # 更新状态
    processing_state['processing'] = False
    processing_state['progress'] = 100 if result['status'] == 'success' else 0
    processing_state['message'] = f"完成：{result.get('cut', 0)}个成功，{result.get('failed_cuts', 0)}个失败" if result['status'] != 'failed' else result.get('error', '失败')
    
    # 广播状态更新
    socketio.emit('status_update', processing_state)
    
    # 广播结果
    socketio.emit('process_complete', result)

@app.route('/api/cleanup-logs', methods=['POST'])
def cleanup_logs():
    """清理日志"""
    days = request.json.get('days', 30)
    deleted = logger.cleanup_logs(days)
    return jsonify({'status': 'success', 'deleted': deleted})

@app.route('/api/stop', methods=['POST'])
def stop_processing():
    """停止处理"""
    # TODO: 实现停止逻辑
    return jsonify({'status': 'success', 'message': '停止请求已发送'})

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
            version=data['version']
        )
        
        # 直接返回结果，使用Response对象避免jsonify问题
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
        return jsonify(upload_manager.start_upload())
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

if __name__ == '__main__':
    print(f'短剧工坊 Web服务启动在 http://localhost:8765')
    print(f'仪表盘：http://localhost:8765')
    print(f'演示：http://localhost:8765/api/demo')
    socketio.run(app, host='127.0.0.1', port=8765, debug=False, allow_unsafe_werkzeug=True)