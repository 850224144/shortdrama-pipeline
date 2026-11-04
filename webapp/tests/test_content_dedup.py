# -*- coding: utf-8 -*-
"""R9 内容去重：生成侧版本剔除 + 上传侧入队查重。

验收（设计 2.3）：
- 生成侧：版本间相似度 > hard 被剔除；soft~hard 保留但告警；保底 min_deliver。
- 上传侧：同账号已上传/已入队内容相似 → 拒收（reason=content_duplicate）；
  指纹失败保守放行。
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

sys_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
sys.path.insert(0, sys_path)

import content_fingerprint as cf
from processor import PipelineProcessor, CONFIG
from upload_manager import UploadManager


def _video(name, content=b'x' * 100):
    p = os.path.join(tempfile.gettempdir(), f'cf_{os.getpid()}_{name}')
    with open(p, 'wb') as f:
        f.write(content)
    return p


class _FakeLogger:
    def log(self, *a, **k):
        pass


class GenerationDedupTests(unittest.TestCase):
    """生成侧 _dedup_versions_by_content（mock 指纹/相似度）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='gen_dedup_')
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.proc = PipelineProcessor(mock.MagicMock(), mock.MagicMock())
        self.proc.logger = _FakeLogger()
        self.proc.assign_account = lambda name: '福桃说剧'
        self.proc._get_video_duration = lambda p: 250.0
        self._orig_data_dir = CONFIG.get('data_dir')
        CONFIG['data_dir'] = self.tmp

    def tearDown(self):
        CONFIG['data_dir'] = self._orig_data_dir

    def _videos(self, n):
        return [{
            'filename': f'剧X_{i}_逆袭_去重版.mp4',
            'path': _video(f'剧X_{i}_逆袭_去重版.mp4'),
            'final_path': _video(f'剧X_{i}_逆袭_去重版.mp4'),
            'duration': 250.0,
        } for i in range(1, n + 1)]

    def _mock_fp(self, sim_matrix):
        """mock video_fingerprint 按调用顺序返回 [idx]，similarity 查矩阵。

        sim_matrix: {(i,j): sim}，i<j（版本序号 = video_fingerprint 调用序）。"""
        import itertools
        counter = itertools.count(1)

        def _sim(a, b):
            ia, ib = a[0], b[0]
            i, j = min(ia, ib), max(ia, ib)
            return sim_matrix.get((i, j), 0.0)

        fp = mock.patch('content_fingerprint.video_fingerprint',
                        side_effect=lambda p, **k: [next(counter)])
        sim = mock.patch('content_fingerprint.similarity', side_effect=_sim)
        return fp, sim

    def test_hard_duplicates_removed(self):
        """V1~V8，其中 V5-V8 与 V1 高度重复（0.9）→ 剔除至保底 3。"""
        videos = self._videos(8)
        sim_matrix = {}
        for j in range(2, 9):
            sim_matrix[(1, j)] = 0.9  # 全与 V1 高重复
        fp, sim = self._mock_fp(sim_matrix)
        with fp, sim:
            kept, report = self.proc._dedup_versions_by_content(
                videos, '剧X', self.tmp)
        # 保底 3：V2-V8 全高重复，但保底 3 → 只剔到剩 3
        self.assertEqual(len(kept), 3)
        self.assertEqual(len(report['removed']), 5)
        self.assertTrue(all(r['reason'] == 'content_duplicate'
                            for r in report['removed']))

    def test_soft_similarity_warns_not_removes(self):
        """soft~hard 区间：保留但告警。"""
        videos = self._videos(4)
        sim_matrix = {(1, 2): 0.6}  # soft=0.5, hard=0.75 → 疑似
        fp, sim = self._mock_fp(sim_matrix)
        with fp, sim:
            kept, report = self.proc._dedup_versions_by_content(
                videos, '剧X', self.tmp)
        self.assertEqual(len(kept), 4)
        self.assertEqual(report['removed'], [])
        self.assertTrue(any('疑似重复' in w for w in report['warnings']))

    def test_no_duplicates_keeps_all(self):
        videos = self._videos(4)
        fp, sim = self._mock_fp({})
        with fp, sim:
            kept, report = self.proc._dedup_versions_by_content(
                videos, '剧X', self.tmp)
        self.assertEqual(len(kept), 4)
        self.assertEqual(report['removed'], [])
        self.assertEqual(report['warnings'], [])

    def test_fingerprint_failure_passthrough(self):
        """指纹计算失败 → 保守放行不剔除。"""
        videos = self._videos(4)
        with mock.patch('content_fingerprint.video_fingerprint',
                        return_value=None):
            kept, report = self.proc._dedup_versions_by_content(
                videos, '剧X', self.tmp)
        self.assertEqual(len(kept), 4)
        self.assertEqual(report['removed'], [])


