# -*- coding: utf-8 -*-
"""dramaid_client.lookup_best 错误语义单测。

背景（BUG）：lookup_best 把网络失败/非200 静默吞成 None，resolve 记为
'none'，与「真查不到」无法区分 → 能查到的剧集 ID 被当查不到跳过填链接
（2026-11-11 深夜批次全部 drama_id=None 即此因）。
修复后：网络失败/非200/解析失败**抛异常**（resolve 记为 error 并重试），
仅「上游确实无匹配」返回 None。
"""
import tempfile
import unittest
from unittest import mock

import requests

from dramaid_client import DramaidClient


class _Resp:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data or {}

    def json(self):
        return self._data


class TestLookupBestErrorSemantics(unittest.TestCase):
    def _client(self):
        return DramaidClient(tempfile.mkdtemp(prefix='dramaid_lookup_'))

    def test_network_error_raises(self):
        """网络失败必须抛异常，不得吞成 None。"""
        c = self._client()
        with mock.patch.object(c, 'search',
                               side_effect=requests.RequestException('网络超时')):
            with self.assertRaises(requests.RequestException):
                c.lookup_best('某剧')

    def test_non200_raises(self):
        """非 200 必须抛异常（记为 error 重试），不得当查不到。"""
        c = self._client()
        with mock.patch.object(c, 'search', return_value=_Resp(status_code=500)):
            with self.assertRaises(RuntimeError):
                c.lookup_best('某剧')

    def test_no_items_returns_none(self):
        """上游确实无匹配 → None（真查不到，不抛）。"""
        c = self._client()
        with mock.patch.object(c, 'search', return_value=_Resp(data={'data': []})):
            self.assertIsNone(c.lookup_best('某剧'))

    def test_exact_match_preferred(self):
        """精确剧名优先（多条时取精确匹配）。"""
        c = self._client()
        items = [
            {'drama_id': 'a', 'drama_name': '别名'},
            {'drama_id': 'b', 'drama_name': '某剧'},
        ]
        with mock.patch.object(c, 'search', return_value=_Resp(data={'data': items})):
            it = c.lookup_best('某剧')
        assert it is not None
        self.assertEqual(it['drama_id'], 'b')

    def test_first_item_when_no_exact(self):
        """无精确匹配 → 取首条已上架。"""
        c = self._client()
        items = [{'drama_id': 'a', 'drama_name': '别名'}]
        with mock.patch.object(c, 'search', return_value=_Resp(data={'data': items})):
            it = c.lookup_best('某剧')
        assert it is not None
        self.assertEqual(it['drama_id'], 'a')


class TestResolveSeriesFallbackToMini(unittest.TestCase):
    """resolve() 的 series→mini_drama 回退（A3 补充）回归。

    背景（BUG）：resolve 只查 drama_type='series'，部分剧在库里只有
    mini_drama 条目（如「她断十二载交情」），series 查无 → 误判「查不到」
    → 跳过填链接 → 上传成功但剧集ID 空。修复后 series 无匹配回退 mini_drama。
    """

    def _client(self):
        return DramaidClient(tempfile.mkdtemp(prefix='dramaid_resolve_'))

    def _mk_search(self, by_type):
        """by_type: dict drama_type -> _Resp | Exception。返回 side_effect。"""
        def _search(keyword, drama_type='series', **kw):
            val = by_type.get(drama_type)
            if isinstance(val, Exception):
                raise val
            return val
        return _search

    def test_series_none_falls_back_to_mini_drama(self):
        """series 无匹配 → 回退 mini_drama 命中 → autolookup。"""
        c = self._client()
        items = [{'drama_id': 'wx111', 'drama_name': '某剧', 'is_published': 1}]
        by_type = {'series': _Resp(data={'data': []}),
                   'mini_drama': _Resp(data={'data': items})}
        with mock.patch.object(c, 'search', side_effect=self._mk_search(by_type)):
            drama_id, source, matched, err = c.resolve('某剧')
        self.assertEqual(drama_id, 'wx111')
        self.assertEqual(source, 'autolookup')
        self.assertEqual(matched, '某剧')
        self.assertIsNone(err)

    def test_series_match_does_not_query_mini_drama(self):
        """series 命中 → 直接用，不再查 mini_drama（行为不变，不加请求）。"""
        c = self._client()
        items = [{'drama_id': 'event/abc', 'drama_name': '某剧', 'is_published': 1}]
        with mock.patch.object(c, 'search', return_value=_Resp(data={'data': items})) as m:
            drama_id, source, _, _ = c.resolve('某剧')
        self.assertEqual(drama_id, 'event/abc')
        self.assertEqual(source, 'autolookup')
        m.assert_called_once()
        self.assertEqual(m.call_args.kwargs.get('drama_type'), 'series')

    def test_both_none_returns_none(self):
        """series+mini 均无匹配 → none（真查不到）。"""
        c = self._client()
        by_type = {'series': _Resp(data={'data': []}),
                   'mini_drama': _Resp(data={'data': []})}
        with mock.patch.object(c, 'search', side_effect=self._mk_search(by_type)):
            drama_id, source, _, err = c.resolve('某剧')
        self.assertIsNone(drama_id)
        self.assertEqual(source, 'none')
        self.assertIn('未找到', err)

    def test_series_network_error_no_mini_fallback(self):
        """series 网络失败 → error，不回退 mini_drama（不把瞬时故障当查不到）。"""
        c = self._client()
        by_type = {'series': requests.RequestException('网络超时')}
        with mock.patch.object(c, 'search', side_effect=self._mk_search(by_type)) as m:
            drama_id, source, _, err = c.resolve('某剧')
        self.assertIsNone(drama_id)
        self.assertEqual(source, 'error')
        self.assertIn('查询失败', err)
        m.assert_called_once()

    def test_explicit_mini_drama_no_series_fallback(self):
        """显式指定 mini_drama → 只查 mini_drama，不回退 series。"""
        c = self._client()
        by_type = {'mini_drama': _Resp(data={'data': []})}
        with mock.patch.object(c, 'search', side_effect=self._mk_search(by_type)) as m:
            drama_id, source, _, _ = c.resolve('某剧', drama_type='mini_drama')
        self.assertIsNone(drama_id)
        self.assertEqual(source, 'none')
        m.assert_called_once()
        self.assertEqual(m.call_args.kwargs.get('drama_type'), 'mini_drama')

    def test_bound_used_before_lookup(self):
        """已有绑定优先，不发网络查询。"""
        c = self._client()
        c.bind('某剧', 'bound/xyz', drama_type='series')
        with mock.patch.object(c, 'search', return_value=_Resp(data={'data': []})) as m:
            drama_id, source, _, _ = c.resolve('某剧')
        self.assertEqual(drama_id, 'bound/xyz')
        self.assertEqual(source, 'bound')
        m.assert_not_called()


if __name__ == '__main__':
    unittest.main()
