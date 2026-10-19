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
        "public_license_consent": True,
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


@pytest.mark.parametrize("model", generate.AVAILABLE_IMAGE_TO_SKIN_MODELS
                         + generate.AVAILABlE_TEXT_TO_IMAGE_MODELS + generate.AVAILABLE_IMAGE_EDIT_MODELS)
def test_monitor_display_priority_round_trip_legacy_save_and_reset(client, pricing_user, pricing_store, model):
    saved = save_prices(client, pricing_user, model_name=model, display_priority=25)
    assert saved["display_priority"] == 25
    assert pricing_store.get(f"config:model_display_priority:{model}") == b"25"
    assert client.get("/skin/api/monitor/model_prices").json()[model]["display_priority"] == 25

    # Updating prices from an older client must not erase the saved priority.
    saved = save_prices(client, pricing_user, model_name=model, credits=6)
    assert saved["display_priority"] == 25
    saved = save_prices(client, pricing_user, model_name=model, display_priority=0)
    assert saved["display_priority"] == 0
    assert client.get("/skin/api/monitor/model_prices").json()[model]["display_priority"] == 0


@pytest.mark.parametrize("priority", [-1, 1.5, "invalid"])
def test_monitor_rejects_invalid_priority_without_changing_config(client, pricing_user, pricing_store, priority):
    save_prices(client, pricing_user, display_priority=10)
    response = client.post("/skin/api/monitor/model_prices", json={
        "model_name": SKING_DDJ_V101C, "credits": 6, "is_pro": True,
        "display_priority": priority,
    })
    assert response.status_code == 422
    assert pricing_store.get(f"config:model_display_priority:{SKING_DDJ_V101C}") == b"10"
    assert backend_utils.get_model_credit_cost(SKING_DDJ_V101C, is_pro=True) == 4
    assert not backend_utils.is_model_pro_exclusive(SKING_DDJ_V101C)


@pytest.mark.parametrize("stored,expected", [(None, 0), (b"invalid", 0), (b"-2", 0), (b"0", 0), (b"25", 25)])
def test_display_priority_defaults_and_invalid_redis_values(pricing_store, stored, expected):
    if stored is not None:
        pricing_store.set(f"config:model_display_priority:{SKING_DDJ_V101C}", stored)
    assert backend_utils.get_model_display_priority(SKING_DDJ_V101C) == expected


def test_display_priority_defaults_when_redis_is_unavailable(pricing_store, monkeypatch):
    monkeypatch.setattr(pricing_store, "get", Mock(side_effect=ConnectionError("Redis unavailable")))
    assert backend_utils.get_model_display_priority(SKING_DDJ_V101C) == 0


def test_model_priority_sorts_catalog_and_keeps_pricing_tiers_adjacent(client, pricing_user):
    legacy = generate.LEGACY_SKIN_MODEL_VERSION
    original_order = list(generate.AVAILABLE_IMAGE_TO_SKIN_MODELS)
    save_prices(client, pricing_user, model_name=legacy, display_priority=100)
    save_prices(client, pricing_user, display_priority=20)
    catalog = client.get("/skin/api/models").json()
    assert catalog["image_to_skin_models"] == [legacy, SKING_DDJ_V101C]
    assert [option["id"] for option in catalog["image_to_skin_options"]] == [
        f"{model}:{tier}" for model in [legacy, SKING_DDJ_V101C] for tier in ["pro", "standard"]
    ]
    assert generate.AVAILABLE_IMAGE_TO_SKIN_MODELS == original_order

    save_prices(client, pricing_user, display_priority=100)
    assert client.get("/skin/api/models").json()["image_to_skin_models"] == original_order
    save_prices(client, pricing_user, model_name=legacy, display_priority=0)
    assert client.get("/skin/api/models").json()["image_to_skin_models"] == original_order


@pytest.mark.parametrize("catalog_name,list_key,options_key,base", [
    ("AVAILABlE_TEXT_TO_IMAGE_MODELS", "text_to_image_models", "text_to_skin_options", "z_image"),
    ("AVAILABLE_IMAGE_EDIT_MODELS", "image_edit_models", "image_edit_to_skin_options", "flux_4b"),
])
def test_combined_model_options_follow_auxiliary_priority(
    client, pricing_user, monkeypatch, catalog_name, list_key, options_key, base
):
    other = f"another_{base}"
    monkeypatch.setattr(generate, catalog_name, [base, other])
    save_prices(client, pricing_user, model_name=base, credits=0, free_credits=0, display_priority=1)
    save_prices(client, pricing_user, model_name=other, credits=0, free_credits=0, display_priority=10)
    save_prices(client, pricing_user, model_name=generate.LEGACY_SKIN_MODEL_VERSION)
    catalog = client.get("/skin/api/models").json()
    assert catalog[list_key] == [other, base]
    assert [option["id"] for option in catalog[options_key]] == [
        f"{aux} + {generate.LEGACY_SKIN_MODEL_VERSION}:{tier}"
        for aux in [other, base] for tier in ["pro", "standard"]
    ]


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
        "public_license_consent": True,
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


