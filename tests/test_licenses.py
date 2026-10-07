from types import SimpleNamespace

import licenses
import pytest


def user(user_id: str, is_pro: bool):
    return SimpleNamespace(id=user_id, is_pro_active=is_pro)


def parent(user_id: str, license_code: str):
    return SimpleNamespace(user_id=user_id, license=license_code, public_license=licenses.LICENSE_CC_BY_NC_4 if license_code != licenses.LICENSE_UNKNOWN else None)


def test_generation_license_uses_plan_without_parent():
    assert licenses.generated_license(user("free", False)) == licenses.LICENSE_CC_BY_NC_4
    assert licenses.generated_license(user("pro", True)) == licenses.LICENSE_COMMERCIAL


def test_generation_license_never_expands_parent_rights():
    pro = user("owner", True)
    assert licenses.generated_license(
        pro, parent("owner", licenses.LICENSE_UNKNOWN)
    ) == licenses.LICENSE_UNKNOWN
    assert licenses.generated_license(
        pro, parent("owner", licenses.LICENSE_CC_BY_NC_4)
    ) == licenses.LICENSE_CC_BY_NC_4
    assert licenses.generated_license(
        pro, parent("someone-else", licenses.LICENSE_COMMERCIAL)
    ) == licenses.LICENSE_CC_BY_NC_4


def test_owner_can_keep_commercial_rights_for_paid_regeneration_and_manual_edit():
    commercial_parent = parent("owner", licenses.LICENSE_COMMERCIAL)
    assert licenses.generated_license(
        user("owner", True), commercial_parent
    ) == licenses.LICENSE_COMMERCIAL
    assert licenses.edited_license(
        user("owner", False), commercial_parent
    ) == licenses.LICENSE_COMMERCIAL


def test_unknown_content_has_no_public_license():
    assert licenses.public_license_for(licenses.LICENSE_UNKNOWN, True) is None
    assert licenses.public_license_for(
        licenses.LICENSE_COMMERCIAL, True
    ) == licenses.LICENSE_CC_BY_NC_4
    assert licenses.public_license_for(licenses.LICENSE_CC_BY_NC_4, False) is None


@pytest.mark.parametrize("is_pro", [False, True])
def test_uploads_preserve_rights_without_platform_grants(is_pro):
    account = user("u", is_pro)
    assert licenses.saved_license(account, source_rights="original") == licenses.LICENSE_ORIGINAL
    assert licenses.saved_license(account, source_rights="external") == licenses.LICENSE_SOURCE
    with pytest.raises(ValueError):
        licenses.saved_license(account, requested_license=licenses.LICENSE_COMMERCIAL, is_upload=True)


def test_saved_license_rejects_commercial_upgrade_of_restricted_sources():
    pro = user("pro", True)
    for source_license in (licenses.LICENSE_CC_BY_NC_4, licenses.LICENSE_UNKNOWN):
        with pytest.raises(ValueError):
            licenses.saved_license(
                pro,
                parent("another-user", source_license),
                requested_license=licenses.LICENSE_COMMERCIAL,
            )


def test_saved_license_allows_permanent_owned_commercial_parent():
    assert licenses.saved_license(
        user("owner", False),
        parent("owner", licenses.LICENSE_COMMERCIAL),
        requested_license=licenses.LICENSE_COMMERCIAL,
    ) == licenses.LICENSE_COMMERCIAL

@pytest.mark.parametrize("is_pro", [False, True])
def test_external_reference_does_not_gain_rights_from_plan(is_pro):
    account = user("owner", is_pro)
    assert licenses.generated_license(account, source_rights="external") == licenses.LICENSE_SOURCE
    assert licenses.generated_license(account, parent("owner", licenses.LICENSE_SOURCE)) == licenses.LICENSE_SOURCE
    assert licenses.generated_license(account, parent("owner", licenses.LICENSE_ORIGINAL)) == (
        licenses.LICENSE_COMMERCIAL if is_pro else licenses.LICENSE_CC_BY_NC_4)


def test_original_rights_survive_manual_edit_without_pro():
    assert licenses.edited_license(user("owner", False), parent("owner", licenses.LICENSE_ORIGINAL)) == licenses.LICENSE_ORIGINAL
    assert licenses.edited_license(user("viewer", True), parent("owner", licenses.LICENSE_ORIGINAL)) == licenses.LICENSE_CC_BY_NC_4


def test_public_confirmation_does_not_overwrite_account_rights():
    assert licenses.public_license_for(licenses.LICENSE_ORIGINAL, True, consent=True) == licenses.LICENSE_CC_BY_NC_4
    assert licenses.public_license_for(licenses.LICENSE_SOURCE, False, consent=True) is None
    assert licenses.public_license_for(licenses.LICENSE_UNKNOWN, True, consent=True) == licenses.LICENSE_CC_BY_NC_4
