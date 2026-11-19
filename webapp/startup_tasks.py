# -*- coding: utf-8 -*-
"""服务启动后台钩子（阶段一 4.4 从 app.py 抽出）。

每个函数接收所需组件作为参数，由 app.py 在装配阶段起 daemon 线程调用；
不自行实例化 manager / service，避免循环 import。
"""
import os
import time
import threading

from artifact_cleanup import STRATEGY_RECYCLE


def start_cut_reconcile(batch_cut_manager, logger):
    """启动状态对账：恢复中断任务、修复错误 running、标记缺成片/可恢复现场。"""
    def _run():
        try:
            report = batch_cut_manager.reconcile()
            if report['changed']:
                logger.log('system', 'INFO', f'切片对账：{report}')
        except Exception as e:
            logger.log('system', 'ERROR', f'切片对账失败: {e}')

    threading.Thread(target=_run, daemon=True).start()


def start_download_resume(processor, process_controller, logger, CONFIG):
    """P3: 启动恢复崩溃遗留的下载任务（心跳过期且 status=downloading 的自动续传）。

    修复：旧实现固定取 orphan_jobs[0] 并在成功提交后 return，同时崩溃的
    多个下载只恢复第一条，其余永远卡在 downloading。现在把所有可续传链接
    合并为一条消息提交（单控制器一次只跑一个任务），一次覆盖全部孤儿。
    """
    def _run():
        try:
            orphan_jobs = processor.find_orphaned_download_jobs()
            if not orphan_jobs:
                return
            min_files = int(CONFIG['download'].get('min_manual_files', 2))
            pending_links = []
            seen = set()
            for job in orphan_jobs:
                params = job.get('params') or {}
                link = params.get('link')
                if not link or link in seen:
                    continue
                seen.add(link)
                # 启动恢复防重：素材已就绪（archive 判定不可下载）→ 不续传。
                # 崩溃遗留的孤儿若已被后续任务下载完成，每次重启都会命中
                # stale_jobs 并重复提交同一条链接，触发「跳过：状态文件显示
                # 已完成」噪音。此时把孤儿标记为已解决，不再重新提交。
                try:
                    from archive import should_download as _arch_should
                    _link_id = params.get('link_id')
                    _guess = _link_id or link.split('/')[-1].split('?')[0]
                    _local = os.path.join(CONFIG['download_base_dir'], _guess)
                    _ok, _reason = _arch_should(_guess, _local, source_url=link,
                                                min_files=min_files)
                    if not _ok:
                        processor.resolve_orphan_download_job(
                            job.get('id'),
                            f'启动恢复：素材已就绪（{_reason}），取消续传')
                        logger.log('system', 'INFO',
                                   f'启动恢复：素材已就绪，取消续传 {_guess}')
                        continue
                except Exception as _e:
                    logger.log('system', 'WARN',
                               f'启动恢复防重检查失败（继续续传）：{_e}')
                # 失败任务自动续传：记录次数，防永久失效源无限重试。
                if job.get('status') == 'failed':
                    processor.bump_download_auto_resume(job.get('id'))
                pwd = params.get('pwd') or CONFIG['download'].get('default_pwd', '9999')
                msg = link if 'pwd=' in link else f"{link}?pwd={pwd}"
                if msg not in pending_links:
                    pending_links.append(msg)
            if not pending_links:
                return
            logger.log('system', 'WARN',
                       f'启动恢复：{len(pending_links)}条中断下载待续传')
            # 单控制器一次只跑一个任务：全部链接合并成一条消息提交，
            # 避免只恢复第一条、其余孤儿永久卡死。
            message = '\n'.join(pending_links)
            for attempt in range(3):
                body, status = process_controller.start(message)
                # start 成功返回 202（Accepted），非 200；判 <300 才算接受，
                # 否则每次启动恢复都误报「被拒」并多余重试一次。
                if isinstance(status, int) and status < 300:
                    logger.log('system', 'INFO',
                               f'启动恢复：已提交{len(pending_links)}条续传任务')
                    return
                logger.log('system', 'WARN',
                           f'启动恢复：续传被拒（{status}），{20}s后重试'
                           f'（{attempt + 1}/3）')
                time.sleep(20)
            logger.log('system', 'ERROR',
                       '启动恢复：续传多次被拒，放弃本次自动续传（任务仍保留可手工重提）')
        except Exception as e:
            logger.log('system', 'ERROR', f'启动下载恢复失败: {e}')

    threading.Thread(target=_run, daemon=True).start()


