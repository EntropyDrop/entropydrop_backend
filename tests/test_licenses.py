from types import SimpleNamespace

import licenses
import pytest


def user(user_id: str, is_pro: bool):
    return SimpleNamespace(id=user_id, is_pro_active=is_pro)


def parent(user_id: str, license_code: str):
    return SimpleNamespace(user_id=user_id, license=license_code)


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


def test_pro_can_select_commercial_for_new_independent_work():
    assert licenses.saved_license(
        user("pro", True),
        requested_license=licenses.LICENSE_COMMERCIAL,
        is_upload=True,
    ) == licenses.LICENSE_COMMERCIAL
    with pytest.raises(ValueError):
        licenses.saved_license(
            user("free", False),
            requested_license=licenses.LICENSE_COMMERCIAL,
            is_upload=True,
        )


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
