# -*- coding: utf-8 -*-
"""UploadManager 真实 schema 与 ArtifactCleanup 集成测试（端到端，不含浏览器）。
模拟：批次登记 → 上传完成记录 → 草稿确认 → 清理执行 → 回收站 + DB 状态。
"""
import os
import json
import shutil
import tempfile
import unittest
import sqlite3

from upload_manager import UploadManager
from artifact_cleanup import ArtifactCleanup, STRATEGY_RECYCLE


class CleanupIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cleanup_int_')
        self.data_dir = os.path.join(self.tmp, 'data')
        self.material_root = os.path.join(self.tmp, 'material')
        self.output_root = os.path.join(self.tmp, 'output')
        self.recycle_root = os.path.join(self.tmp, 'recycle')
        for d in (self.data_dir, self.material_root, self.output_root, self.recycle_root):
            os.makedirs(d, exist_ok=True)
        # 用 UploadManager 初始化真实 schema
        self.um = UploadManager(data_dir=self.data_dir)
        self.cleanup = ArtifactCleanup(
            data_dir=self.data_dir,
            material_root=self.material_root,
            output_root=self.output_root,
            recycle_root=self.recycle_root,
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed_completed_upload(self, batch_id, drama='集成剧', account='福桃说剧',
                               version=1, cover_status='applied', draft_saved=1,
                               record_status='completed'):
        """构造一个已完成上传记录（模拟 worker 写回 DB 后的状态）。"""
        out_dir = os.path.join(self.output_root, account, drama)
        src_dir = os.path.join(self.material_root, drama)
        os.makedirs(os.path.join(out_dir, 'covers'), exist_ok=True)
        os.makedirs(src_dir, exist_ok=True)
        video = os.path.join(out_dir, f'{drama}_{version}_逆袭_去重版.mp4')
        cover = os.path.join(out_dir, 'covers', f'{drama}_{version}_封面.jpg')
        open(video, 'w').write(f'video{version}')
        open(cover, 'w').write(f'cover{version}')
        # 写 edl_config（artifacts），模拟切片产物
        cfg = {
            'drama_name': drama,
            'source_dir': src_dir,
            'output_dir': out_dir,
            'artifacts': {'video_path': video, 'cover_path': cover,
                          'source_dir': src_dir, 'output_dir': out_dir},
        }
        with open(os.path.join(out_dir, f'edl_config_v{version}.json'), 'w',
                  encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        # 直接插记录（绕过 add_to_queue，模拟已完成）
        conn = sqlite3.connect(self.um.db_path)
        conn.execute('''INSERT INTO upload_records
            (drama_name, version, video_path, cover_path, title, description, topics,
             account, status, batch_id, source_dir, output_dir, cover_status, draft_saved)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (drama, version, video, cover, '标题', '描述', '[]', account,
             record_status, batch_id, src_dir, out_dir, cover_status, draft_saved))
        conn.commit()
        conn.close()
        return {'out_dir': out_dir, 'src_dir': src_dir, 'video': video, 'cover': cover}

    def test_full_flow_batch_complete(self):
        """批次全部完成 + 草稿确认 → 版本级与剧集级都清理。"""
        batch = 'batch_full'
        d1 = self._seed_completed_upload(batch, version=1)
        d2 = self._seed_completed_upload(batch, version=2)
        plan = self.cleanup.plan_cleanup(batch, strategy=STRATEGY_RECYCLE, dry_run=True)
        self.assertEqual(plan['status'], 'success')
        self.assertGreaterEqual(len(plan['jobs']), 4, plan['jobs'])
        # 执行
        result = self.cleanup.execute_cleanup(batch, strategy=STRATEGY_RECYCLE)
        moved = [r for r in result['results'] if r['status'] == 'moved']
        self.assertGreaterEqual(len(moved), 4)
        # 视频已移走
        self.assertFalse(os.path.exists(d1['video']))
        self.assertFalse(os.path.exists(d2['video']))
        # DB 清理任务全部 moved
        conn = sqlite3.connect(self.cleanup.db_path)
        n_moved = conn.execute("SELECT COUNT(*) FROM artifact_cleanup_jobs WHERE status='moved'").fetchone()[0]
        n_total = conn.execute("SELECT COUNT(*) FROM artifact_cleanup_jobs WHERE batch_id=?", (batch,)).fetchone()[0]
        conn.close()
        self.assertEqual(n_moved, n_total)
        # 回收站里有内容
        self.assertTrue(os.listdir(self.recycle_root))

    def test_draft_not_saved_keeps_files(self):
        """草稿未确认 → 不清理该版本（文件保留）。"""
        batch = 'batch_nodraft'
        d = self._seed_completed_upload(batch, version=1, draft_saved=0)
        plan = self.cleanup.plan_cleanup(batch, strategy=STRATEGY_RECYCLE, dry_run=True)
        version_jobs = [j for j in plan['jobs'] if j['job_type'] == 'version']
        self.assertEqual(version_jobs, [], '草稿未确认不应生成版本清理任务')
        self.assertTrue(os.path.exists(d['video']))

    def test_needs_attention_cover_failed(self):
        """封面失败 → 任务 needs_attention，不清理该版本。"""
        batch = 'batch_cf'
        d = self._seed_completed_upload(batch, version=1, cover_status='failed')
        plan = self.cleanup.plan_cleanup(batch, strategy=STRATEGY_RECYCLE, dry_run=True)
        version_jobs = [j for j in plan['jobs'] if j['job_type'] == 'version']
        self.assertEqual(version_jobs, [], '封面失败版本不应清理')
        self.assertTrue(os.path.exists(d['video']))

    def test_restart_recovery(self):
        """清理任务持久化后，重建实例仍能查到（服务重启恢复）。"""
        batch = 'batch_rs'
        self._seed_completed_upload(batch, version=1)
        self._seed_completed_upload(batch, version=2)
        self.cleanup.plan_cleanup(batch, strategy=STRATEGY_RECYCLE, dry_run=False)
        # 模拟重启
        cleanup2 = ArtifactCleanup(
            data_dir=self.data_dir, material_root=self.material_root,
            output_root=self.output_root, recycle_root=self.recycle_root)
        jobs = cleanup2.list_jobs(batch)
        self.assertGreaterEqual(len(jobs), 4, jobs)


if __name__ == '__main__':
    unittest.main(verbosity=2)
