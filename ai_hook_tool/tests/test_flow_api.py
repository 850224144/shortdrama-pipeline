# -*- coding: utf-8 -*-
"""Web API：路由错误码、UUID 校验与 202+job_id。"""
import sys
import uuid

import pytest
from flask import Flask

from hooktool import paths


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORKDIR", tmp_path)
    webapp = r"D:\短剧工坊\webapp"
    if webapp not in sys.path:
        sys.path.insert(0, webapp)
    from api.hooks import create_hooks_bp
    app = Flask(__name__)
    app.register_blueprint(create_hooks_bp({"project_root": r"D:\短剧工坊"}))
    app.config.update(TESTING=True)
    return app.test_client()


def test_create_project_requires_source(client):
    response = client.post("/api/hooks/projects", json={})
    assert response.status_code == 422
    body = response.get_json()
    assert body["code"] == "SOURCE_REQUIRED"
    assert body["status"] == "failed"


def test_invalid_project_id_is_rejected_without_path_traversal(client):
    for value in ("not-a-uuid", str(uuid.uuid4())):
        response = client.get(f"/api/hooks/projects/{value}")
        assert response.status_code == 404
        assert response.get_json()["code"] == "PROJECT_NOT_FOUND"


def test_unknown_job_returns_404(client):
    response = client.get(f"/api/hooks/jobs/{uuid.uuid4()}")
    assert response.status_code == 404


def test_create_project_and_async_analysis(client, tmp_path):
    source = tmp_path / "素材"
    source.mkdir()
    (source / "第1集.mp4").write_bytes(b"fake")
    (source / "第1集.srt").write_text(
        "1\n00:00:01,000 --> 00:00:03,000\n测试对白\n", encoding="utf-8")
    response = client.post("/api/hooks/projects", json={
        "source_dir": str(source), "episode": "第1集.mp4",
        "confirm_first_episode": True, "transcript": "第1集.srt",
        "confirm_transcript": True})
    assert response.status_code == 201
    project_id = response.get_json()["project"]["project_id"]
    assert client.get(f"/api/hooks/projects/{project_id}").status_code == 200

    accepted = client.post(f"/api/hooks/projects/{project_id}/analysis",
                           json={"allow_asr": False})
    assert accepted.status_code == 202
    assert accepted.get_json()["job_id"].startswith("job_")
    job = client.get(f"/api/hooks/jobs/{accepted.get_json()['job_id']}")
    assert job.status_code == 200
    assert job.get_json()["stage"] == "analyze"


def test_episode_confirmation_requires_revision(client, tmp_path):
    source = tmp_path / "素材2"
    source.mkdir()
    (source / "第1集.mp4").write_bytes(b"fake")
    (source / "第1集.srt").write_text(
        "1\n00:00:01,000 --> 00:00:03,000\n测试\n", encoding="utf-8")
    created = client.post("/api/hooks/projects", json={
        "source_dir": str(source), "episode": "第1集.mp4",
        "confirm_first_episode": True, "transcript": "第1集.srt",
        "confirm_transcript": True}).get_json()
    project_id = created["project"]["project_id"]
    response = client.post(
        f"/api/hooks/projects/{project_id}/episode-confirmation", json={})
    assert response.status_code == 422
    assert response.get_json()["code"] == "EXPECTED_REVISION_REQUIRED"
