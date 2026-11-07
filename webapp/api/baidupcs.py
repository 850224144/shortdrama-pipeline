# -*- coding: utf-8 -*-
"""BaiduPCS-Go 登录状态域。"""
import os
import subprocess
from flask import Blueprint, jsonify


def create_baidupcs_bp(deps):
    bp = Blueprint('baidupcs', __name__)
    CONFIG = deps['CONFIG']
    logger = deps['logger']

    @bp.route('/api/baidupcs/login', methods=['POST'])
    def login_baidupcs():
        """启动BaiduPCS-Go登录"""
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

    @bp.route('/api/baidupcs/status')
    def get_baidupcs_status():
        """检查BaiduPCS-Go登录状态"""
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

    @bp.route('/api/baidupcs/logout', methods=['POST'])
    def logout_baidupcs():
        """退出BaiduPCS-Go登录"""
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

    return bp
