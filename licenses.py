"""License policy helpers for generated and uploaded skins.

The stored ``license`` value is the creator/uploader-facing license snapshot.
``public_license`` is deliberately separate: visibility never grants commercial
rights, and a public commercially licensed generation is still offered to other
users only under CC BY-NC 4.0.
"""

import datetime


LICENSE_UNKNOWN = "unknown"
LICENSE_CC_BY_NC_4 = "cc-by-nc-4.0"
LICENSE_COMMERCIAL = "entropydrop-commercial-1.0"
LICENSE_VERSION = 1


def public_license_for(license_code: str, is_public: bool) -> str | None:
    if is_public and license_code != LICENSE_UNKNOWN:
        return LICENSE_CC_BY_NC_4
    return None


def generated_license(current_user, parent_log=None) -> str:
    """Resolve a new AI generation without expanding its source rights."""
    if parent_log is None:
        return (
            LICENSE_COMMERCIAL
            if current_user.is_pro_active
            else LICENSE_CC_BY_NC_4
        )

    parent_license = parent_log.license or LICENSE_UNKNOWN
    if parent_license == LICENSE_UNKNOWN:
        return LICENSE_UNKNOWN
    if parent_license == LICENSE_CC_BY_NC_4:
        return LICENSE_CC_BY_NC_4
    if parent_license == LICENSE_COMMERCIAL:
        owns_parent_commercial_license = parent_log.user_id == current_user.id
        if owns_parent_commercial_license and current_user.is_pro_active:
            return LICENSE_COMMERCIAL
        return LICENSE_CC_BY_NC_4
    return LICENSE_UNKNOWN


def edited_license(current_user, parent_log=None) -> str:
    """Resolve a manual edit; an edit never grants broader source rights."""
    if parent_log is None:
        return LICENSE_CC_BY_NC_4

    parent_license = parent_log.license or LICENSE_UNKNOWN
    if parent_license == LICENSE_COMMERCIAL:
        return (
            LICENSE_COMMERCIAL
            if parent_log.user_id == current_user.id
            else LICENSE_CC_BY_NC_4
        )
    if parent_license == LICENSE_CC_BY_NC_4:
        return LICENSE_CC_BY_NC_4
    return LICENSE_UNKNOWN


def saved_license(current_user, parent_log=None, requested_license=None, is_upload=False) -> str:
    """Resolve an explicitly selected license without expanding source rights.

    A new independent upload/edit may select the commercial license only while
    the saving account has an active Pro entitlement.  A commercial license
    already attached to the account's own parent remains usable permanently.
    """
    if requested_license not in {None, LICENSE_CC_BY_NC_4, LICENSE_COMMERCIAL}:
        raise ValueError("Unsupported requested license")

    inherited_license = (
        LICENSE_CC_BY_NC_4
        if is_upload and parent_log is None
        else edited_license(current_user, parent_log)
    )

    if requested_license is None:
        return inherited_license

    if inherited_license == LICENSE_UNKNOWN:
        if requested_license == LICENSE_COMMERCIAL:
            raise ValueError("Unknown source rights cannot be upgraded to a commercial license")
        return LICENSE_UNKNOWN

    if requested_license == LICENSE_CC_BY_NC_4:
        return LICENSE_CC_BY_NC_4

    owns_parent_commercial_license = (
        parent_log is not None
        and parent_log.license == LICENSE_COMMERCIAL
        and parent_log.user_id == current_user.id
    )
    is_new_pro_work = parent_log is None and current_user.is_pro_active
    if owns_parent_commercial_license or is_new_pro_work:
        return LICENSE_COMMERCIAL

    raise ValueError("The source license does not permit commercial use")


def license_payload(log) -> dict:
    granted_at = log.license_granted_at
    if granted_at is not None:
        if granted_at.tzinfo is None:
            granted_at = granted_at.replace(tzinfo=datetime.timezone.utc)
        granted_at_value = granted_at.isoformat().replace("+00:00", "Z")
    else:
        granted_at_value = None

    code = log.license or LICENSE_UNKNOWN
    return {
        "code": code,
        "public_license": log.public_license,
        "version": log.license_version or LICENSE_VERSION,
        "granted_at": granted_at_value,
        "commercial_licensee_user_id": (
            log.user_id if code == LICENSE_COMMERCIAL else None
        ),
    }
