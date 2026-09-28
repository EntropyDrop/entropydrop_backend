import datetime
import io
from unittest.mock import Mock

import pytest
from PIL import Image

import backend_utils
import models
import routers.generate as generate
import routers.monitor as monitor
from auth import get_current_admin, get_current_user
from conftest import FakeRedis
from main import app
from pipeline_registry import SKING_DDJ_V101C


@pytest.fixture(autouse=True)
def pricing_store(monkeypatch):
    store = FakeRedis()
    monkeypatch.setattr(backend_utils, "redis_conn", store)
    monkeypatch.setattr(monitor, "redis_conn", store)
    return store


@pytest.fixture
def pricing_user(db):
    user = models.User(
        id="pricing_user",
        email="pricing@example.com",
        username="Pricing",
        terms_agreed=True,
        credits=100,
        pro_level="free",
    )
    db.add(user)
    db.commit()
    app.dependency_overrides[get_current_user] = lambda: user
    yield user
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_current_admin, None)


def set_membership(db, user, tier):
    user.pro_level = "pro-plus" if tier == "expired" else tier
    user.pro_expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        days=-1 if tier == "expired" else 1
    )
    db.commit()


def save_prices(client, user, **overrides):
    app.dependency_overrides[get_current_admin] = lambda: user
    response = client.post("/skin/api/monitor/model_prices", json={
        "model_name": SKING_DDJ_V101C,
        "credits": 4,
        "free_credits": 12,
        "is_pro": False,
        "under_maintenance": False,
        **overrides,
    })
    assert response.status_code == 200, response.text
    return response.json()


def submit_image(client, **overrides):
    image = io.BytesIO()
    Image.new("RGB", (768, 768), color="red").save(image, format="PNG")
    return client.post("/skin/api/generate", data={
        "model_version": SKING_DDJ_V101C,
        "mode": "aigc_image_to_skin",
        "is_public": True,
        **overrides,
    }, files={"file": ("source.png", image.getvalue(), "image/png")})


@pytest.fixture
def generation_io(monkeypatch):
    enqueue = Mock()
    monkeypatch.setattr(generate, "enqueue_generation_task", enqueue)
    monkeypatch.setattr(generate, "upload_to_s3", Mock())
    return enqueue


@pytest.mark.parametrize("is_pro", [False, True])
def test_unconfigured_free_price_keeps_legacy_price(pricing_store, is_pro):
    pricing_store.set(f"config:model_price:{SKING_DDJ_V101C}", 4)
    assert backend_utils.get_model_credit_cost(SKING_DDJ_V101C, is_pro=is_pro) == 4
    assert backend_utils.get_model_credit_cost("z_image", is_pro=is_pro) == 0
    assert backend_utils.get_model_credit_cost(generate.LEGACY_SKIN_MODEL_VERSION, is_pro=is_pro) == 1


def test_zero_free_price_is_not_treated_as_missing(pricing_store):
    pricing_store.set(f"config:model_price:{SKING_DDJ_V101C}", 4)
    pricing_store.set(f"config:model_free_price:{SKING_DDJ_V101C}", 0)
    assert backend_utils.get_model_credit_cost(SKING_DDJ_V101C) == 0
    assert backend_utils.get_model_credit_cost(SKING_DDJ_V101C, is_pro=True) == 4


def test_monitor_round_trip_and_legacy_updates(client, pricing_user, pricing_store):
    saved = save_prices(client, pricing_user)
    response = client.get("/skin/api/monitor/model_prices")
    assert response.status_code == 200
    assert response.json()[SKING_DDJ_V101C] == {
        key: value for key, value in saved.items() if key != "model_name"
    }
    assert pricing_store.get(f"config:model_price:{SKING_DDJ_V101C}") == b"4"
    assert pricing_store.get(f"config:model_free_price:{SKING_DDJ_V101C}") == b"12"

    # An older admin client must not erase an already configured Free price.
    response = client.post("/skin/api/monitor/model_prices", json={
        "model_name": SKING_DDJ_V101C,
        "credits": 6,
    })
    assert response.status_code == 200
    assert response.json()["credits"] == 6
    assert response.json()["free_credits"] == 12