class UploadDedupTests(unittest.TestCase):
    """上传侧 _check_content_duplicate / add_batch 拒收。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='up_dedup_')
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.mgr = UploadManager(self.tmp)
        self._orig_data_dir = CONFIG.get('data_dir')
        CONFIG['data_dir'] = self.tmp

    def tearDown(self):
        CONFIG['data_dir'] = self._orig_data_dir

    def test_add_batch_rejects_content_duplicate(self):
        """已上传内容相似 → 拒收 reason=content_duplicate。"""
        # 预置一条已上传指纹（seq=[7]）
        from content_fingerprint import FingerprintStore
        store = FingerprintStore(
            os.path.join(CONFIG['data_dir'], 'upload.db'))
        existing = _video('已传_1_逆袭_去重版.mp4')
        store.upsert('剧A', existing, [7], version=1, account='福桃说剧',
                     filename='已传_1_逆袭_去重版.mp4')
        store.mark_uploaded(existing)
        # 新任务：fingerprint 返回 [7]（与已传相同内容）
        new = _video('剧B_1_打脸_去重版.mp4')
        with mock.patch('content_fingerprint.video_fingerprint',
                        return_value=[7]):
            res = self.mgr.add_batch([{
                'drama_name': '剧B', 'video_path': new,
                'account': '福桃说剧', 'version': 1,
            }])
        self.assertEqual(res['status'], 'success')
        self.assertEqual(res['accepted'], 0)
        self.assertEqual(len(res['duplicates']), 1)
        d = res['duplicates'][0]
        self.assertEqual(d['reason'], 'content_duplicate')
        self.assertEqual(d['conflict'], '[福桃说剧] 剧A V1')

    def test_add_batch_fingerprint_failure_passthrough(self):
        """指纹失败 → 保守放行。"""
        new = _video('剧B_2_真相_去重版.mp4')
        with mock.patch('content_fingerprint.video_fingerprint',
                        return_value=None):
            res = self.mgr.add_batch([{
                'drama_name': '剧B', 'video_path': new,
                'account': '福桃说剧', 'version': 2,
            }])
        self.assertEqual(res['accepted'], 1)
        self.assertEqual(res['duplicates'], [])

    def test_add_batch_distinct_content_accepted(self):
        """内容不同 → 通过（mock similarity 低值）。"""
        from content_fingerprint import FingerprintStore
        store = FingerprintStore(
            os.path.join(CONFIG['data_dir'], 'upload.db'))
        existing = _video('已传_1_逆袭_去重版.mp4')
        store.upsert('剧A', existing, [7], version=1, account='福桃说剧')
        store.mark_uploaded(existing)
        new = _video('剧C_1_泪目_去重版.mp4')
        with mock.patch('content_fingerprint.video_fingerprint',
                        return_value=[99]), \
             mock.patch('content_fingerprint.similarity',
                        return_value=0.1):  # 内容不同
            res = self.mgr.add_batch([{
                'drama_name': '剧C', 'video_path': new,
                'account': '福桃说剧', 'version': 1,
            }])
        self.assertEqual(res['accepted'], 1)
        self.assertEqual(res['duplicates'], [])

    def test_add_batch_rejects_cross_account_duplicate(self):
        """A 账号已上传的内容换 B 账号发布也必须拒绝。"""
        from content_fingerprint import FingerprintStore
        store = FingerprintStore(
            os.path.join(CONFIG['data_dir'], 'upload.db'))
        existing = _video('跨号旧片_1_真相_去重版.mp4')
        store.upsert('剧A', existing, [11, 12, 13], version=1,
                     account='福桃说剧')
        store.mark_uploaded(existing)
        new = _video('跨号新片_1_真相_去重版.mp4')
        with mock.patch('content_fingerprint.video_fingerprint',
                        return_value=[11, 12, 13]):
            res = self.mgr.add_batch([{
                'drama_name': '剧B', 'video_path': new,
                'account': '小桃追剧中', 'version': 1,
            }])
        self.assertEqual(res['accepted'], 0)
        self.assertEqual(len(res['duplicates']), 1)
        self.assertEqual(res['duplicates'][0]['reason'], 'content_duplicate')
        self.assertIn('福桃说剧', res['duplicates'][0]['conflict'])

    def test_mark_uploaded_registers_flag(self):
        from content_fingerprint import FingerprintStore
        store = FingerprintStore(
            os.path.join(CONFIG['data_dir'], 'upload.db'))
        p = _video('剧D_1_反转_去重版.mp4')
        store.upsert('剧D', p, [3], version=1, account='福桃说剧')
        self.mgr._mark_fingerprint_uploaded(p)
        row = store.get_by_path(p)
        self.assertEqual(row['uploaded'], 1)
        self.assertIsNotNone(row['uploaded_at'])


if __name__ == '__main__':
    unittest.main()
