# -*- coding: utf-8 -*-
"""剧级档案域：下载/切片/上传状态追踪、防重下载检查。"""
import os
from flask import Blueprint, request, jsonify

from archive import search_archive, get_archive, should_download


def create_archive_bp(deps):
    bp = Blueprint('archive', __name__)

    @bp.route('/api/archive/search')
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

    @bp.route('/api/archive/get')
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

    @bp.route('/api/archive/download-check')
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

    return bp
