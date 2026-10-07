"""Exercise preview, persisted rights and public consent across all entry points."""
import datetime
import io
from unittest.mock import patch

import pytest
from PIL import Image
from auth import get_current_user
from main import app
from models import User, GenerationLog, Collection


@pytest.fixture(autouse=True)
def account(db):
    user = User(id="license-owner", email="license@example.test", username="License", terms_agreed=True, credits=100)
    db.add(user)
    db.commit()
    app.dependency_overrides[get_current_user] = lambda: user
    yield user
    app.dependency_overrides.pop(get_current_user, None)


def pro(account, db):
    account.pro_level = "pro-plus"
    account.pro_expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)
    db.commit()


def png(size=64):
    stream = io.BytesIO()
    Image.new("RGB", (size, size)).save(stream, format="PNG")
    return {"file": ("skin.png", stream.getvalue(), "image/png")}


@pytest.mark.parametrize("is_pro,is_public", [(False, True), (True, True), (True, False)])
@pytest.mark.parametrize("source,expected", [("original", "original-work"), ("external", "source-license")])
@pytest.mark.parametrize("mode", ["human_upload", "human_edit"])
def test_both_manual_entries_match_preview(client, db, account, is_pro, is_public, source, expected, mode):
    if is_pro:
        pro(account, db)
    preview = client.get("/api/licenses/preview", params={"operation": "save", "source_rights": source, "is_public": is_public})
    assert preview.status_code == 200
    assert preview.json()["code"] == expected
    destination = "creations_public" if is_public else "creations_private"
    with patch("s3_utils.s3_client"):
        response = client.post(f"/api/collections/{destination}/upload", files=png(), data={
            "mode": mode, "source_rights": source, "license_consent": True, "public_license_consent": is_public})
    assert response.status_code == 200, response.text
    log = db.get(GenerationLog, response.json()["log_id"])
    assert log.license == preview.json()["code"]
    assert log.public_license == preview.json()["public_license"] == ("cc-by-nc-4.0" if is_public else None)


@pytest.mark.parametrize("owner,license_code,expected", [
    (True, "entropydrop-commercial-1.0", "entropydrop-commercial-1.0"),
    (True, "original-work", "original-work"),
    (True, "source-license", "source-license"),
    (False, "entropydrop-commercial-1.0", "cc-by-nc-4.0"),
    (False, "original-work", "cc-by-nc-4.0"),
])
def test_manual_edits_inherit_without_pro_or_overriding_from_declaration(client, db, account, owner, license_code, expected):
    source = GenerationLog(user_id=account.id if owner else "other", mode="human_upload", status="success",
        is_public=True, license=license_code, public_license="cc-by-nc-4.0")
    db.add(source)
    db.commit()
    params = {"operation": "save", "parent": source.id, "source_rights": "original"}
    assert client.get("/api/licenses/preview", params=params).json()["code"] == expected
    with patch("s3_utils.s3_client"):
        response = client.post("/api/collections/creations_public/upload", files=png(), data={
            "mode": "human_edit", "parent": source.id, "source_rights": "original", "public_license_consent": True})
    assert response.status_code == 200, response.text
    saved = db.get(GenerationLog, response.json()["log_id"])
    assert saved.license == expected
    assert saved.public_license == "cc-by-nc-4.0"
    assert saved.parent == source.id


@pytest.mark.parametrize("is_pro,is_public,expected", [(False, True, "cc-by-nc-4.0"), (True, True, "entropydrop-commercial-1.0"), (True, False, "entropydrop-commercial-1.0")])
def test_new_generation_preview_matches_persisted_grant(client, db, account, is_pro, is_public, expected):
    if is_pro:
        pro(account, db)
    preview = client.get("/api/licenses/preview", params={"operation": "generate", "is_public": is_public})
    with patch("routers.generate.enqueue_generation_task"):
        response = client.post("/api/generate", data={"mode": "aigc_text_to_skin", "prompt": "robot",
            "aux_model_version": "z_image", "model_version": "sking_v73_flux_4b_000027000",
            "is_public": is_public, "public_license_consent": is_public})
    assert response.status_code == 200, response.text
    saved = db.get(GenerationLog, response.json()["id"])
    assert saved.license == preview.json()["code"] == expected
    assert saved.public_license == preview.json()["public_license"]


