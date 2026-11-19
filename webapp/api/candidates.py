# -*- coding: utf-8 -*-
"""候选库和自动选剧新入口。"""
import os
from flask import Blueprint, jsonify, request, Response

import candidate_library as library


def create_candidates_bp(deps):
    bp = Blueprint('candidates', __name__)
    CONFIG = deps['CONFIG']
    logger = deps['logger']
    automatic = deps['automatic_selection']

    @bp.route('/api/candidates/status')
    def candidate_status():
        try:
            return Response(__import__('json').dumps(library.get_import_status(), ensure_ascii=False),
                            mimetype='application/json')
        except Exception as exc:
            return jsonify({'status': 'failed', 'error': str(exc)}), 500

    @bp.route('/api/candidates/list')
    def candidate_list():
        try:
            data = library.list_candidates(
                q=request.args.get('q', '').strip() or None,
                channel=request.args.get('channel', '').strip() or None,
                category=request.args.get('category', '').strip() or None,
                state=request.args.get('state', '').strip() or None,
                page=request.args.get('page', 1, type=int),
                limit=request.args.get('limit', 50, type=int))
            from archive import get_archive
            archive_db = os.path.join(CONFIG['data_dir'], 'upload.db')
            published_titles = set()
            try:
                import sqlite3
                conn = sqlite3.connect(archive_db, timeout=2)
                try:
                    exists = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='upload_records'").fetchone()
                    if exists:
                        published_titles = {row[0] for row in conn.execute(
                            "SELECT DISTINCT drama_name FROM upload_records "
                            "WHERE status='completed'")}
                finally:
                    conn.close()
            except Exception:
                published_titles = set()
            for item in data['items']:
                path = os.path.join(CONFIG['download_base_dir'], item['title'])
                item['local_dir_exists'] = os.path.isdir(path)
                record = get_archive(item['title'])
                item['download_state'] = (record or {}).get('download_state') or (
                    'done' if item['local_dir_exists'] else 'none')
                item['slice_state'] = (record or {}).get('slice_state') or 'none'
                item['upload_state'] = (record or {}).get('upload_state') or 'none'
                item['published'] = bool(
                    item.get('published_accounts') or item['title'] in published_titles or
                    (record and record.get('notes') and '正式发布' in record['notes']))
                item['local_dir_exists'] = bool(item['local_dir_exists'])
            return Response(__import__('json').dumps(data, ensure_ascii=False),
                            mimetype='application/json')
        except Exception as exc:
            return jsonify({'status': 'failed', 'error': str(exc)}), 500

    @bp.route('/api/candidates/duplicates')
    def candidate_duplicates():
        try:
            conn = __import__('sqlite3').connect(library.DB_PATH)
            conn.row_factory = __import__('sqlite3').Row
            try:
                rows = [dict(row) for row in conn.execute(
                    'SELECT * FROM duplicate_pairs ORDER BY detected_at DESC LIMIT 500')]
            finally:
                conn.close()
            return Response(__import__('json').dumps({'items': rows}, ensure_ascii=False),
                            mimetype='application/json')
        except Exception as exc:
            return jsonify({'status': 'failed', 'error': str(exc)}), 500

    @bp.route('/api/materials/fingerprint-scan', methods=['POST'])
    def fingerprint_scan():
        """对已有本地素材做每剧首集原素材感知指纹，疑似重复只做标记。"""
        try:
            report = {'scanned': 0, 'fingerprinted': 0, 'matches': 0, 'errors': []}
            root = CONFIG['download_base_dir']
            for entry in library.all_candidates():
                if not entry.get('present_in_latest') or entry.get('recycled_at'):
                    continue
                folder = os.path.join(root, entry['title'])
                if not os.path.isdir(folder):
                    continue
                report['scanned'] += 1
                files = []
                for walk_root, _dirs, names in os.walk(folder):
                    files.extend(os.path.join(walk_root, name) for name in names
                                 if name.lower().endswith('.mp4') and
                                 '.downloading' not in name.lower())
                if not files:
                    continue
                try:
                    from content_fingerprint import extract_gray_frames, frame_dhash
                    video = next((path for path in sorted(files)
                                  if os.path.getsize(path) > 0), None)
                    frames = extract_gray_frames(video) if video else None
                    if frames is None or len(frames) == 0:
                        continue
                    seq = [frame_dhash(frame) for frame in frames]
                    matches = library.find_source_matches(seq, entry['title'])
                    library.save_source_fingerprint(entry['title'], video, seq,
                                                     size_bytes=os.path.getsize(video))
                    report['fingerprinted'] += 1
                    if matches:
                        library.update_candidate_flags(entry['title'], duplicate_marked=True)
                        for match in matches:
                            library.update_candidate_flags(match['title'], duplicate_marked=True)
                            library.save_duplicate_pair(entry['title'], match['title'],
                                                        match['similarity'])
                        report['matches'] += len(matches)
                except Exception as exc:
                    report['errors'].append(f"{entry['title']}: {exc}")
            return Response(__import__('json').dumps({'status': 'success', 'report': report}, ensure_ascii=False),
                            mimetype='application/json')
        except Exception as exc:
            return jsonify({'status': 'failed', 'error': str(exc)}), 500

    @bp.route('/api/candidates/duplicates/review', methods=['POST'])
    def review_duplicate():
        body = request.get_json(silent=True) or {}
        left, right = body.get('left_title'), body.get('right_title')
        state = body.get('status')
        if not left or not right or state not in ('confirmed', 'dismissed'):
            return jsonify({'status': 'failed', 'error': '缺少剧名或复核状态'}), 400
        library.save_duplicate_pair(left, right, float(body.get('similarity', 0)), state)
        library.set_candidate_flag_by_key(library.normalize_title(left),
                                          duplicate_marked=(state == 'confirmed'))
        library.set_candidate_flag_by_key(library.normalize_title(right),
                                          duplicate_marked=(state == 'confirmed'))
        return jsonify({'status': 'success'})

    @bp.route('/api/candidates/import', methods=['POST'])
    def candidate_import():
        try:
            upload = request.files.get('file')
            if upload:
                import tempfile
                fd, temp_path = tempfile.mkstemp(prefix='candidate-import-', suffix='.csv',
                                                 dir=CONFIG.get('temp_dir'))
                os.close(fd)
                try:
                    upload.save(temp_path)
                    result = library.import_csv(
                        temp_path,
                        fixed_path=os.path.join(deps['project_root'], '素材', '候选库', '候选库.csv'))
                finally:
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
            else:
                data = request.get_json(silent=True) or {}
                path = data.get('path')
                if not path or not os.path.isfile(path):
                    return jsonify({'status': 'failed', 'error': '请上传 CSV 文件'}), 400
                result = library.import_csv(
                    path, fixed_path=os.path.join(deps['project_root'], '素材',
                                                  '候选库', '候选库.csv'))
            return Response(__import__('json').dumps({'status': 'success', **result}, ensure_ascii=False),
                            mimetype='application/json')
        except Exception as exc:
            return jsonify({'status': 'failed', 'error': str(exc)}), 400

    @bp.route('/api/candidates/auto-select', methods=['POST'])
    def auto_select():
        body = request.get_json(silent=True) or {}
        result, code = automatic.start(body.get('count', 2), account=body.get('account'))
        return Response(__import__('json').dumps(result, ensure_ascii=False),
                        mimetype='application/json', status=code)

    @bp.route('/api/candidates/auto-select/status')
    def auto_select_status():
        try:
            return Response(__import__('json').dumps(automatic.status(), ensure_ascii=False),
                            mimetype='application/json')
        except Exception as exc:
            return jsonify({'status': 'failed', 'error': str(exc)}), 500

    @bp.route('/api/candidates/auto-select/stop', methods=['POST'])
    def auto_select_stop():
        result, code = automatic.stop()
        return Response(__import__('json').dumps(result, ensure_ascii=False),
                        mimetype='application/json', status=code)

    @bp.route('/api/candidates/flags', methods=['POST'])
    def candidate_flags():
        body = request.get_json(silent=True) or {}
        title = body.get('title') or ''
        if not title:
            return jsonify({'status': 'failed', 'error': '缺少剧名'}), 400
        updated = library.update_candidate_flags(
            title, weak_marked=body.get('weak_marked'),
            duplicate_marked=body.get('duplicate_marked'))
        return jsonify({'status': 'success' if updated else 'not_found'})

    @bp.route('/api/materials/reconcile', methods=['POST'])
    def reconcile_materials():
        """调用现有档案扫描/修复能力作为素材状态对账入口。"""
        report = {'archive': 0, 'cut': None, 'backfilled': 0}
        try:
            from archive import get_archive, upsert_archive
            candidates = library.all_candidates()
            for item in candidates:
                name = item['title']
                local = os.path.join(CONFIG['download_base_dir'], name)
                if os.path.isdir(local):
                    record = get_archive(name) or {}
                    if not record:
                        upsert_archive(name, local_dir=local,
                                       download_state='done')
                    else:
                        update = {}
                        if not record.get('local_dir'):
                            update['local_dir'] = local
                        if record.get('download_state') == 'none':
                            update['download_state'] = 'done'
                        if update:
                            upsert_archive(name, **update)
                    report['archive'] += 1
            report['cut'] = deps['batch_cut_manager'].reconcile()
            report['backfilled'] = 0
            conn = __import__('sqlite3').connect(os.path.join(CONFIG['data_dir'], 'upload.db'))
            try:
                archive_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='drama_archive'").fetchone()
                rows = (conn.execute('''SELECT drama_name, source_url, source_passwd, rating, category
                    FROM drama_archive''').fetchall() if archive_table else [])
                by_title = {library.normalize_title(x['title']): x for x in candidates}
                for drama, source_url, source_passwd, rating, category in rows:
                    candidate = by_title.get(library.normalize_title(drama))
                    if not candidate:
                        continue
                    # 只补空字段。CSV 新值不会覆盖档案现有的值和状态。
                    update = {}
                    candidate_url = candidate.get('source_url')
                    if not source_url and candidate_url:
                        update['source_url'] = candidate['source_url']
                        if candidate.get('source_passwd'):
                            update['source_passwd'] = candidate['source_passwd']
                    elif not source_passwd and candidate.get('source_passwd'):
                        update['source_passwd'] = candidate['source_passwd']
                    if not rating and candidate.get('rating'):
                        update['rating'] = candidate['rating']
                    if not category and candidate.get('category'):
                        update['category'] = candidate['category']
                    if update:
                        upsert_archive(drama, **update)
                        report['backfilled'] += 1
            finally:
                conn.close()
            return Response(__import__('json').dumps({'status': 'success', 'report': report}, ensure_ascii=False),
                            mimetype='application/json')
        except Exception as exc:
            return jsonify({'status': 'failed', 'error': str(exc), 'report': report}), 500

    @bp.route('/api/materials/reconcile/auto-cut', methods=['POST'])
    def reconcile_auto_cut():
        """自动对账本轮切片项，避免运行页因恢复任务而误报结束。"""
        body = request.get_json(silent=True) or {}
        run_id = body.get('run_id')
        run = library.get_auto_run(run_id) if run_id else None
        if not run:
            return jsonify({'status': 'failed', 'error': '自动选剧任务不存在'}), 404
        manager = deps['batch_cut_manager']
        status_map = deps['automatic_selection'].status()
        refreshed = status_map.get('run')
        return Response(__import__('json').dumps(refreshed or run, ensure_ascii=False),
                        mimetype='application/json')

    @bp.route('/api/materials/recycle', methods=['POST'])
    def recycle_material():
        body = request.get_json(silent=True) or {}
        title = body.get('title') or ''
        if not title:
            return jsonify({'status': 'failed', 'error': '缺少剧名'}), 400
        path = os.path.join(CONFIG['download_base_dir'], title)
        try:
            target = library.recycle_material(title, path)
            return Response(__import__('json').dumps({'status': 'success', 'path': target}, ensure_ascii=False),
                            mimetype='application/json')
        except Exception as exc:
            return jsonify({'status': 'failed', 'error': str(exc)}), 400

    @bp.route('/api/materials/recycle-bin')
    def recycle_bin():
        return Response(__import__('json').dumps({'items': library.list_recycle_items()}, ensure_ascii=False),
                        mimetype='application/json')

    @bp.route('/api/materials/recycle-bin/empty', methods=['POST'])
    def empty_recycle_bin():
        body = request.get_json(silent=True) or {}
        if body.get('confirm') is not True:
            return jsonify({'status': 'failed', 'error': '请在确认对话框后再清空回收站'}), 400
        try:
            library.empty_recycle_bin()
            return jsonify({'status': 'success'})
        except Exception as exc:
            return jsonify({'status': 'failed', 'error': str(exc)}), 500

    @bp.route('/api/materials/recycle-bin/item', methods=['DELETE'])
    def remove_recycle_item():
        body = request.get_json(silent=True) or {}
        path = body.get('path') or ''
        if not path:
            return jsonify({'status': 'failed', 'error': '缺少回收项路径'}), 400
        try:
            library.delete_recycle_item(path)
            return jsonify({'status': 'success'})
        except Exception as exc:
            return jsonify({'status': 'failed', 'error': str(exc)}), 400

    return bp
