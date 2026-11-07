# -*- coding: utf-8 -*-
"""共享资料库代理蓝图单测：参数映射、响应透传、异常兜底。

不访问网络：`requests.Session` 整体 mock，只验证本代理的行为契约，
上游连通性由人工冒烟（tests/manual 或直接调用真实端点）覆盖。
"""
import json
import unittest
from unittest import mock

import requests
from flask import Flask

from api.dramaid import create_dramaid_bp, DRAMAID_BASE, SEARCH_TIMEOUT


class _FakeResponse:
    def __init__(self, payload=None, status=200, raw=None):
        body = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.content = body
        self.status_code = status


def _client(get_impl):
    """构造只挂载 dramaid 蓝图的最小 Flask app，返回 test_client。"""
    with mock.patch('api.dramaid.requests.Session') as session_cls:
        session_cls.return_value.get = get_impl
        bp = create_dramaid_bp({})
    app = Flask(__name__)
    app.register_blueprint(bp)
    return app.test_client()


class TestDramaidProxyForwarding(unittest.TestCase):
    def test_params_mapped_and_body_passthrough(self):
        payload = {'status': 'ok', 'data': [{'drama_name': '前夫', 'drama_id': 'event/x'}],
                   'total': 1, 'page': 1, 'limit': 5}
        calls = {}

        def fake_get(url, params=None, timeout=None):
            calls['url'] = url
            calls['params'] = params
            calls['timeout'] = timeout
            return _FakeResponse(payload)

        c = _client(fake_get)
        resp = c.get('/api/dramaid/search?keyword=前夫&drama_type=mini&page=2&page_size=5')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, 'application/json')
        self.assertEqual(json.loads(resp.data.decode('utf-8')), payload)
        # 上游路径与参数名：page_size 必须映射成上游的 limit
        self.assertEqual(calls['url'], DRAMAID_BASE + '/api/dramas')
        self.assertEqual(calls['params'], {'keyword': '前夫', 'drama_type': 'mini',
                                           'page': '2', 'limit': '5'})
        self.assertEqual(calls['timeout'], SEARCH_TIMEOUT)

    def test_defaults_when_query_absent(self):
        seen = {}

        def fake_get(url, params=None, timeout=None):
            seen.update(params)
            return _FakeResponse({'data': [], 'total': 0})

        c = _client(fake_get)
        resp = c.get('/api/dramaid/search')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(seen['keyword'], '')
        self.assertEqual(seen['drama_type'], 'series')
        self.assertEqual(seen['page'], '1')
        self.assertEqual(seen['limit'], '20')

    def test_keyword_is_stripped(self):
        seen = {}

        def fake_get(url, params=None, timeout=None):
            seen.update(params)
            return _FakeResponse({'data': []})

        c = _client(fake_get)
        c.get('/api/dramaid/search?keyword=%20%20双胞胎%20%20')
        self.assertEqual(seen['keyword'], '双胞胎')

    def test_upstream_error_status_passthrough(self):
        c = _client(lambda url, params=None, timeout=None: _FakeResponse(
            {'status': 'error', 'error': 'not found'}, status=404))
        resp = c.get('/api/dramaid/search?keyword=x')
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(json.loads(resp.data.decode('utf-8'))['status'], 'error')


class TestDramaidProxyFailure(unittest.TestCase):
    def test_request_exception_returns_502(self):
        def boom(url, params=None, timeout=None):
            raise requests.RequestException('connection reset')

        c = _client(boom)
        resp = c.get('/api/dramaid/search?keyword=x')

        self.assertEqual(resp.status_code, 502)
        body = json.loads(resp.data.decode('utf-8'))
        self.assertEqual(body['status'], 'error')
        self.assertIn('共享资料库连接失败', body['error'])
        self.assertIn('connection reset', body['error'])


class TestDramaidStatus(unittest.TestCase):
    """按内部 ID 查上下架状态：/api/dramaid/status。"""

    def test_numeric_id_forwards_and_passthrough(self):
        payload = {'id': 18657, 'drama_name': '前夫请让路', 'is_published': 1,
                   'deleted_at': None, 'drama_type': 'series', 'drama_id': 'event/x'}
        calls = {}

        def fake_get(url, params=None, timeout=None):
            calls['url'] = url
            calls['timeout'] = timeout
            return _FakeResponse(payload)

        c = _client(fake_get)
        resp = c.get('/api/dramaid/status?id=18657')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, 'application/json')
        self.assertEqual(json.loads(resp.data.decode('utf-8')), payload)
        self.assertEqual(calls['url'], DRAMAID_BASE + '/api/dramas/18657')
        self.assertEqual(calls['timeout'], SEARCH_TIMEOUT)

    def test_non_numeric_or_missing_id_returns_400_without_request(self):
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append(url)
            return _FakeResponse({})

        c = _client(fake_get)
        for qs in ('', '?id=', '?id=abc', '?id=12a3', '?id=劇名'):
            resp = c.get('/api/dramaid/status' + qs)
            self.assertEqual(resp.status_code, 400, f'应拦截: {qs}')
            self.assertIn('id 参数无效', json.loads(resp.data.decode('utf-8'))['error'])
        self.assertEqual(calls, [], '非法 id 不应转发上游')

    def test_upstream_404_passthrough(self):
        upstream = {'error': '剧集不存在'}
        c = _client(lambda url, params=None, timeout=None: _FakeResponse(
            upstream, status=404))
        resp = c.get('/api/dramaid/status?id=999999')
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(json.loads(resp.data.decode('utf-8')), upstream)

    def test_request_exception_returns_502(self):
        def boom(url, params=None, timeout=None):
            raise requests.RequestException('connection reset')

        c = _client(boom)
        resp = c.get('/api/dramaid/status?id=18657')

        self.assertEqual(resp.status_code, 502)
        body = json.loads(resp.data.decode('utf-8'))
        self.assertEqual(body['status'], 'error')
        self.assertIn('共享资料库连接失败', body['error'])


if __name__ == '__main__':
    unittest.main()
