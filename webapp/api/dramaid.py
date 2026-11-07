# -*- coding: utf-8 -*-
"""共享资料库代理：转发 dramaid.lol 的免登录公开搜索 API。

背景：dramaid.lol 响应头带 `Content-Security-Policy: frame-ancestors 'none'`
（浏览器拒绝 iframe 嵌入）且 API 无 CORS 头（跨域 fetch 被拦）。因此前端
既不能直嵌也不能直连该站。本蓝图在服务端转发其免登录公开端点
`/api/dramas`（按剧名搜索剧集 ID），前端同源调用本代理即可，不受上述限制。

仅代理免登录公开端点；需要登录态的 /api/admin/*、/api/sph/* 不在此列。
"""
import json
import requests
from flask import Blueprint, request, Response

# 共享资料库站点（免登录可查的公开搜索接口）
DRAMAID_BASE = 'http://dramaid.lol'
SEARCH_TIMEOUT = 12


def create_dramaid_bp(deps):
    bp = Blueprint('dramaid', __name__)
    _session = requests.Session()

    @bp.route('/api/dramaid/search')
    def dramaid_search():
        """按剧名搜索共享资料库（剧集 ID / 小程序 ID / AI 剧集 ID）。

        query: keyword=<剧名>&drama_type=series|mini|ai&page=1&page_size=20
        返回原站 JSON：{data:[{drama_name,theater_name,drama_id,...}], total,
        page, limit}。转发失败返回 502。
        """
        keyword = (request.args.get('keyword') or '').strip()
        drama_type = request.args.get('drama_type') or 'series'
        page = request.args.get('page') or '1'
        page_size = request.args.get('page_size') or '20'
        try:
            resp = _session.get(
                DRAMAID_BASE + '/api/dramas',
                params={
                    'keyword': keyword,
                    'drama_type': drama_type,
                    'page': page,
                    'limit': page_size,
                },
                timeout=SEARCH_TIMEOUT,
            )
            # 透传原站状态码与 JSON 体；不转发原站 CSP/安全响应头
            return Response(resp.content, status=resp.status_code,
                            mimetype='application/json')
        except requests.RequestException as exc:
            return Response(json.dumps({
                'status': 'error',
                'error': f'共享资料库连接失败：{exc}',
            }, ensure_ascii=False), status=502, mimetype='application/json')

    return bp
