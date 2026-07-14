"""clean_duplicate_login_rewards

Revision ID: a40782d0bec5
Revises: ae9ec0759716
Create Date: 2026-07-09 17:02:55.855149

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a40782d0bec5'
down_revision: Union[str, None] = 'ae9ec0759716'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Get database session
    from sqlalchemy.orm import Session
    import models
    from datetime import timedelta
    
    bind = op.get_bind()
    db = Session(bind=bind)
    
    try:
        users = db.query(models.User).all()
        print(f"Scanning duplicate login rewards for {len(users)} users...")
        
        total_logs_deleted = 0
        total_notifs_deleted = 0
        total_credits_recovered = 0
        users_corrected = 0

        for user in users:
            # 1. Daily Login duplicates
            daily_logs = db.query(models.CreditLog).filter(
                models.CreditLog.user_id == user.id,
                models.CreditLog.action == "daily_login"
            ).order_by(models.CreditLog.created_at.asc()).all()

            # Group daily logs by date
            daily_by_date = {}
            for log in daily_logs:
                log_date = log.created_at.date()
                if log_date not in daily_by_date:
                    daily_by_date[log_date] = []
                daily_by_date[log_date].append(log)

            # Find duplicates for daily
            user_recovered_credits = 0
            user_logs_deleted = 0
            user_notifs_deleted = 0

            for log_date, logs in daily_by_date.items():
                if len(logs) > 1:
                    keep_log = logs[0]
                    duplicate_logs = logs[1:]
                    for dup in duplicate_logs:
                        user_recovered_credits += dup.amount
                        user_logs_deleted += 1
                        
                        # Find corresponding notifications for daily login
                        time_lower = dup.created_at - timedelta(seconds=5)
                        time_upper = dup.created_at + timedelta(seconds=5)
                        notifs = db.query(models.ForumNotification).filter(
                            models.ForumNotification.user_id == user.id,
                            models.ForumNotification.type == "daily_login",
                            models.ForumNotification.created_at >= time_lower,
                            models.ForumNotification.created_at <= time_upper
                        ).all()
                        for notif in notifs:
                            db.delete(notif)
                            user_notifs_deleted += 1
                        
                        db.delete(dup)

            # 2. Monthly Login duplicates
            monthly_logs = db.query(models.CreditLog).filter(
                models.CreditLog.user_id == user.id,
                models.CreditLog.action == "monthly_login"
            ).order_by(models.CreditLog.created_at.asc()).all()

            # Group monthly logs by month (year, month)
            monthly_by_month = {}
            for log in monthly_logs:
                month_key = (log.created_at.year, log.created_at.month)
                if month_key not in monthly_by_month:
                    monthly_by_month[month_key] = []
                monthly_by_month[month_key].append(log)

            # Find duplicates for monthly
            for month_key, logs in monthly_by_month.items():
                if len(logs) > 1:
                    keep_log = logs[0]
                    duplicate_logs = logs[1:]
                    for dup in duplicate_logs:
                        user_recovered_credits += dup.amount
                        user_logs_deleted += 1

                        # Find corresponding notifications for monthly login
                        time_lower = dup.created_at - timedelta(seconds=5)
                        time_upper = dup.created_at + timedelta(seconds=5)
                        notifs = db.query(models.ForumNotification).filter(
                            models.ForumNotification.user_id == user.id,
                            models.ForumNotification.type == "monthly_login",
                            models.ForumNotification.created_at >= time_lower,
                            models.ForumNotification.created_at <= time_upper
                        ).all()
                        for notif in notifs:
                            db.delete(notif)
                            user_notifs_deleted += 1

                        db.delete(dup)

            if user_recovered_credits > 0:
                old_credits = user.credits or 0
                new_credits = max(0, old_credits - user_recovered_credits)
                print(f"User: {user.username or user.email} (ID: {user.id})")
                print(f"  - Old Credits: {old_credits}, Recovered: {user_recovered_credits}, New Credits: {new_credits}")
                print(f"  - Duplicate Logs Deleted: {user_logs_deleted}")
                print(f"  - Duplicate Notifications Deleted: {user_notifs_deleted}")
                user.credits = new_credits
                users_corrected += 1
                total_logs_deleted += user_logs_deleted
                total_notifs_deleted += user_notifs_deleted
                total_credits_recovered += user_recovered_credits

        print("\n" + "="*50)
        print(f"Summary (Alembic Data Migration):")
        print(f"  - Users Corrected: {users_corrected}")
        print(f"  - Total Logs Deleted: {total_logs_deleted}")
        print(f"  - Total Notifications Deleted: {total_notifs_deleted}")
        print(f"  - Total Credits Recovered: {total_credits_recovered}")
        print("="*50)

        db.flush()
    except Exception as e:
        db.rollback()
        raise e


def downgrade() -> None:
    pass
