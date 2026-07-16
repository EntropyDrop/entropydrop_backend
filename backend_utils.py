from redis import Redis
from config import settings
import datetime
from datetime import timezone, timedelta
from sqlalchemy.orm import Session
import models

# Shared Redis connection
redis_conn = Redis.from_url(settings.REDIS_URL)



def is_text_to_skin_enabled() -> bool:
    try:
        val = redis_conn.get("config:text_to_skin_enabled")
        if val is None:
            return True
        return val == b"1"
    except Exception as e:
        print(f"Failed to check config:text_to_skin_enabled in Redis: {e}")
        return True

def is_image_to_skin_enabled() -> bool:
    try:
        val = redis_conn.get("config:image_to_skin_enabled")
        if val is None:
            return True
        return val == b"1"
    except Exception as e:
        print(f"Failed to check config:image_to_skin_enabled in Redis: {e}")
        return True

def is_image_edit_to_skin_enabled() -> bool:
    try:
        val = redis_conn.get("config:image_edit_to_skin_enabled")
        if val is None:
            return True
        return val == b"1"
    except Exception as e:
        print(f"Failed to check config:image_edit_to_skin_enabled in Redis: {e}")
        return True



def get_daily_free_credits():
    try:
        val = redis_conn.get("config:daily_free_credits")
        if val is not None:
            return int(val.decode("utf-8"))
    except Exception as e:
        print(f"Failed to read daily free credits config from redis: {e}")
    return 1


def get_generation_credit_cost():
    try:
        val = redis_conn.get("config:generation_credit_cost")
        if val is not None:
            return max(0, int(val.decode("utf-8")))
    except Exception as e:
        print(f"Failed to read generation credit cost config from redis: {e}")
    return 1



def award_daily_login_credits(db: Session, user: models.User):
    import time
    now_utc = datetime.datetime.now(timezone.utc)
    today_utc = now_utc.date()
    
    # 1. Fast path check: if the user in session already has today's date, skip
    if user.last_login_date == today_utc:
        return

    # 2. Acquire lock with retry (blocking lock)
    lock_key = f"lock:award_credits:{user.id}"
    acquired = False
    try:
        retries = 20  # Try for 2 seconds (20 * 0.1s)
        while retries > 0:
            if redis_conn.set(lock_key, "1", ex=10, nx=True):
                acquired = True
                break
            time.sleep(0.1)
            retries -= 1
    except Exception as e:
        print(f"Redis lock failed: {e}. Falling back to lock-free execution.")
        acquired = True

    if not acquired:
        print(f"Could not acquire lock for user {user.id} after retries")
        return

    try:
        # Refresh user from DB to get the latest state after releasing/acquiring the lock
        db.refresh(user)
        if user.last_login_date == today_utc:
            return
            
        # 1. Monthly Reward Check (All users, including Pro, receive 20 credits on first login of the month)
        month_start = now_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month_end = month_start + timedelta(days=32)
        month_end = month_end.replace(day=1) - timedelta(microseconds=1)
        
        already_claimed_monthly = db.query(models.CreditLog).filter(
            models.CreditLog.user_id == user.id,
            models.CreditLog.action == "monthly_login",
            models.CreditLog.created_at >= month_start,
            models.CreditLog.created_at <= month_end
        ).first() is not None

        if not already_claimed_monthly:
            monthly_awarded = 20
            user.credits = (user.credits or 0) + monthly_awarded
            
            # Record monthly credit log
            monthly_log = models.CreditLog(
                user_id=user.id,
                amount=monthly_awarded,
                action="monthly_login",
                source="Monthly Login Reward"
            )
            db.add(monthly_log)
            
            # Record system notification for mailbox for monthly reward
            monthly_notif = models.ForumNotification(
                user_id=user.id,
                sender_id=None,
                type="monthly_login",
                post_id=None,
                comment_id=str(monthly_awarded),
                is_read=False
            )
            db.add(monthly_notif)

        # 2. Daily Reward Check (All users get get_daily_free_credits() - defaults to 1 credit)
        if user.last_login_date != today_utc:
            daily_awarded = get_daily_free_credits()
            user.credits = (user.credits or 0) + daily_awarded
            user.last_login_date = today_utc
            
            # Record daily credit log
            daily_log = models.CreditLog(
                user_id=user.id,
                amount=daily_awarded,
                action="daily_login",
                source="Daily Login Reward"
            )
            db.add(daily_log)
            
            # Record system notification for mailbox for daily reward
            daily_notif = models.ForumNotification(
                user_id=user.id,
                sender_id=None,
                type="daily_login",
                post_id=None,
                comment_id=str(daily_awarded),
                is_read=False
            )
            db.add(daily_notif)

        db.commit()
        db.refresh(user)
    finally:
        try:
            redis_conn.delete(lock_key)
        except Exception as e:
            print(f"Failed to release Redis lock: {e}")



def award_subscription_credits(db: Session, user: models.User, pro_level: str, subscription_id: str, is_webhook: bool):
    """
    Award monthly credits immediately to Pro users upon successful subscription activation or renewal payment.
    Includes a 3-day deduplication window matching the subscription ID to prevent duplicate credits from concurrent activation and webhooks.
    """
    import datetime
    from datetime import timezone
    three_days_ago = datetime.datetime.now(timezone.utc) - timedelta(days=3)
    
    existing_grant = db.query(models.CreditLog).filter(
        models.CreditLog.user_id == user.id,
        models.CreditLog.action == "subscription_grant",
        models.CreditLog.created_at >= three_days_ago,
        models.CreditLog.source.like(f"%{subscription_id}%")
    ).first()
    
    if existing_grant:
        print(f"Skipping subscription credits grant for user {user.id}: already awarded for subscription {subscription_id} in the last 3 days (Reference: {existing_grant.source})")
        return
        
    monthly_credits = 180 if pro_level == "pro-max" else 60
    user.credits = (user.credits or 0) + monthly_credits
    
    # Record credit log
    source_str = f"Subscription Webhook Grant: {subscription_id}" if is_webhook else f"Subscription Activation Grant: {subscription_id}"
    credit_log = models.CreditLog(
        user_id=user.id,
        amount=monthly_credits,
        action="subscription_grant",
        source=source_str
    )
    db.add(credit_log)
    
    # Record system notification for mailbox
    notif = models.ForumNotification(
        user_id=user.id,
        sender_id=None,
        type="subscription_grant",
        post_id=None,
        comment_id=str(monthly_credits),
        is_read=False
    )
    db.add(notif)
    print(f"Awarded {monthly_credits} subscription credits to user {user.id} ({pro_level}) for subscription {subscription_id}")


def paginate_response(items: list, total: int, page: int, page_size: int, **kwargs):
    """
    Standardize pagination response structure.
    """
    res = {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size if page_size > 0 else 1
    }
    res.update(kwargs)
    return res
