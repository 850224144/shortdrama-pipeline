# -*- coding: utf-8 -*-
"""上传精确查重单测（2026-11-10 人鱼赴归潮 V5 重复上传根因）。

根因：`_check_content_duplicate` 只做指纹相似查重，没有按
(drama_name, version, account) 直接查 upload_records 已完成记录；
且指纹表无该视频行 + uploaded 标志因路径分隔符不一致长期为 0，
指纹兜底全失效 → 同文件重传（id339 completed → id345 再传）漏拦。
修复：add_to_queue/add_batch 的重复检查把 status IN 扩展到 'completed'。
"""
import unittest
import tempfile
import sqlite3
from unittest import mock

from upload_manager import UploadManager


def _make_manager():
    tmp = tempfile.mkdtemp(prefix='sd_dupblock_')
    return UploadManager(data_dir=tmp), tmp


def _insert(mgr, drama, version, account, status):
    db = mgr.db_path
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO upload_records (drama_name, version, account, status,"
            " video_path, created_at, completed_at) VALUES (?,?,?,?,'x.mp4',"
            " datetime('now'), CASE WHEN ?='completed' THEN datetime('now') ELSE NULL END)",
            (drama, version, account, status, status))
        conn.commit()
    finally:
        conn.close()


class TestDuplicateUploadBlock(unittest.TestCase):
    def _call_add(self, mgr, drama, version, account):
        with mock.patch.object(mgr, '_read_edl_title', return_value=''), \
                mock.patch.object(mgr, '_compute_publish_time',
                                  return_value='2026-11-11 15:00'):
            return mgr.add_to_queue(
                drama, f'D:/out/{drama}_{version}_x_去重版.mp4',
                account=account, version=version)

    def test_add_to_queue_rejects_completed_same_drama_version_account(self):
        # 本次 bug 场景：同剧同版本同账号已 completed → 拒绝
        mgr, _tmp = _make_manager()
        _insert(mgr, '人鱼赴归潮', 5, '小桃追剧中', 'completed')
        r = self._call_add(mgr, '人鱼赴归潮', 5, '小桃追剧中')
        self.assertEqual(r['status'], 'failed')
        self.assertIn('已上传完成', r['error'])

    def test_add_to_queue_rejects_pending(self):
        # pending 仍在队列 → 拒绝（原有行为）
        mgr, _tmp = _make_manager()
        _insert(mgr, '剧A', 1, '福桃说剧', 'pending')
        r = self._call_add(mgr, '剧A', 1, '福桃说剧')
        self.assertEqual(r['status'], 'failed')
        self.assertIn('已在队列', r['error'])

    def test_add_to_queue_allows_failed_retry(self):
        # failed 允许重传（失败放行语义），不误拦
        mgr, _tmp = _make_manager()
        _insert(mgr, '剧X', 2, '小桃追剧中', 'failed')
        with mock.patch.object(mgr, '_check_content_duplicate', return_value=None), \
                mock.patch.object(mgr, '_bound_id', return_value=(None, 'none')):
            r = self._call_add(mgr, '剧X', 2, '小桃追剧中')
        self.assertNotIn('已上传完成', r.get('error') or '')
        self.assertNotIn('已在队列', r.get('error') or '')

    def test_mark_uploaded_path_separator_insensitive(self):
        # 指纹表路径用反斜杠、上传记录用正斜杠 → 归一化后仍能置 uploaded=1
        import content_fingerprint as cf
        store = cf.FingerprintStore(_make_manager()[0].db_path)
        p_db = r'C:\out\剧C_1_x_去重版.mp4'
        p_call = 'C:/out/剧C_1_x_去重版.mp4'
        seq = cf.FingerprintStore._seq_to_blob([1] * 8)
        store.upsert('剧C', p_db, [1] * 8, version=1, account='福桃说剧')
        store.mark_uploaded(p_call)
        row = store.get_by_path(p_db)
        self.assertEqual(row['uploaded'], 1)


if __name__ == '__main__':
    unittest.main()
