# -*- coding: utf-8 -*-
"""R4 原子批量上传：一次校验 + 一个事务创建整批，无半批次。

验收（设计文档 R4）：
- 一次请求创建整批；重复、缺失和失败有结构化明细；无半批次。
"""

import os
import shutil
import sqlite3
import tempfile
import unittest

from upload_manager import UploadManager


class AddBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='add_batch_')
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.mgr = UploadManager(self.tmp)
        # 造 3 个真实文件
        self.paths = []
        for i in (1, 2, 3):
            p = os.path.join(self.tmp, f'剧A_{i}_打脸_去重版.mp4')
            open(p, 'w').close()
            self.paths.append(p)

    def _item(self, version, path=None, drama='剧A', account='福桃说剧'):
        return {
            'drama_name': drama, 'video_path': path or self.paths[version - 1],
            'account': account, 'version': version,
            'batch_id': 'B_ATOMIC', 'output_dir': self.tmp,
            'expected_versions': [1, 2, 3],
        }

    def test_creates_entire_batch_in_one_request(self):
        items = [self._item(1), self._item(2), self._item(3)]
        res = self.mgr.add_batch(items)
        self.assertEqual(res['status'], 'success')
        self.assertEqual(res['accepted'], 3)
        self.assertEqual(len(res['ids']), 3)
        self.assertEqual(res['duplicates'], [])
        self.assertEqual(res['missing'], [])
        self.assertEqual(res['rejected'], [])
        self.assertEqual(len(self.mgr.queue), 3)
        # 落库校验
        with sqlite3.connect(self.mgr.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM upload_records WHERE batch_id='B_ATOMIC'"
            ).fetchone()[0]
        self.assertEqual(count, 3)

    def test_missing_file_reported_and_others_accepted(self):
        items = [self._item(1), self._item(2, path=os.path.join(self.tmp, '不存在.mp4'))]
        res = self.mgr.add_batch(items)
        self.assertEqual(res['accepted'], 1)
        self.assertEqual(len(res['missing']), 1)
        self.assertEqual(res['missing'][0]['version'], 2)
        self.assertEqual(len(self.mgr.queue), 1, '缺失条不得入队，其余正常')
        self.assertEqual(res['missing'][0]['video_path'],
                         os.path.join(self.tmp, '不存在.mp4'))

    def test_duplicate_reported_with_structured_detail(self):
        # 先入队 V1
        first = self.mgr.add_to_queue(
            drama_name='剧A', video_path=self.paths[0], account='福桃说剧',
            version=1, batch_id='B_OLD')
        self.assertEqual(first['status'], 'success')
        res = self.mgr.add_batch([self._item(1)])
        self.assertEqual(res['accepted'], 0)
        self.assertEqual(len(res['duplicates']), 1)
        self.assertEqual(res['duplicates'][0]['account'], '福桃说剧')
        self.assertEqual(res['duplicates'][0]['version'], 1)
        self.assertEqual(len(self.mgr.queue), 1, '重复条不得重复入队')

    def test_missing_required_fields_rejected(self):
        res = self.mgr.add_batch([
            {'drama_name': '剧B', 'video_path': '', 'version': 1},
            {'drama_name': '', 'video_path': self.paths[0], 'version': 1},
            {'drama_name': '剧B', 'video_path': self.paths[1], 'version': None},
        ])
        self.assertEqual(res['accepted'], 0)
        self.assertEqual(len(res['rejected']), 3)
        self.assertTrue(all('必填' in r['reason'] for r in res['rejected']))
        self.assertEqual(len(self.mgr.queue), 0)

    def test_empty_items_is_noop(self):
        res = self.mgr.add_batch([])
        self.assertEqual(res['status'], 'success')
        self.assertEqual(res['accepted'], 0)
        self.assertEqual(len(self.mgr.queue), 0)

    def test_no_half_batch_on_mixed_validation(self):
        """混合场景：1 有效 + 1 重复 + 1 缺失 → 只有有效的入队（无半批次残留）。"""
        self.mgr.add_to_queue(drama_name='剧A', video_path=self.paths[0],
                              account='福桃说剧', version=2)
        res = self.mgr.add_batch([
            self._item(1),
            self._item(2),                                   # 重复（已在队列）
            self._item(3, path=os.path.join(self.tmp, 'x.mp4')),  # 缺失
        ])
        self.assertEqual(res['accepted'], 1)
        self.assertEqual(len(res['duplicates']), 1)
        self.assertEqual(len(res['missing']), 1)
        # 队列 = 已有 V2 + 新入队 V1
        self.assertEqual(len(self.mgr.queue), 2)
        versions = {t['version'] for t in self.mgr.queue}
        self.assertEqual(versions, {1, 2})


if __name__ == '__main__':
    unittest.main()