@pytest.mark.parametrize("model", generate.AVAILABLE_IMAGE_TO_SKIN_MODELS)
@pytest.mark.parametrize("free_price,pro_price", [(12, 4), (4, 4), (0, 0), (0, 4)])
@pytest.mark.parametrize("pro_only", [False, True])
def test_model_list_respects_pro_only_before_price_differences(client, pricing_user, model, free_price, pro_price, pro_only):
    saved = save_prices(client, pricing_user, model_name=model, free_credits=free_price,
                        credits=pro_price, is_pro=pro_only)
    response = client.get("/skin/api/models")
    assert response.status_code == 200
    options = response.json()["image_to_skin_options"]
    entries = [option for option in options if option["model_version"] == model]
    if pro_only:
        assert entries == [{"id": f"{model}:pro", "model_version": model, "pricing_tier": "pro"}]
        assert saved["pricing_tiers"] == ["pro"]
        quote = client.get("/skin/api/generation_credit_cost", params={
            "model_version": model, "pricing_tier": "pro",
        })
        assert quote.json() == {"credits": pro_price, "is_pro": True, "under_maintenance": False}
    elif free_price == pro_price:
        assert entries == [{"id": model, "model_version": model}]
        assert saved["pricing_tiers"] == []
    else:
        assert [option["pricing_tier"] for option in entries] == ["pro", "standard"]
        assert saved["pricing_tiers"] == ["pro", "standard"]
    assert len({option["id"] for option in options}) == len(options)
    assert response.json()["image_to_skin_models"].count(model) == 1


@pytest.mark.parametrize("model", generate.AVAILABLE_IMAGE_TO_SKIN_MODELS)
def test_disabling_pro_only_restores_free_option_without_changing_prices(client, pricing_user, model):
    for pro_only, tiers in [(True, ["pro"]), (False, ["pro", "standard"])]:
        save_prices(client, pricing_user, model_name=model, is_pro=pro_only)
        options = client.get("/skin/api/models").json()["image_to_skin_options"]
        assert [option["pricing_tier"] for option in options if option["model_version"] == model] == tiers
        assert backend_utils.get_model_credit_cost(model, is_pro=False) == 12
        assert backend_utils.get_model_credit_cost(model, is_pro=True) == 4


def test_unconfigured_models_have_one_option_each(client, pricing_user):
    response = client.get("/skin/api/models")
    assert response.json()["image_to_skin_options"] == [
        {"id": model, "model_version": model} for model in generate.AVAILABLE_IMAGE_TO_SKIN_MODELS
    ]


@pytest.mark.parametrize("tier", ["free", "expired", "pro-plus", "pro-max"])
@pytest.mark.parametrize("option,cost", [("standard", 12), ("pro", 4)])
@pytest.mark.parametrize("pro_only", [False, True])
@pytest.mark.parametrize("model", generate.AVAILABLE_IMAGE_TO_SKIN_MODELS)
def test_explicit_option_quote_permissions_and_charge(
    client, db, pricing_user, generation_io, tier, option, cost, pro_only, model
):
    set_membership(db, pricing_user, tier)
    save_prices(client, pricing_user, model_name=model, is_pro=pro_only)
    quote = client.get("/skin/api/generation_credit_cost", params={
        "model_version": model, "pricing_tier": option,
    })
    assert quote.status_code == 200
    assert quote.json() == {
        "credits": cost, "is_pro": pro_only or option == "pro", "under_maintenance": False,
    }
    response = submit_image(client, model_version=model, pricing_tier=option)
    db.refresh(pricing_user)
    if (pro_only or option == "pro") and tier in ("free", "expired"):
        assert response.status_code == 403
        assert "exclusive to Pro" in response.json()["detail"]
        assert pricing_user.credits == 100
        assert db.query(models.GenerationLog).count() == 0
        assert db.query(models.CreditLog).count() == 0
        generation_io.assert_not_called()
    else:
        assert response.status_code == 200, response.text
        log = db.get(models.GenerationLog, response.json()["id"])
        assert log.model_version == model
        assert log.credits_charged == cost
        assert pricing_user.credits == 100 - cost
        assert db.query(models.CreditLog).filter_by(action="generation").one().amount == -cost
        assert generation_io.call_args.args[0].model_version == model


@pytest.mark.parametrize("model", generate.AVAILABLE_IMAGE_TO_SKIN_MODELS)
@pytest.mark.parametrize("option", ["standard", "pro"])
def test_maintenance_blocks_both_options(client, db, pricing_user, generation_io, option, model):
    set_membership(db, pricing_user, "pro-plus")
    save_prices(client, pricing_user, model_name=model, under_maintenance=True)
    quote = client.get("/skin/api/generation_credit_cost", params={
        "model_version": model, "pricing_tier": option,
    })
    assert quote.json()["under_maintenance"] is True
    assert submit_image(client, model_version=model, pricing_tier=option).status_code == 403
    generation_io.assert_not_called()


