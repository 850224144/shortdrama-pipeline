# -*- coding: utf-8 -*-
"""EDL 审批闭环的纯逻辑（P2-9 / P1-5 拆分第二批）。

processor 侧只保留“读日志、抛异常”的接线；hash 计算、批准状态读写
都在这里，方便单测与未来审批 UI 复用。
"""
import hashlib
import json
import os
from datetime import datetime


def edl_hash_of(edl_config):
    """只对 segments 取指纹：文案/标题改动不该让渲染批准失效。"""
    try:
        segments = ((edl_config.get('videos') or [{}])[0] or {}).get('segments') or []
    except AttributeError:
        segments = []
    payload = json.dumps(segments, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.md5(payload.encode('utf-8')).hexdigest()


def _write(path, cfg):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_edl(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def set_approval(path, approved=True, by='manual', cfg=None):
    """写入批准状态并返回新的 approval 结构。"""
    cfg = cfg if cfg is not None else read_edl(path)
    cfg['approval'] = {
        'state': 'approved' if approved else 'pending',
        'edl_hash': edl_hash_of(cfg),
        'approved_at': (datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        if approved else None),
        'approved_by': by if approved else None,
    }
    _write(path, cfg)
    return cfg['approval']


def check_approval(path, auto_approve=True):
    """返回 (ok, reason, reapproved)。

    批准 hash 与当前 segments 一致 → 放行；不一致说明 EDL 被改，旧批准失效。
    auto_approve=True 时自动重新批准（保持现有全自动跑批），否则拒绝渲染。
    """
    try:
        cfg = read_edl(path)
    except Exception as e:
        return False, f'EDL 读取失败：{e}', False
    current = edl_hash_of(cfg)
    ap = cfg.get('approval') or {}
    if ap.get('state') == 'approved' and ap.get('edl_hash') == current:
        return True, '', False
    if not auto_approve:
        return False, 'EDL 未批准或内容已变更，需人工批准后再渲染', False
    try:
        set_approval(path, approved=True, by='auto-reapprove', cfg=cfg)
    except Exception as e:
        return False, f'自动批准写入失败：{e}', False
    return True, '', True
