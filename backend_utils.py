from credit_balance import lock_balance
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



def get_daily_login_credits(user: models.User) -> int:
    """
    Fixed daily login credits:
    - Free user and pro-plus: 1 credit
    - Pro-max: 4 credits
    """
    if getattr(user, "is_pro", False) and getattr(user, "pro_level", None) == "pro-max":
        return 4
    return 1


def get_daily_free_credits():
    return 1



def get_generation_credit_cost():
    return 1


def get_model_credit_cost(model_name: str) -> int:
    try:
        val = redis_conn.get(f"config:model_price:{model_name}")
        if val is not None:
            return max(0, int(val.decode("utf-8")))
    except Exception as e:
        print(f"Failed to read model credit cost for {model_name} from redis: {e}")
    # Fallback: base models default to 0, skin models default to global cost
    from routers.generate import AVAILABlE_TEXT_TO_IMAGE_MODELS, AVAILABLE_IMAGE_EDIT_MODELS
    if model_name in AVAILABlE_TEXT_TO_IMAGE_MODELS or model_name in AVAILABLE_IMAGE_EDIT_MODELS:
        return 0
    return get_generation_credit_cost()


def is_model_pro_exclusive(model_name: str) -> bool:
    try:
        val = redis_conn.get(f"config:model_pro:{model_name}")
        if val is not None:
            return val == b"1"
    except Exception as e:
        print(f"Failed to check config:model_pro:{model_name} in Redis: {e}")
    return False


def is_model_under_maintenance(model_name: str) -> bool:
    try:
        val = redis_conn.get(f"config:model_maintenance:{model_name}")
        if val is not None:
            return val == b"1"
    except Exception as e:
        print(f"Failed to check config:model_maintenance:{model_name} in Redis: {e}")
    return False





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
        db.refresh(user, with_for_update=True)
        if user.last_login_date == today_utc:
            return
            
        # 1. Monthly Reward Check (All users receive 10 credits on first login of the month)
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
            monthly_awarded = 10
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

        # 2. Daily Reward Check (Free and pro-plus users get 1 credit, pro-max gets 4 credits)
        if user.last_login_date != today_utc:
            daily_awarded = get_daily_login_credits(user)
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



def award_subscription_credits(db: Session, user: models.User, pro_level: str,
                               subscription_id: str, is_webhook: bool, *, paid_at):
    """Grant once per verified payment, shared by activation and webhook."""
    paid_at = paid_at.astimezone(timezone.utc)
    key = f"subscription:{subscription_id}:{paid_at.isoformat()}"
    lock_balance(db, user)
    if db.query(models.CreditLog).filter(models.CreditLog.idempotency_key == key).first():
        return False
    # Adopt a historical grant from this paid period on first encounter. Old
    # rows have no payment identity; never give a second reward during rollout.
    legacy = db.query(models.CreditLog).filter(
        models.CreditLog.user_id == user.id,
        models.CreditLog.action == "subscription_grant",
        models.CreditLog.idempotency_key.is_(None),
        models.CreditLog.created_at >= paid_at,
        models.CreditLog.source.in_([
            f"Subscription Activation Grant: {subscription_id}",
            f"Subscription Webhook Grant: {subscription_id}",
        ]),
    ).order_by(models.CreditLog.created_at).first()
    if legacy:
        legacy.idempotency_key = key
        db.flush()
        return False

    monthly_credits = 200 if pro_level == "pro-max" else 80
    user.credits = (user.credits or 0) + monthly_credits
    
    # Record credit log
    source_str = f"Subscription Webhook Grant: {subscription_id}" if is_webhook else f"Subscription Activation Grant: {subscription_id}"
    credit_log = models.CreditLog(
        user_id=user.id,
        amount=monthly_credits,
        action="subscription_grant",
        source=source_str,
        idempotency_key=key,
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
