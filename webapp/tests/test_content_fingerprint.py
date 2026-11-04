# -*- coding: utf-8 -*-
"""R9 内容指纹：帧哈希稳定性/区分度、相似度滑动匹配、内部重复、指纹存储。"""

import os
import shutil
import sqlite3
import tempfile
import unittest

import numpy as np

import content_fingerprint as cf


def _gray(val):
    """构造 (64,64) 灰度帧，填充固定值。"""
    return np.full((64, 64), val, dtype=np.uint8)


def _seq(n, seed=0):
    """构造 n 帧伪随机 56-bit 哈希序列。"""
    import random
    rng = random.Random(seed)
    return [rng.getrandbits(cf.DHASH_BITS) for _ in range(n)]


class FrameDhashTests(unittest.TestCase):
    def test_stable_same_frame(self):
        self.assertEqual(cf.frame_dhash(_gray(100)),
                         cf.frame_dhash(_gray(100)))

    def _gradient(self):
        """渐变帧：每列值递增，确保 dHash 非 0。"""
        row = np.arange(64, dtype=np.uint8)
        return np.tile(row, (64, 1))

    def test_distinguishes_different_frames(self):
        a = cf.frame_dhash(self._gradient())
        b = cf.frame_dhash(self._gradient()[:, ::-1])  # 水平翻转
        self.assertNotEqual(a, b)

    def test_gradient_has_bits(self):
        v = cf.frame_dhash(self._gradient())
        self.assertGreater(v.bit_count(), 0)

    def test_bit_width(self):
        v = cf.frame_dhash(_gray(100))
        self.assertLessEqual(v.bit_length(), cf.DHASH_BITS)


class SimilarityTests(unittest.TestCase):
    def test_identical_is_one(self):
        s = _seq(20, seed=1)
        self.assertAlmostEqual(cf.similarity(s, list(s)), 1.0)

    def test_unrelated_is_low(self):
        a = _seq(20, seed=1)
        b = _seq(20, seed=2)
        self.assertLess(cf.similarity(a, b), 0.3)

    def test_perturbed_stays_high(self):
        """微扰（每帧翻 ≤25% bit）仍高相似——抗转码/噪声/水印。"""
        a = _seq(20, seed=3)
        b = [v ^ 0x1F for v in a]  # 每帧翻 5 bit（约 9%）
        self.assertGreater(cf.similarity(a, b), 0.9)

    def test_subclip_detected(self):
        """b 含 a 作为子区间 → 高相似（滑动窗口命中）。"""
        base = _seq(60, seed=4)
        clip = base[10:40]  # 30s 子区间
        self.assertGreater(cf.similarity(clip, base), 0.9)

    def test_empty_returns_zero(self):
        self.assertEqual(cf.similarity([], _seq(5)), 0.0)
        self.assertEqual(cf.similarity(_seq(5), []), 0.0)


class InternalDuplicateTests(unittest.TestCase):
    def test_detects_repeated_block(self):
        """同一 10s 块在第 40s 重现 → 检出内部重复。"""
        base = _seq(50, seed=5)
        block = base[5:15]
        seq = base[:20] + [0] * 15 + block + base[40:]  # block 在 ~35s 重现
        # 简化：直接构造两段相同块
        seq = list(block) + [v ^ 0xFFFFFFFF for v in _seq(20, seed=9)] \
              + list(block)
        dups = cf.internal_duplicate(seq, window=10, min_gap=5,
                                     match_ratio=0.95)
        self.assertTrue(dups, f'期望检出重复块，得到 {dups}')

    def test_no_duplicate_clean(self):
        seq = _seq(40, seed=6)
        dups = cf.internal_duplicate(seq, window=10, min_gap=5)
        self.assertEqual(dups, [])


class FingerprintStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='fp_')
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.db = os.path.join(self.tmp, 'upload.db')
        self.store = cf.FingerprintStore(self.db)

    def test_upsert_and_get(self):
        seq = _seq(12, seed=7)
        self.store.upsert('剧A', r'C:\out\剧A_1_逆袭_去重版.mp4', seq,
                          version=1, account='福桃说剧',
                          filename='剧A_1_逆袭_去重版.mp4',
                          duration=250.0, size_bytes=1024)
        row = self.store.get_by_path(r'C:\out\剧A_1_逆袭_去重版.mp4')
        self.assertIsNotNone(row)
        self.assertEqual(row['hash_seq'], seq)
        self.assertEqual(row['version'], 1)
        self.assertEqual(row['uploaded'], 0)

    def test_upsert_overwrites_same_path(self):
        seq1 = _seq(5, seed=1)
        seq2 = _seq(5, seed=2)
        p = r'C:\out\x_1_打脸_去重版.mp4'
        self.store.upsert('剧B', p, seq1, version=1)
        self.store.upsert('剧B', p, seq2, version=1)
        row = self.store.get_by_path(p)
        self.assertEqual(row['hash_seq'], seq2)  # 重切覆盖

    def test_find_similar_filters_uploaded(self):
        p_a = r'C:\out\A_1_x_去重版.mp4'
        seq_a = _seq(10, seed=8)
        self.store.upsert('剧A', p_a, seq_a, version=1, account='福桃说剧')
        self.store.mark_uploaded(p_a)
        # 同序列 → 高相似，uploaded=1 命中
        hits = self.store.find_similar('福桃说剧', list(seq_a), 0.6)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['drama_name'], '剧A')
        self.assertTrue(hits[0]['uploaded'])
        # 未上传（include_pending=False）→ 不命中
        p_b = r'C:\out\B_1_x_去重版.mp4'
        self.store.upsert('剧B', p_b, list(seq_a), version=1,
                          account='福桃说剧')
        hits2 = self.store.find_similar('福桃说剧', list(seq_a), 0.6)
        self.assertEqual(len(hits2), 1)
        # include_pending=True → 命中 B（已入队未传）
        hits3 = self.store.find_similar('福桃说剧', list(seq_a), 0.6,
                                        include_pending=True)
        self.assertEqual(len(hits3), 2)

    def test_find_similar_exclude_path(self):
        p_a = r'C:\out\A_1_x_去重版.mp4'
        seq_a = _seq(10, seed=8)
        self.store.upsert('剧A', p_a, seq_a, version=1, account='福桃说剧')
        self.store.mark_uploaded(p_a)
        hits = self.store.find_similar('福桃说剧', list(seq_a), 0.6,
                                       exclude_path=p_a)
        self.assertEqual(hits, [])

    def test_find_similar_none_account_searches_all_accounts(self):
        p_a = r'C:\out\A_1_x_去重版.mp4'
        seq_a = _seq(10, seed=8)
        self.store.upsert('剧A', p_a, seq_a, version=1, account='福桃说剧')
        self.store.mark_uploaded(p_a)
        hits = self.store.find_similar(None, list(seq_a), 0.6)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['account'], '福桃说剧')

    def test_blob_roundtrip(self):
        seq = _seq(300, seed=11)
        blob = cf.FingerprintStore._seq_to_blob(seq)
        self.assertEqual(len(blob), 300 * 8)
        self.assertEqual(cf.FingerprintStore._blob_to_seq(blob), seq)


if __name__ == '__main__':
    unittest.main()
