"""All debits respect cloud Space holds while sharing the user's row lock."""
def available_balance(db, user):
    from models import SpaceCreditReservation
    held = db.query(SpaceCreditReservation).filter_by(user_id=user.id, state="reserved").count()
    return max(0, int(user.credits or 0) - held)


def lock_balance(db, user):
    # Refresh just the balance so pending subscription/profile edits are preserved.
    db.refresh(user, attribute_names=["credits"], with_for_update=True)
    return available_balance(db, user)
