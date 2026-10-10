"""Versioned acceptance of the website's separate 3D printing service terms.

Keep this version aligned with the frontend's constants/figurePrintTerms.ts.
The corresponding bilingual text is archived in the frontend docs/legal folder.
"""
from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from models import User

FIGURE_PRINT_TERMS_VERSION = "1.0"
FIGURE_PRINT_TERMS_EFFECTIVE_DATE = "2026-10-09"


def acceptance_status(user: User) -> dict:
    accepted_at = user.figure_print_terms_accepted_at
    # SQLite tests may drop tzinfo; all stored acceptance times are UTC.
    if accepted_at is not None and accepted_at.tzinfo is None:
        accepted_at = accepted_at.replace(tzinfo=timezone.utc)
    return {
        "required_version": FIGURE_PRINT_TERMS_VERSION,
        "effective_date": FIGURE_PRINT_TERMS_EFFECTIVE_DATE,
        "accepted_version": user.figure_print_terms_version,
        "accepted_at": accepted_at,
    }


def record_acceptance(db: Session, user: User) -> dict:
    # A conditional update makes duplicate/concurrent requests idempotent and
    # retains the first acceptance time for the current version.
    db.query(User).filter(
        User.id == user.id,
        or_(
            User.figure_print_terms_version.is_(None),
            User.figure_print_terms_version != FIGURE_PRINT_TERMS_VERSION,
            User.figure_print_terms_accepted_at.is_(None),
        ),
    ).update({
        User.figure_print_terms_version: FIGURE_PRINT_TERMS_VERSION,
        User.figure_print_terms_accepted_at: datetime.now(timezone.utc),
    }, synchronize_session=False)
    db.commit()
    db.refresh(user)
    return acceptance_status(user)