@pytest.mark.parametrize("aux,key", [("z_image", "text_to_skin_options"), ("flux_4b", "image_edit_to_skin_options")])
@pytest.mark.parametrize("free_price,pro_price", [(3, 2), (2, 2)])
@pytest.mark.parametrize("pro_only_model", [None, "skin", "aux"])
def test_combined_options_respect_pro_only_and_total_price(client, pricing_user, aux, key, free_price, pro_price, pro_only_model):
    model = generate.LEGACY_SKIN_MODEL_VERSION
    save_prices(client, pricing_user, model_name=model, credits=4, free_credits=4, is_pro=pro_only_model == "skin")
    save_prices(client, pricing_user, model_name=aux, credits=pro_price, free_credits=free_price, is_pro=pro_only_model == "aux")
    options = client.get("/skin/api/models").json()[key]
    assert len(options) == (1 if pro_only_model or free_price == pro_price else 2)
    for option in options:
        if pro_only_model:
            assert option["pricing_tier"] == "pro"
        assert option["model_version"] == model
        assert option["aux_model_version"] == aux
        params = {key: value for key, value in option.items() if key != "id"}
        quote = client.get("/skin/api/generation_credit_cost", params=params)
        assert quote.status_code == 200
        assert quote.json()["credits"] == 4 + (pro_price if option.get("pricing_tier") == "pro" else free_price)


@pytest.mark.parametrize("tier,option,cost", [("free", "standard", 15), ("pro-plus", "pro", 6)])
def test_combined_explicit_options_quote_and_charge(client, db, pricing_user, generation_io, tier, option, cost):
    set_membership(db, pricing_user, tier)
    model = generate.LEGACY_SKIN_MODEL_VERSION
    save_prices(client, pricing_user, model_name=model)
    save_prices(client, pricing_user, model_name="z_image", credits=2, free_credits=3)
    params = {"model_version": model, "aux_model_version": "z_image", "pricing_tier": option}
    assert client.get("/skin/api/generation_credit_cost", params=params).json()["credits"] == cost
    response = client.post("/skin/api/generate", data={
        **params, "prompt": "robot", "mode": "aigc_text_to_skin", "is_public": True,
        "public_license_consent": True,
    })
    assert response.status_code == 200, response.text
    db.refresh(pricing_user)
    assert pricing_user.credits == 100 - cost
    assert db.get(models.GenerationLog, response.json()["id"]).credits_charged == cost


@pytest.mark.parametrize("flag", ["is_pro", "under_maintenance"])
@pytest.mark.parametrize("restricted_model", [generate.LEGACY_SKIN_MODEL_VERSION, "z_image"])
def test_combined_standard_option_cannot_bypass_either_models_switches(
    client, pricing_user, generation_io, flag, restricted_model
):
    model = generate.LEGACY_SKIN_MODEL_VERSION
    save_prices(client, pricing_user, model_name=model)
    save_prices(client, pricing_user, model_name=restricted_model, **{flag: True})
    params = {"model_version": model, "aux_model_version": "z_image", "pricing_tier": "standard"}
    assert client.get("/skin/api/generation_credit_cost", params=params).json()[flag] is True
    response = client.post("/skin/api/generate", data={
        **params, "prompt": "robot", "mode": "aigc_text_to_skin", "is_public": True,
        "public_license_consent": True,
    })
    assert response.status_code == 403
    generation_io.assert_not_called()


@pytest.mark.parametrize("params,status", [
    ({"model_version": SKING_DDJ_V101C, "pricing_tier": "invalid"}, 422),
    ({"model_version": "unknown-model", "pricing_tier": "pro"}, 400),
    ({"model_version": SKING_DDJ_V101C, "aux_model_version": "z_image", "pricing_tier": "pro"}, 400),
])
def test_invalid_pricing_options_are_rejected(
    client, pricing_user, generation_io, params, status
):
    assert client.get("/skin/api/generation_credit_cost", params=params).status_code == status
    assert submit_image(client, **params).status_code == status
    generation_io.assert_not_called()


@pytest.mark.parametrize("option", ["standard", "pro"])
def test_open_tabs_can_submit_existing_options_after_prices_become_equal(
    client, db, pricing_user, generation_io, option
):
    set_membership(db, pricing_user, "pro-plus")
    save_prices(client, pricing_user)
    save_prices(client, pricing_user, free_credits=4)
    params = {"model_version": SKING_DDJ_V101C, "pricing_tier": option}
    quote = client.get("/skin/api/generation_credit_cost", params=params)
    assert quote.status_code == 200
    assert quote.json()["credits"] == 4
    response = submit_image(client, **params)
    assert response.status_code == 200, response.text
    db.refresh(pricing_user)
    assert pricing_user.credits == 96
    assert db.get(models.GenerationLog, response.json()["id"]).credits_charged == 4
