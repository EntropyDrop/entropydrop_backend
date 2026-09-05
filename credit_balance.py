"""Credit writers share a database row lock with hourly Space hosting."""
def lock_balance(db, user):
    # Refresh just the balance so pending subscription/profile edits are preserved.
    db.refresh(user, attribute_names=["credits"], with_for_update=True)
    return user.credits
