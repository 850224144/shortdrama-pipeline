# -*- coding: utf-8 -*-
"""dramaid.lol 共用客户端：查询 + 「剧名 → 剧集 ID」绑定存储。

供 api/dramaid.py 蓝图与 upload_manager（保存草稿前自动填剧集链接）共用，
集中 dramaid.lol 的网络访问与绑定表逻辑，避免重复与常量漂移。

背景：dramaid.lol 响应头带 `Content-Security-Policy: frame-ancestors 'none'`
（浏览器拒绝 iframe 嵌入）且 API 无 CORS 头（跨域 fetch 被拦），因此前端
既不能直嵌也不能直连该站，必须服务端代理。本客户端只碰免登录公开端点；
需要登录态的 /api/admin/*、/api/sph/* 不在此列。
"""
import os
import sqlite3

import requests

DRAMAID_BASE = 'http://dramaid.lol'
SEARCH_TIMEOUT = 12
BIND_DB = 'dramaid.db'  # 相对 data_dir

# drama_type 合法值（实测：mini/ai 会返回「剧集类型参数无效」）
VALID_DRAMA_TYPES = ('series', 'mini_drama', 'ai_drama')


class DramaidClient:
    """dramaid.lol 查询 + 剧集 ID 绑定。线程安全由调用方保证（Flask 单线程 worker 场景）。"""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.db_path = os.path.join(data_dir, BIND_DB)
        self._session = requests.Session()
        self._init_db()

    # ── 查询（上游透传） ──────────────────────────────────────────

    def search(self, keyword, drama_type='series', page=1, page_size=20):
        """调上游搜索，返回 requests.Response（透传状态码与 JSON 体）。

        网络异常抛 requests.RequestException；上游 4xx/5xx 不抛（由调用方
        决定透传 status 还是判无匹配）。
        """
        return self._session.get(
            DRAMAID_BASE + '/api/dramas',
            params={
                'keyword': keyword,
                'drama_type': drama_type,
                'page': page,
                'limit': page_size,
            },
            timeout=SEARCH_TIMEOUT,
        )

    def get_detail(self, drama_id):
        """按内部数字 ID 查单部剧集详情，返回 requests.Response。"""
        return self._session.get(
            DRAMAID_BASE + '/api/dramas/' + drama_id, timeout=SEARCH_TIMEOUT)

    def lookup_best(self, drama_name, drama_type='series'):
        """按剧名取最佳匹配（精确剧名优先，否则首个已上架）。

        返回上游 item dict（含 drama_id/drama_name/is_published...）或 None。
        None **仅表示上游确实无匹配**；网络失败/非 200/解析失败**抛异常**，
        由 resolve 记为 'error' 并重试——避免把瞬时网络故障误判成「查不到」
        而跳过填链接（能查到的剧集 ID 必须填进去）。
        """
        resp = self.search(drama_name, drama_type=drama_type, page=1, page_size=20)
        if resp.status_code != 200:
            raise RuntimeError(f'共享资料库返回 HTTP {resp.status_code}')
        data = resp.json()  # 解析失败抛异常，同样记为 error
        items = (data or {}).get('data') or []
        if not items:
            return None
        target = drama_name.strip()
        for it in items:
            if (it.get('drama_name') or '').strip() == target:
                return it
        return items[0]

    # ── 绑定存储（剧名 → 剧集 ID） ───────────────────────────────

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS drama_id_map (
                    drama_name TEXT PRIMARY KEY,
                    drama_id   TEXT NOT NULL,
                    drama_type TEXT DEFAULT 'series',
                    source     TEXT DEFAULT 'manual',   -- manual | autolookup
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()
        finally:
            conn.close()

    def bind(self, drama_name, drama_id, drama_type='series', source='manual'):
        """绑定剧名 → 剧集 ID（幂等 upsert）。"""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute('''
                INSERT INTO drama_id_map (drama_name, drama_id, drama_type, source, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(drama_name) DO UPDATE SET
                    drama_id=excluded.drama_id,
                    drama_type=excluded.drama_type,
                    source=excluded.source,
                    updated_at=CURRENT_TIMESTAMP
            ''', (drama_name.strip(), drama_id.strip(), drama_type, source))
            conn.commit()
        finally:
            conn.close()

    def get_bound(self, drama_name):
        """查单部剧的绑定；无返回 None。"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                'SELECT * FROM drama_id_map WHERE drama_name=?',
                (drama_name.strip(),)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_bound(self):
        """全部绑定，按更新时间倒序。"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                'SELECT * FROM drama_id_map ORDER BY updated_at DESC').fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def resolve(self, drama_name, drama_type='series'):
        """发布前解析剧集 ID（A3 混合语义）。

        已有绑定 → 用绑定；无绑定 → 自动查上游首条（本次用，不落库）。
        series 无匹配时回退 mini_drama（A3 补充）：部分剧在库里只有
        mini_drama 条目（如「她断十二载交情」），只查 series 会误判
        「查不到」而跳过填链接。ai_drama 需登录态，不做自动回退。
        返回 (drama_id, source, matched_name, error)：
          drama_id 可为 None（未找到）；source ∈ bound|autolookup|none|error。
        """
        bound = self.get_bound(drama_name)
        if bound and bound.get('drama_id'):
            return (bound['drama_id'], 'bound', bound.get('drama_name') or drama_name, None)
        # series 无匹配时回退 mini_drama；显式指定其它类型则不回退。
        types = ('series', 'mini_drama') if drama_type == 'series' else (drama_type,)
        for dt in types:
            try:
                item = self.lookup_best(drama_name, drama_type=dt)
            except Exception as exc:  # 网络失败/非200/解析失败：记为 error 重试，不回退
                return (None, 'error', None, f'共享资料库查询失败：{exc}')
            if item and item.get('drama_id'):
                return (item['drama_id'], 'autolookup',
                        item.get('drama_name') or drama_name, None)
        return (None, 'none', None, '未找到匹配剧集')
