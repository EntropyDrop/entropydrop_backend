"""compensate_pro_user_credits

Revision ID: 76e0bf44aa23
Revises: c21c0794457a
Create Date: 2026-07-09 17:55:41.511015

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '76e0bf44aa23'
down_revision: Union[str, None] = 'c21c0794457a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from sqlalchemy.orm import Session
    from sqlalchemy import func
    import datetime
    from datetime import timezone, timedelta
    import models
    
    bind = op.get_bind()
    db = Session(bind=bind)
    
    try:
        # 1. Update any old monthly_login logs for Pro users (where amount was 60 or 180)
        # to amount = 20, and insert corresponding subscription_grant of 60/180.
        old_pro_logs = db.query(models.CreditLog).filter(
            models.CreditLog.action == "monthly_login",
            models.CreditLog.amount.in_([60, 180])
        ).all()
        
        print(f"Found {len(old_pro_logs)} old monthly_login logs to split/convert.")
        for log in old_pro_logs:
            old_amount = log.amount
            pro_level = "pro-max" if old_amount == 180 else "pro-plus"
            sub_credits = 180 if pro_level == "pro-max" else 60
            
            print(f"Splitting log {log.id} for user {log.user_id}: amount {old_amount} -> 20 (monthly_login) + {sub_credits} (subscription_grant)")
            # Update old log to 20
            log.amount = 20
            log.source = "Monthly Login Reward"
            
            # Insert new subscription_grant log
            new_log = models.CreditLog(
                user_id=log.user_id,
                amount=sub_credits,
                action="subscription_grant",
                source="Subscription Migration Grant (Split)",
                created_at=log.created_at
            )
            db.add(new_log)
            
            # Find and update corresponding monthly_login ForumNotification to "20"
            time_lower = log.created_at - timedelta(seconds=5)
            time_upper = log.created_at + timedelta(seconds=5)
            notifs = db.query(models.ForumNotification).filter(
                models.ForumNotification.user_id == log.user_id,
                models.ForumNotification.type == "monthly_login",
                models.ForumNotification.comment_id == str(old_amount),
                models.ForumNotification.created_at >= time_lower,
                models.ForumNotification.created_at <= time_upper
            ).all()
            for notif in notifs:
                notif.comment_id = "20"
                
            # Create a corresponding subscription_grant ForumNotification
            new_notif = models.ForumNotification(
                user_id=log.user_id,
                sender_id=None,
                type="subscription_grant",
                post_id=None,
                comment_id=str(sub_credits),
                is_read=False,
                created_at=log.created_at
            )
            db.add(new_notif)

        db.flush()

        # 2. For all active Pro users, check if they have any subscription_grant logs in their current billing cycle.
        # If not, compensate/award it.
        now = datetime.datetime.now(timezone.utc)
        pro_users = db.query(models.User).filter(
            models.User.pro_expires_at > now,
            models.User.pro_level.in_(["pro-plus", "pro-max"])
        ).all()
        
        print(f"Found {len(pro_users)} active Pro users. Checking compensation...")
        compensations_awarded = 0
        
        for u in pro_users:
            # Current billing cycle starts roughly at pro_expires_at - 31 days
            cycle_start = u.pro_expires_at - timedelta(days=31)
            
            existing_grant = db.query(models.CreditLog).filter(
                models.CreditLog.user_id == u.id,
                models.CreditLog.action == "subscription_grant",
                models.CreditLog.created_at >= cycle_start
            ).first()
            
            if not existing_grant:
                # Award compensation credits
                sub_credits = 180 if u.pro_level == "pro-max" else 60
                print(f"Awarding compensation: user {u.username or u.email} (ID: {u.id}) gets {sub_credits} credits for active {u.pro_level} subscription.")
                
                comp_log = models.CreditLog(
                    user_id=u.id,
                    amount=sub_credits,
                    action="subscription_grant",
                    source="Subscription Compensation Grant",
                    created_at=now
                )
                db.add(comp_log)
                
                comp_notif = models.ForumNotification(
                    user_id=u.id,
                    sender_id=None,
                    type="subscription_grant",
                    post_id=None,
                    comment_id=str(sub_credits),
                    is_read=False,
                    created_at=now
                )
                db.add(comp_notif)
                compensations_awarded += 1

        if compensations_awarded > 0:
            db.flush()
            print(f"Awarded subscription compensation to {compensations_awarded} users.")

        # 3. Recalculate and reset all users' credits based on their remaining logs
        users = db.query(models.User).all()
        print(f"Recalculating credits from logs for {len(users)} users...")
        users_updated = 0
        for u in users:
            log_sum = db.query(func.sum(models.CreditLog.amount)).filter(
                models.CreditLog.user_id == u.id
            ).scalar()
            
            target_credits = max(0, int(log_sum)) if log_sum is not None else 0
            
            if u.credits != target_credits:
                print(f"User: {u.username or u.email} (ID: {u.id}) credits: {u.credits} -> {target_credits}")
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
