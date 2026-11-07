# -*- coding: utf-8 -*-
"""
短剧工坊 Web 服务入口（阶段一 4.4 收口）。

本文件只负责：Flask / SocketIO 创建、配置加载、组件实例化、Blueprint 注册、
socketio 事件绑定、启动钩子与后台线程、全局错误处理器。具体路由按业务域拆分到
``api/`` 下各蓝图模块，启动后台任务在 ``startup_tasks`` 模块；路由契约
（路径 / 方法 / 返回结构）保持不变。
"""
import os
import atexit
from datetime import datetime
from flask import Flask, jsonify
from flask_socketio import SocketIO

# 处理器模块
from processor import PipelineProcessor, Logger, StateManager
from config import CONFIG

# 队列管理器与任务控制器
from upload_manager import UploadManager
from cut_manager import BatchCutManager
from process_job import ProcessJobController

# 成品清理服务
from artifact_cleanup import ArtifactCleanup, STRATEGY_RECYCLE

# 剧级档案初始化
from archive import init_database as _archive_init

# 配置环境变量覆盖（须在任何组件读 CONFIG 前应用）
from health import apply_env_overrides

# 启动后台钩子
from startup_tasks import (
    start_cut_reconcile,
    start_download_resume,
    start_logs_cleanup,
    start_cleanup_sweep,
)

# 各业务域蓝图工厂
from api.process import create_process_bp
from api.baidupcs import create_baidupcs_bp
from api.archive import create_archive_bp
from api.files import create_files_bp
from api.login import create_login_bp
from api.cleanup import create_cleanup_bp
from api.upload import create_upload_bp
from api.cut import create_cut_bp
from api.dramaid import create_dramaid_bp

apply_env_overrides(CONFIG)

# 重启/切版核验标识：与代码文件的修改时间无关，展示的是当前进程实际启动时间。
RUNTIME_STARTED_AT = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
RUNTIME_PID = os.getpid()

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

# 全局错误处理器 - 确保所有错误都返回 JSON
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
upload_manager.set_cleanup_callback(
    lambda batch_id: artifact_cleanup.execute_cleanup(batch_id, strategy=STRATEGY_RECYCLE)
)

# 初始化批量切片管理器
batch_cut_manager = BatchCutManager(processor, CONFIG['data_dir'],
                                    output_root=CONFIG['output_dir'])

# 主消息处理任务控制器；processing_state 保留为兼容别名。
process_controller = ProcessJobController(processor, on_status=socketio.emit)
processing_state = process_controller.state

# ── 依赖注入容器：蓝图工厂只接收所需组件，不自行实例化，避免循环 import ──
deps = {
    'CONFIG': CONFIG,
    'logger': logger,
    'state_manager': state_manager,
    'processor': processor,
    'process_controller': process_controller,
    'runtime_started_at': RUNTIME_STARTED_AT,
    'runtime_pid': RUNTIME_PID,
    'upload_manager': upload_manager,
    'artifact_cleanup': artifact_cleanup,
    'batch_cut_manager': batch_cut_manager,
}

# ── 注册业务域蓝图 ──
app.register_blueprint(create_process_bp(deps))
app.register_blueprint(create_baidupcs_bp(deps))
app.register_blueprint(create_archive_bp(deps))
app.register_blueprint(create_files_bp(deps))
app.register_blueprint(create_login_bp(deps))
app.register_blueprint(create_cleanup_bp(deps))
app.register_blueprint(create_upload_bp(deps))
app.register_blueprint(create_cut_bp(deps))
app.register_blueprint(create_dramaid_bp(deps))


# ── WebSocket 事件 ──────────────────────────────────────
@socketio.on('connect')
def handle_connect():
    """客户端连接"""
    socketio.emit('status_update', processing_state)


@socketio.on('disconnect')
def handle_disconnect():
    """客户端断开"""
    pass


# ── 启动后台钩子（对账 / 续传 / 日志清理 / 清理补扫）─────
start_cut_reconcile(batch_cut_manager, logger)
start_download_resume(processor, process_controller, logger, CONFIG)
start_logs_cleanup(logger)
start_cleanup_sweep(artifact_cleanup, logger)


if __name__ == '__main__':
    print(f'短剧工坊 Web服务启动在 http://localhost:8765')
    print(f'仪表盘：http://localhost:8765')
    print(f'演示：http://localhost:8765/api/demo')
    socketio.run(app, host='127.0.0.1', port=8765, debug=False, allow_unsafe_werkzeug=True)