def start_pending_batch_resume(processor, process_controller, logger, CONFIG):
    """恢复未完成的批次队列（崩溃时内存队列丢失的链接）。

    队列在批次开始前落盘（data/pending_batch.json），每完成一步更新状态；
    进程重启后把仍处于 pending/failed 的链接重新提交，避免「只处理了一半，
    其余链接再也不会出现」。

    与 start_download_resume 并行执行，需避开两条路径的重复提交，并等待
    控制器空闲（单控制器一次只跑一个任务）。
    """
    def _wait_idle(timeout=1800, interval=30):
        waited = 0
        while waited < timeout:
            try:
                if not process_controller.snapshot().get('processing'):
                    return True
            except Exception:
                return True
            time.sleep(interval)
            waited += interval
        return False

    def _run():
        try:
            from pending_queue import load_pending
            batch = load_pending()
            if not batch:
                return
            leftover = [x for x in batch.get('links', [])
                        if x.get('status') in ('pending', 'failed')]
            if not leftover:
                from pending_queue import clear_pending
                clear_pending()
                return
            # 与孤儿续传去重：同一条链接已被 start_download_resume 提交则跳过
            orphan_links = set()
            try:
                for j in processor.find_orphaned_download_jobs():
                    p = (j.get('params') or {})
                    if p.get('link'):
                        orphan_links.add(p['link'])
            except Exception:
                pass
            leftover = [x for x in leftover if x.get('link') not in orphan_links]
            if not leftover:
                return
            message = '\n'.join(x['msg'] for x in leftover if x.get('msg'))
            if not message:
                return
            if not _wait_idle():
                logger.log('system', 'WARN',
                           '批次队列恢复：控制器持续忙碌，放弃本次自动恢复'
                           '（队列文件保留）')
                return
            logger.log('system', 'WARN',
                       f'批次队列恢复：{len(leftover)}条链接未完成，重新提交')
            for attempt in range(3):
                body, status = process_controller.start(message)
                if isinstance(status, int) and status < 300:
                    logger.log('system', 'INFO', '批次队列恢复：已提交剩余链接')
                    return
                if status == 409 and _wait_idle():
                    continue
                logger.log('system', 'WARN',
                           f'批次队列恢复被拒（{status}），60s后重试'
                           f'（{attempt + 1}/3）')
                time.sleep(60)
        except Exception as e:
            logger.log('system', 'ERROR', f'批次队列恢复失败: {e}')

    threading.Thread(target=_run, daemon=True).start()


def start_drama_job_reconcile(job_store, logger):
    """P0-1 续跑对账：上次运行中断的单剧任务重新排进批次队列。

    进程被杀时 drama 任务停在 running；重启后先按 link 补回
    data/pending_batch.json，由 start_pending_batch_resume 统一重跑，
    任务行状态改回 pending 保留轨迹（下次 start 会复用同一 job_id）。
    """
    def _run():
        try:
            import drama_jobs
            from pending_queue import add_pending
            stuck = drama_jobs.list_unfinished(job_store)
            if not stuck:
                return
            items = []
            for job in stuck:
                p = job.get('params') or {}
                if not (p.get('link') or p.get('link_id')):
                    continue
                items.append({'link': p.get('link') or '',
                              'link_id': p.get('link_id') or '',
                              'drama_name': p.get('drama_name') or ''})
            added = add_pending(items) if items else 0
            for job in stuck:
                try:
                    drama_jobs.stage(job_store, job['job_id'], 'resume_pending')
                except Exception:
                    pass
            logger.log('system', 'WARN',
                       f'单剧任务对账：{len(stuck)} 个未完成，补排队 {added} 条，'
                       f'交由批次续跑接管')
        except Exception as e:
            logger.log('system', 'ERROR', f'单剧任务对账失败: {e}')

    threading.Thread(target=_run, daemon=True).start()


def start_disk_watermark_watch(artifact_cleanup, logger, CONFIG,
                               interval_seconds=1800):
    """P2-12 磁盘水位看护：超阈值时自动回收已交付批次的素材/成片。

    每批 12 部 × 8 版约 40–50G；等到盘满再人工清理会把流水线卡死在下载阶段。
    这里只做“移动到回收站”的回收，不做永久删除。
    """
    def _run():
        threshold = (CONFIG.get('cleanup') or {}).get('disk_watermark_pct', 85)
        while True:
            time.sleep(interval_seconds)
            try:
                rep = artifact_cleanup.watermark_sweep(threshold_pct=threshold)
                if rep.get('over'):
                    logger.log('system', 'WARN',
                               f'磁盘水位告警：{rep["usage"]} 超阈值 {threshold}%，'
                               f'已回收 {len(rep["cleaned"])} 批'
                               f'{"，失败 " + str(len(rep["failed"])) + " 批" if rep["failed"] else ""}')
            except Exception as e:
                logger.log('system', 'ERROR', f'磁盘水位检查失败: {e}')

    threading.Thread(target=_run, daemon=True, name='disk-watermark').start()


def start_logs_cleanup(logger):
    """后台周期性清理过期日志。"""
    def _run():
        while True:
            time.sleep(3600 * 24)  # 每小时检查一次
            deleted = logger.cleanup_logs(30)
            if deleted > 0:
                logger.log('system', 'INFO', f'清理了{deleted}个过期日志文件')

    threading.Thread(target=_run, daemon=True).start()


def start_cleanup_sweep(artifact_cleanup, logger):
    """服务启动补扫：上次进程结束前已标记完成且启用自动清理、但清理未落地的批次。

    处理「服务重启后自动清理不会执行」的缺口（#4）：后端 worker 在批次自然结束时
    同步执行清理，正常不会遗留；仅当进程在「标记完成」与「清理执行」之间崩溃时，
    此补扫在下一次启动时补跑。execute_cleanup 幂等（已回收的路径不会重复移动）。
    """
    def _run():
        try:
            for batch_id in artifact_cleanup.list_startup_cleanup_batches():
                try:
                    logger.log('system', 'INFO', f'启动补扫：批次 {batch_id} 补跑自动清理')
                    artifact_cleanup.execute_cleanup(batch_id, strategy=STRATEGY_RECYCLE)
                except Exception as e:
                    logger.log('system', 'ERROR', f'启动补扫：批次 {batch_id} 清理失败: {e}')
        except Exception as e:
            logger.log('system', 'ERROR', f'启动补扫失败: {e}')

    threading.Thread(target=_run, daemon=True).start()
