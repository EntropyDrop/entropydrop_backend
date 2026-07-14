"""reset_credits_by_logs

Revision ID: c21c0794457a
Revises: a40782d0bec5
Create Date: 2026-07-09 17:22:50.450052

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c21c0794457a'
down_revision: Union[str, None] = 'a40782d0bec5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from sqlalchemy.orm import Session
    from sqlalchemy import func
    import models
    
    bind = op.get_bind()
    db = Session(bind=bind)
    
    try:
        users = db.query(models.User).all()
        print(f"Loaded {len(users)} users. Recalculating credits from logs...")
        
        users_updated = 0
        for u in users:
            log_sum = db.query(func.sum(models.CreditLog.amount)).filter(
                models.CreditLog.user_id == u.id
            ).scalar()
            
            target_credits = max(0, int(log_sum)) if log_sum is not None else 0
            
            if u.credits != target_credits:
                print(f"User: {u.username or u.email} (ID: {u.id})")
                print(f"  - Credits mismatch: {u.credits} -> {target_credits}")
                u.credits = target_credits
                users_updated += 1
                
        if users_updated > 0:
            db.flush()
            print(f"Successfully recalculated and updated credits for {users_updated} users.")
        else:
            print("All users are already consistent with their logs.")
            
    except Exception as e:
        db.rollback()
        raise e


def downgrade() -> None:
    pass
