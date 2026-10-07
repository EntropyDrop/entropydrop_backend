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
# These describe pre-existing rights, not grants from EntropyDrop.
LICENSE_ORIGINAL = "original-work"
LICENSE_SOURCE = "source-license"
LICENSE_VERSION = 2
SOURCE_RIGHTS = {"original", "external"}


def public_license_for(license_code: str, is_public: bool, *, consent=False) -> str | None:
    if is_public and (license_code != LICENSE_UNKNOWN or consent):
        return LICENSE_CC_BY_NC_4
    return None


def source_license(current_user, parent_log) -> str:
    """A source's account rights belong to its owner, not its collectors."""
    code = parent_log.license or LICENSE_UNKNOWN
    if parent_log.user_id == current_user.id:
        return code if code in {
            LICENSE_COMMERCIAL, LICENSE_CC_BY_NC_4, LICENSE_ORIGINAL, LICENSE_SOURCE
        } else LICENSE_UNKNOWN
    return (
        LICENSE_CC_BY_NC_4
        if getattr(parent_log, "public_license", None) == LICENSE_CC_BY_NC_4
        else LICENSE_UNKNOWN
    )


def generated_license(current_user, parent_log=None, source_rights=None) -> str:
    """Resolve a new AI generation without expanding its source rights."""
    if source_rights not in {None, *SOURCE_RIGHTS}:
        raise ValueError("Unsupported source rights")
    if parent_log is not None:
        inherited = source_license(current_user, parent_log)
        if inherited not in {LICENSE_COMMERCIAL, LICENSE_ORIGINAL}:
            return inherited
    elif source_rights == "external":
        return LICENSE_SOURCE
    return LICENSE_COMMERCIAL if current_user.is_pro_active else LICENSE_CC_BY_NC_4


def edited_license(current_user, parent_log=None, source_rights=None) -> str:
    """Manual creation/import preserves existing rights regardless of plan."""
    if source_rights not in {None, *SOURCE_RIGHTS}:
        raise ValueError("Unsupported source rights")
    if parent_log is not None:
        return source_license(current_user, parent_log)
    return LICENSE_ORIGINAL if source_rights == "original" else LICENSE_SOURCE


def saved_license(current_user, parent_log=None, requested_license=None, is_upload=False, source_rights=None) -> str:
    """Old clients may echo an inherited value but cannot choose new rights."""
    inherited = edited_license(current_user, parent_log, source_rights)
    if requested_license is not None and requested_license != inherited:
        raise ValueError("Source permissions are inherited; uploads do not grant a commercial license")
    return inherited


def license_preview(current_user, operation, parent_log=None, source_rights=None, is_public=True):
    if parent_log is not None and (operation == "generate" or not parent_log.is_public):
        is_public = parent_log.is_public
    code = (generated_license(current_user, parent_log, source_rights)
            if operation == "generate"
            else edited_license(current_user, parent_log, source_rights))
    return {
        "code": code,
        "public_license": public_license_for(code, is_public, consent=True),
        "is_pro": current_user.is_pro_active,
        "parent_is_private": bool(parent_log and not parent_log.is_public),
    }


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
