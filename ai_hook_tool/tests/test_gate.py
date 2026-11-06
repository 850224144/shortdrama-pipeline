# -*- coding: utf-8 -*-
from hooktool.gate import evaluate, PASS, FAIL, PENDING, WARN


def _material(faces=True):
    return {"faces": {"白玉瑶": "x.png"} if faces else {}}


def _collect():
    return {"clips": [], "missing": [], "warns": [], "pending": []}


def _assemble(duration=31.0, badge_ok=True):
    return {"duration": duration, "width": 1080, "height": 1920,
             "has_audio": True, "badge_ok": badge_ok}


def _review(**overrides):
    base = {"face_consistent": True, "similarity": 0.6, "genre_match": True,
            "animal_hook": False, "animal_has_main": False}
    base.update(overrides)
    return base


def test_all_filled_pass():
    report = evaluate(_material(), {}, _collect(), _assemble(), _review())
    assert report["overall"] == PASS
    assert report["block"] is False
    assert report["warns"] == []
    assert report["pending"] == []


def test_similarity_below_threshold_fail_block():
    report = evaluate(_material(), {}, _collect(), _assemble(), _review(similarity=0.3))
    assert report["overall"] == FAIL
    assert report["block"] is True
    assert report["fail_reasons"]


def test_duration_below_min_fail_block():
    report = evaluate(_material(), {}, _collect(), _assemble(duration=20.0), _review())
    assert report["overall"] == FAIL
    assert report["block"] is True


def test_missing_evidence_pending_not_pass():
    report = evaluate(_material(), {}, _collect(), _assemble(), _review(similarity=None))
    assert report["overall"] == PENDING
    assert report["block"] is False


def test_face_mismatch_soft_warn_not_block():
    report = evaluate(_material(), {}, _collect(), _assemble(), _review(face_consistent=False))
    assert report["overall"] == WARN
    assert report["block"] is False
    assert any("脸" in w for w in report["warns"])


def test_badge_failure_soft_warn_not_block():
    report = evaluate(_material(), {}, _collect(), _assemble(badge_ok=False), _review())
    assert report["overall"] == WARN
    assert report["block"] is False
    assert any("角标" in w for w in report["warns"])


def test_pure_animal_hook_fail():
    report = evaluate(_material(), {}, _collect(), _assemble(),
                      _review(animal_hook=True, animal_has_main=False))
    assert report["overall"] == FAIL
    assert report["block"] is True


def test_animal_hook_with_main_passes():
    report = evaluate(_material(), {}, _collect(), _assemble(),
                      _review(animal_hook=True, animal_has_main=True))
    assert report["overall"] == PASS


def test_no_face_ref_pending():
    report = evaluate(_material(faces=False), {}, _collect(), _assemble(), _review())
    assert report["overall"] == PENDING
    assert any("定妆" in p for p in report["pending"])