def test_monitor_legacy_save_without_override_uses_shared_price(client, pricing_user):
    app.dependency_overrides[get_current_admin] = lambda: pricing_user
    response = client.post("/skin/api/monitor/model_prices", json={
        "model_name": SKING_DDJ_V101C,
        "credits": 4,
    })
    assert response.status_code == 200
    assert response.json()["free_credits"] == 4


@pytest.mark.parametrize("field", ["credits", "free_credits"])
@pytest.mark.parametrize("value,status", [(-1, 400), (1.5, 422)])
def test_monitor_rejects_invalid_prices_without_changing_config(
    client, pricing_user, field, value, status
):
    save_prices(client, pricing_user)
    response = client.post("/skin/api/monitor/model_prices", json={
        "model_name": SKING_DDJ_V101C,
        "credits": 4,
        "free_credits": 12,
        field: value,
        "is_pro": True,
    })
    assert response.status_code == status
    assert backend_utils.get_model_credit_cost(SKING_DDJ_V101C) == 12
    assert backend_utils.get_model_credit_cost(SKING_DDJ_V101C, is_pro=True) == 4
    assert not backend_utils.is_model_pro_exclusive(SKING_DDJ_V101C)


def test_monitor_prices_require_admin(client):
    assert client.get("/skin/api/monitor/model_prices").status_code in (401, 403)
    assert client.post("/skin/api/monitor/model_prices", json={
        "model_name": SKING_DDJ_V101C, "credits": 4, "free_credits": 12,
    }).status_code in (401, 403)


@pytest.mark.parametrize("tier,cost", [
    ("free", 12), ("pro-plus", 4), ("pro-max", 4), ("expired", 12),
])
def test_quote_and_charge_follow_active_membership(
    client, db, pricing_user, generation_io, tier, cost
):
    set_membership(db, pricing_user, tier)
    save_prices(client, pricing_user)
    quote = client.get("/skin/api/generation_credit_cost", params={
        "model_version": SKING_DDJ_V101C,
        # The caller cannot choose their pricing tier.
        "is_pro": True,
    })
    assert quote.status_code == 200
    assert quote.json() == {"credits": cost, "is_pro": False, "under_maintenance": False}

    response = submit_image(client)
    assert response.status_code == 200, response.text
    db.refresh(pricing_user)
    log = db.get(models.GenerationLog, response.json()["id"])
    assert pricing_user.credits == 100 - cost
    assert log.credits_charged == cost
    assert log.is_pro is (tier in ("pro-plus", "pro-max"))
    assert db.query(models.CreditLog).filter_by(action="generation").one().amount == -cost
    generation_io.assert_called_once()


@pytest.mark.parametrize("tier,cost", [("free", 15), ("pro-plus", 6)])
def test_combined_models_use_same_membership_tier(
    client, db, pricing_user, generation_io, tier, cost
):
    set_membership(db, pricing_user, tier)
    save_prices(client, pricing_user, model_name=generate.LEGACY_SKIN_MODEL_VERSION)
    save_prices(client, pricing_user, model_name="z_image", credits=2, free_credits=3)
    params = {
        "model_version": generate.LEGACY_SKIN_MODEL_VERSION,
        "aux_model_version": "z_image",
    }
    quote = client.get("/skin/api/generation_credit_cost", params=params)
    assert quote.status_code == 200
    assert quote.json()["credits"] == cost
    response = client.post("/skin/api/generate", data={
        **params, "prompt": "tiered pricing", "mode": "aigc_text_to_skin", "is_public": True,
    })
    assert response.status_code == 200, response.text
    db.refresh(pricing_user)
    assert pricing_user.credits == 100 - cost
    assert db.get(models.GenerationLog, response.json()["id"]).credits_charged == cost


def test_free_user_cannot_generate_with_only_pro_price_balance(
    client, db, pricing_user, generation_io
):
    save_prices(client, pricing_user)
    pricing_user.credits = 4
    db.commit()
    response = submit_image(client)
    assert response.status_code == 403
    assert response.json()["detail"] == "Insufficient credits"
    db.refresh(pricing_user)
    assert pricing_user.credits == 4
    assert db.query(models.GenerationLog).count() == 0
    assert db.query(models.CreditLog).count() == 0
    generation_io.assert_not_called()


@pytest.mark.parametrize("flag", ["is_pro", "under_maintenance"])
def test_tiered_prices_do_not_bypass_access_controls(
    client, pricing_user, generation_io, flag
):
    save_prices(client, pricing_user, **{flag: True})
    response = submit_image(client)
    assert response.status_code == 403
    generation_io.assert_not_called()


