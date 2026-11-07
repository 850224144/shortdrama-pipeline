# -*- coding: utf-8 -*-
"""渠道账号扫码登录域。"""
import os
import sys
import re
import subprocess
from datetime import datetime
from flask import Blueprint, request, jsonify


def create_login_bp(deps):
    bp = Blueprint('login', __name__)

    @bp.route('/api/login/check')
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

    @bp.route('/api/login/start', methods=['POST'])
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
            # 蓝图位于 webapp/api/ 子目录，需回退一级得到 webapp 目录（与原 app.py 语义一致）。
            webapp_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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

    return bp