def test_public_generation_requires_consent_before_enqueue_or_charge(client, db, account):
    with patch("routers.generate.enqueue_generation_task") as enqueue:
        response = client.post("/api/generate", data={"prompt": "robot", "aux_model_version": "z_image", "model_version": "sking_v73_flux_4b_000027000"})
    assert response.status_code == 400
    assert "CC BY-NC" in response.json()["detail"]
    assert db.query(GenerationLog).count() == 0
    db.refresh(account)
    assert account.credits == 100
    enqueue.assert_not_called()


@pytest.mark.parametrize("missing", [False, True])
def test_preview_and_generation_reject_inaccessible_sources(client, db, account, missing):
    source = GenerationLog(user_id="other", mode="human_upload", status="success", is_public=False, license="original-work")
    db.add(source)
    db.commit()
    parent = "missing" if missing else source.id
    preview = client.get("/api/licenses/preview", params={"operation": "generate", "parent": parent})
    response = client.post("/api/generate", data={"prompt": "robot", "parent": parent, "public_license_consent": True,
        "aux_model_version": "z_image", "model_version": "sking_v73_flux_4b_000027000"})
    assert preview.status_code == response.status_code == (404 if missing else 403)


def test_collecting_public_skin_privately_does_not_change_rights(client, db, account):
    pro(account, db)
    source = GenerationLog(user_id="other", mode="human_upload", status="success", is_public=True,
        license="original-work", public_license="cc-by-nc-4.0", result="skin.png")
    folder = Collection(user_id=account.id, name="Private folder", is_public=False)
    db.add_all([source, folder])
    db.commit()
    response = client.post("/api/collections/items", json={"collection_id": folder.id, "log_id": source.id, "name": "Collected", "type": "image", "data": {}})
    assert response.status_code == 200, response.text
    db.refresh(source)
    assert source.is_public and source.license == "original-work" and source.public_license == "cc-by-nc-4.0"
    preview = client.get("/api/licenses/preview", params={"operation": "save", "parent": source.id, "is_public": False})
    assert preview.json()["code"] == "cc-by-nc-4.0"


def test_explicit_public_consent_refusal_is_not_overridden_by_upload_consent(client, db):
    with patch("s3_utils.s3_client") as storage:
        response = client.post("/api/collections/creations_public/upload", files=png(), data={
            "license_consent": True, "public_license_consent": False,
        })
    assert response.status_code == 400
    assert "Public sharing requires consent" in response.json()["detail"]
    assert db.query(GenerationLog).count() == 0
    storage.put_object.assert_not_called()


@pytest.mark.parametrize("status", ["pending", "processing", "failed"])
def test_manual_save_rejects_unfinished_sources_like_preview(client, db, account, status):
    source = GenerationLog(user_id=account.id, mode="aigc_text_to_skin", status=status,
                           is_public=True, license="entropydrop-commercial-1.0")
    db.add(source)
    db.commit()
    preview = client.get("/api/licenses/preview", params={"operation": "save", "parent": source.id})
    with patch("s3_utils.s3_client") as storage:
        saved = client.post("/api/collections/creations_public/upload", files=png(), data={
            "parent": source.id, "mode": "human_edit", "public_license_consent": True,
        })
    assert preview.status_code == saved.status_code == 404
    assert db.query(GenerationLog).count() == 1
    storage.put_object.assert_not_called()


def test_private_source_save_preview_never_promises_a_public_license(client, db, account):
    pro(account, db)
    source = GenerationLog(user_id=account.id, mode="human_upload", status="success",
                           is_public=False, license="source-license")
    db.add(source)
    db.commit()
    preview = client.get("/api/licenses/preview", params={"operation": "save", "parent": source.id})
    assert preview.status_code == 200
    assert preview.json()["parent_is_private"] is True
    assert preview.json()["public_license"] is None
    with patch("s3_utils.s3_client"):
        saved = client.post("/api/collections/creations_private/upload", files=png(), data={
            "parent": source.id, "mode": "human_edit",
        })
    assert saved.status_code == 200
    log = db.get(GenerationLog, saved.json()["log_id"])
    assert log.license == preview.json()["code"] == "source-license"
    assert log.public_license is None