@pytest.mark.parametrize("tier,cost", [("free", 12), ("pro-plus", 4)])
def test_refund_uses_original_charge_after_prices_and_membership_change(
    client, db, pricing_user, generation_io, tier, cost
):
    set_membership(db, pricing_user, tier)
    save_prices(client, pricing_user)
    response = submit_image(client)
    assert response.status_code == 200
    log = db.get(models.GenerationLog, response.json()["id"])

    save_prices(client, pricing_user, credits=7, free_credits=20)
    set_membership(db, pricing_user, "pro-max" if tier == "free" else "expired")
    assert generate.refund_generation_credits(db, log) == cost
    db.commit()
    assert generate.refund_generation_credits(db, log) == 0
    db.refresh(pricing_user)
    assert pricing_user.credits == 100
    assert db.query(models.CreditLog).filter_by(action="refund").one().amount == cost


def test_model_list_has_distinct_options_for_one_real_model(client, pricing_user):
    response = client.get("/skin/api/models")
    assert response.status_code == 200
    options = response.json()["image_to_skin_options"]
    latest = [option for option in options if option["model_version"] == SKING_DDJ_V101C]
    assert [option["pricing_tier"] for option in latest] == ["pro", "standard"]
    assert len({option["id"] for option in options}) == len(options)
    assert response.json()["image_to_skin_models"].count(SKING_DDJ_V101C) == 1


@pytest.mark.parametrize("tier", ["free", "expired", "pro-plus", "pro-max"])
@pytest.mark.parametrize("option,cost", [("standard", 12), ("pro", 4)])
def test_explicit_option_quote_permissions_and_charge(
    client, db, pricing_user, generation_io, tier, option, cost
):
    set_membership(db, pricing_user, tier)
    # The legacy model-wide flag must not restrict the explicit All users option.
    save_prices(client, pricing_user, is_pro=True)
    quote = client.get("/skin/api/generation_credit_cost", params={
        "model_version": SKING_DDJ_V101C, "pricing_tier": option,
    })
    assert quote.status_code == 200
    assert quote.json() == {
        "credits": cost, "is_pro": option == "pro", "under_maintenance": False,
    }
    response = submit_image(client, pricing_tier=option)
    db.refresh(pricing_user)
    if option == "pro" and tier in ("free", "expired"):
        assert response.status_code == 403
        assert "exclusive to Pro" in response.json()["detail"]
        assert pricing_user.credits == 100
        assert db.query(models.GenerationLog).count() == 0
        assert db.query(models.CreditLog).count() == 0
        generation_io.assert_not_called()
    else:
        assert response.status_code == 200, response.text
        log = db.get(models.GenerationLog, response.json()["id"])
        assert log.model_version == SKING_DDJ_V101C
        assert log.credits_charged == cost
        assert pricing_user.credits == 100 - cost
        assert db.query(models.CreditLog).filter_by(action="generation").one().amount == -cost
        assert generation_io.call_args.args[0].model_version == SKING_DDJ_V101C


@pytest.mark.parametrize("option", ["standard", "pro"])
def test_maintenance_blocks_both_options(client, db, pricing_user, generation_io, option):
    set_membership(db, pricing_user, "pro-plus")
    save_prices(client, pricing_user, under_maintenance=True)
    quote = client.get("/skin/api/generation_credit_cost", params={
        "model_version": SKING_DDJ_V101C, "pricing_tier": option,
    })
    assert quote.json()["under_maintenance"] is True
    assert submit_image(client, pricing_tier=option).status_code == 403
    generation_io.assert_not_called()


@pytest.mark.parametrize("params,status", [
    ({"model_version": SKING_DDJ_V101C, "pricing_tier": "invalid"}, 422),
    ({"model_version": generate.LEGACY_SKIN_MODEL_VERSION, "pricing_tier": "pro"}, 400),
    ({"model_version": SKING_DDJ_V101C, "aux_model_version": "z_image", "pricing_tier": "pro"}, 400),
])
def test_invalid_pricing_options_are_rejected(
    client, pricing_user, generation_io, params, status
):
    assert client.get("/skin/api/generation_credit_cost", params=params).status_code == status
    assert submit_image(client, **params).status_code == status
    generation_io.assert_not_called()
