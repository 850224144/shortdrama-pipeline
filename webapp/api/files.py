# -*- coding: utf-8 -*-
"""成品目录文件列表域（路径遍历防护，仅限 output 目录）。"""
import os
from flask import Blueprint, request, jsonify


def create_files_bp(deps):
    bp = Blueprint('files', __name__)
    CONFIG = deps['CONFIG']

    @bp.route('/api/files')
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

    return bp
