# -*- coding: utf-8 -*-
"""artifact_cleanup 单元测试 - 临时目录验证：
部分版本成功、同剧多账号、路径逃逸、封面失败、服务重启恢复。
"""
import os
import json
import shutil
import tempfile
import unittest
import sqlite3
from unittest.mock import patch

from artifact_cleanup import ArtifactCleanup, STRATEGY_NONE, STRATEGY_RECYCLE


class CleanupTestCase(unittest.TestCase):
    def setUp(self):
        # 独立临时根，不碰真实路径
        self.tmp = tempfile.mkdtemp(prefix='cleanup_test_')
        self.material_root = os.path.join(self.tmp, 'material')
        self.output_root = os.path.join(self.tmp, 'output')
        self.recycle_root = os.path.join(self.tmp, 'recycle')
        self.data_dir = os.path.join(self.tmp, 'data')
        for d in (self.material_root, self.output_root, self.recycle_root, self.data_dir):
            os.makedirs(d, exist_ok=True)
        self.cleanup = ArtifactCleanup(
            data_dir=self.data_dir,
            material_root=self.material_root,
            output_root=self.output_root,
            recycle_root=self.recycle_root,
        )
        # 用 UploadManager 建 schema？直接在 cleanup.db 建表更可控——复制最小 schema
        self._init_db()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _init_db(self):
        conn = sqlite3.connect(self.cleanup.db_path)
        conn.execute('''CREATE TABLE IF NOT EXISTS upload_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            drama_name TEXT NOT NULL, version INTEGER NOT NULL,
            video_path TEXT NOT NULL, cover_path TEXT,
            title TEXT, description TEXT, topics TEXT, account TEXT NOT NULL,
            status TEXT DEFAULT 'pending', error_message TEXT,
            retry_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP, completed_at TIMESTAMP,
            batch_id TEXT, source_dir TEXT, output_dir TEXT,
            cover_status TEXT DEFAULT 'not_requested',
            draft_saved INTEGER DEFAULT 0, draft_verified_at TIMESTAMP,
            cleanup_status TEXT DEFAULT 'none', cleanup_error TEXT
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS upload_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL UNIQUE, account TEXT, drama_name TEXT NOT NULL,
            source_dir TEXT, output_dir TEXT, version_count INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0, failed_count INTEGER DEFAULT 0,
            cover_applied_count INTEGER DEFAULT 0, cover_failed_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'running',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, completed_at TIMESTAMP
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS artifact_cleanup_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL, account TEXT, drama_name TEXT NOT NULL,
            job_type TEXT NOT NULL, version INTEGER,
            target_path TEXT NOT NULL, recycle_path TEXT, manifest_path TEXT,
            status TEXT DEFAULT 'pending', strategy TEXT DEFAULT 'recycle_on_full_batch',
            error TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS upload_batch_expected (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL, account TEXT, drama_name TEXT NOT NULL,
            source_dir TEXT, output_dir TEXT, expected_versions TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (batch_id, account, drama_name, source_dir, output_dir)
        )''')
        conn.commit()
        conn.close()

    def _make_drama(self, batch_id, drama='测试剧', account='福桃说剧',
                    versions=(1, 2), cover_status=('applied', 'applied'),
                    draft=(1, 1), record_status=('completed', 'completed'),
                    vids=None):
        """在临时目录构造一个剧的产物，并插入 batch 记录。"""
        out_dir = os.path.join(self.output_root, account, drama)
        src_dir = os.path.join(self.material_root, drama)
        os.makedirs(os.path.join(out_dir, 'covers'), exist_ok=True)
        os.makedirs(src_dir, exist_ok=True)
        rows = []
        for i, v in enumerate(versions):
            vp = os.path.join(out_dir, f'{drama}_{v}_逆袭_去重版.mp4')
            cp = os.path.join(out_dir, 'covers', f'{drama}_{v}_封面.jpg')
            open(vp, 'w').write(f'video{v}')
            open(cp, 'w').write(f'cover{v}')
            # 每个版本切片都会写 edl_config_vN.json（目录级“全量”基准）
            with open(os.path.join(out_dir, f'edl_config_v{v}.json'), 'w',
                      encoding='utf-8') as f:
                json.dump({'drama_name': drama, 'version': v}, f, ensure_ascii=False)
            rows.append((drama, v, vp, cp, account, record_status[i],
                         cover_status[i], draft[i], batch_id, src_dir, out_dir))
        conn = sqlite3.connect(self.cleanup.db_path)
        conn.executemany(
            '''INSERT INTO upload_records
               (drama_name, version, video_path, cover_path, account, status,
                cover_status, draft_saved, batch_id, source_dir, output_dir)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)''', rows)
        conn.commit()
        conn.close()
        return {'out_dir': out_dir, 'src_dir': src_dir}

    def test_strategy_none_no_jobs(self):
        d = self._make_drama('batch_none')
        plan = self.cleanup.plan_cleanup('batch_none', strategy=STRATEGY_NONE)
        self.assertEqual(plan['status'], 'failed')
        self.assertEqual(plan['jobs'], [])

    def test_version_jobs_created_dry_run(self):
        d = self._make_drama('batch_v1')
        plan = self.cleanup.plan_cleanup('batch_v1', strategy=STRATEGY_RECYCLE, dry_run=True)
        self.assertEqual(plan['status'], 'success')
        # 2 版本 × (video + cover) = 4 个版本级任务
        version_jobs = [j for j in plan['jobs'] if j['job_type'].startswith('version')]
        self.assertEqual(len(version_jobs), 4, version_jobs)
        # 全部完成 → 剧集级任务也生成
        drama_jobs = [j for j in plan['jobs'] if j['job_type'].startswith('drama')]
        self.assertEqual(len(drama_jobs), 2, drama_jobs)

    def test_partial_failure_no_drama_job(self):
        # V2 上传失败 → 只有 V1 的版本级任务，无剧集级
        d = self._make_drama('batch_part', versions=(1, 2),
                             record_status=('completed', 'failed'))
        plan = self.cleanup.plan_cleanup('batch_part', strategy=STRATEGY_RECYCLE, dry_run=True)
        version_jobs = [j for j in plan['jobs'] if j['job_type'].startswith('version')]
        self.assertEqual(len(version_jobs), 2, version_jobs)  # 仅 V1 的 video+cover
        drama_jobs = [j for j in plan['jobs'] if j['job_type'].startswith('drama')]
        self.assertEqual(drama_jobs, [])

    def test_cover_failed_no_cleanup_for_that_version(self):
        # V2 封面失败 → V2 不清理（needs_attention），V1 正常
        d = self._make_drama('batch_cf', versions=(1, 2),
                             cover_status=('applied', 'failed'))
        plan = self.cleanup.plan_cleanup('batch_cf', strategy=STRATEGY_RECYCLE, dry_run=True)
        v1_jobs = [j for j in plan['jobs'] if j['version'] == 1]
        v2_jobs = [j for j in plan['jobs'] if j['version'] == 2]
        self.assertEqual(len(v1_jobs), 2, v1_jobs)  # V1 video+cover
        self.assertEqual(v2_jobs, [], v2_jobs)
        # 封面失败 → 不生成剧集级
        drama_jobs = [j for j in plan['jobs'] if j['job_type'].startswith('drama')]
        self.assertEqual(drama_jobs, [])

    def test_path_safety_output_dir_escape(self):
        d = self._make_drama('batch_esc')
        # 成品目录直接指向 output 根 → 拒绝
        ok, err = self.cleanup._validate_output_dir(self.output_root)
        self.assertFalse(ok)
        # 成品目录指向根外的路径 → 拒绝
        outside = os.path.join(self.tmp, 'elsewhere', 'drama')
        ok, err = self.cleanup._validate_output_dir(outside)
        self.assertFalse(ok)
        # 深度 1（账号目录）→ 拒绝
        ok, err = self.cleanup._validate_output_dir(os.path.join(self.output_root, '福桃说剧'))
        self.assertFalse(ok)

    def test_path_safety_material_not_direct_child(self):
        d = self._make_drama('batch_mat')
        # 素材目录=素材根 → 拒绝
        ok, err = self.cleanup._validate_material_dir(self.material_root)
        self.assertFalse(ok)
        # 素材目录不在素材根下 → 拒绝
        ok, err = self.cleanup._validate_material_dir(os.path.join(self.tmp, 'x', '剧'))
        self.assertFalse(ok)
        # 深度 2（根/分类/剧名 布局）→ 允许（部分剧目素材目录嵌套两级）
        deep = os.path.join(self.material_root, '剧', 'sub')
        os.makedirs(deep, exist_ok=True)
        ok, err = self.cleanup._validate_material_dir(deep)
        self.assertTrue(ok, err)
        # 深度 3 → 拒绝（过深，不是剧目目录）
        deep3 = os.path.join(self.material_root, '剧', 'sub', 'x')
        os.makedirs(deep3, exist_ok=True)
        ok, err = self.cleanup._validate_material_dir(deep3)
        self.assertFalse(ok)

    def test_path_safety_material_multiple_roots(self):
        """多个合法素材根：任一根下的一级/二级子目录都合法。"""
        root2 = os.path.join(self.tmp, 'material_legacy')
        os.makedirs(root2, exist_ok=True)
        cleanup = ArtifactCleanup(
            data_dir=self.data_dir,
            material_root=[self.material_root, root2],
            output_root=self.output_root, recycle_root=self.recycle_root)
        # 第二个根下的一级子目录 → 合法
        ok, err = cleanup._validate_material_dir(os.path.join(root2, '旧剧'))
        self.assertTrue(ok, err)
        # 第二个根下的二级子目录 → 合法
        ok, err = cleanup._validate_material_dir(os.path.join(root2, '分类', '旧剧'))
        self.assertTrue(ok, err)
        # 不在任何根下 → 拒绝
        ok, err = cleanup._validate_material_dir(os.path.join(self.tmp, 'elsewhere', '剧'))
        self.assertFalse(ok)

    def test_execute_moves_to_recycle_with_manifest(self):
        d = self._make_drama('batch_ex')
        plan = self.cleanup.execute_cleanup('batch_ex', strategy=STRATEGY_RECYCLE)
        self.assertEqual(plan['status'], 'success')
        moved = [r for r in plan['results'] if r['status'] == 'moved']
        self.assertGreaterEqual(len(moved), 1)
        # 版本级 video 已移出 output
        v1_video = os.path.join(d['out_dir'], '测试剧_1_逆袭_去重版.mp4')
        self.assertFalse(os.path.exists(v1_video), '视频应被移走')
        # manifest 已写
        for r in moved:
            self.assertTrue(os.path.exists(r['manifest']), f'manifest 缺失: {r["manifest"]}')
            with open(r['manifest'], encoding='utf-8') as f:
                m = json.load(f)
            self.assertEqual(m['target_path'], r['target'])
            self.assertEqual(m['recycle_path'], r['recycle_path'])
        # DB 状态已更新为 moved
        conn = sqlite3.connect(self.cleanup.db_path)
        n_moved = conn.execute(
            "SELECT COUNT(*) FROM artifact_cleanup_jobs WHERE status='moved'").fetchone()[0]
        conn.close()
        self.assertEqual(n_moved, len(moved))

    def test_source_referenced_by_other_batch_skipped(self):
        # 同素材被另一批次引用 → 剧集级素材清理被跳过
        d = self._make_drama('batch_a')
        self._make_drama('batch_b', versions=(1,))  # 另一批次也引用同一素材目录
        # 把 batch_b 的记录 source_dir 指向同一素材
        conn = sqlite3.connect(self.cleanup.db_path)
        conn.execute("UPDATE upload_records SET source_dir=?, batch_id='batch_b' "
                     "WHERE batch_id='batch_b'", (d['src_dir'],))
        conn.commit()
        conn.close()
        plan = self.cleanup.plan_cleanup('batch_a', strategy=STRATEGY_RECYCLE, dry_run=True)
        source_jobs = [j for j in plan['jobs'] if j['job_type'] == 'drama_source']
        self.assertEqual(source_jobs, [], '素材被其他批次引用应跳过')

    def test_restart_recovery_jobs_persisted(self):
        # 非 dry-run 计划 → 任务持久化，重启后仍可见
        d = self._make_drama('batch_rs')
        plan = self.cleanup.plan_cleanup('batch_rs', strategy=STRATEGY_RECYCLE, dry_run=False)
        jobs = self.cleanup.list_jobs('batch_rs')
        self.assertGreaterEqual(len(jobs), len(plan['jobs']))
        # 重建服务实例（模拟重启）
        cleanup2 = ArtifactCleanup(
            data_dir=self.data_dir, material_root=self.material_root,
            output_root=self.output_root, recycle_root=self.recycle_root)
        jobs2 = cleanup2.list_jobs('batch_rs')
        self.assertEqual(len(jobs2), len(jobs))


    def test_partial_version_submission_no_drama_cleanup(self):
        """#6：只提交部分版本（缺 V2）→ 禁止目录级清理，版本级 V1 仍可清理。"""
        # 构造全量版本 {1,2}（两个 edl_config），但只插入 V1 记录
        out_dir = os.path.join(self.output_root, '福桃说剧', '部分剧')
        src_dir = os.path.join(self.material_root, '部分剧')
        os.makedirs(out_dir, exist_ok=True)
        os.makedirs(src_dir, exist_ok=True)
        for v in (1, 2):
            with open(os.path.join(out_dir, f'edl_config_v{v}.json'), 'w',
                      encoding='utf-8') as f:
                json.dump({'drama_name': '部分剧', 'version': v}, f, ensure_ascii=False)
        vp = os.path.join(out_dir, '部分剧_1_逆袭_去重版.mp4')
        open(vp, 'w').write('video1')
        conn = sqlite3.connect(self.cleanup.db_path)
        conn.execute(
            '''INSERT INTO upload_records
               (drama_name, version, video_path, cover_path, account, status,
                cover_status, draft_saved, batch_id, source_dir, output_dir)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
            ('部分剧', 1, vp, None, '福桃说剧', 'completed', 'not_requested', 1,
             'batch_partial', src_dir, out_dir))
        conn.commit()
        conn.close()
        plan = self.cleanup.plan_cleanup('batch_partial', strategy=STRATEGY_RECYCLE,
                                         dry_run=True)
        dir_jobs = [j for j in plan['jobs']
                    if j['job_type'] in ('drama_output', 'drama_source')]
        self.assertEqual(dir_jobs, [], '缺 V2 时禁止目录级清理')
        # 版本级 V1 仍可清理
        version_jobs = [j for j in plan['jobs'] if j['job_type'] == 'version']
        self.assertEqual(len(version_jobs), 1)
        # skipped 里有 expected_versions_incomplete 说明
        reasons = [s.get('reason') for s in plan['skipped']]
        self.assertIn('expected_versions_incomplete', reasons)

    def test_execute_cleanup_reports_failed_when_all_moves_fail(self):
        """#3：所有移动都失败 → execute_cleanup 返回 failed，绝不报 success。"""
        d = self._make_drama('batch_movefail')
        with patch('artifact_cleanup.shutil.move', side_effect=OSError('模拟移动失败')):
            result = self.cleanup.execute_cleanup('batch_movefail', strategy=STRATEGY_RECYCLE)
        self.assertEqual(result['status'], 'failed')
        self.assertGreater(result['failed_count'], 0)
        self.assertEqual(result['moved_count'], 0)
        # 文件仍在原地（未被删除）
        self.assertTrue(os.path.exists(os.path.join(d['out_dir'], '测试剧_1_逆袭_去重版.mp4')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
