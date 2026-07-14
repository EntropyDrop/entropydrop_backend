import datetime
from datetime import timezone

import uuid
import random
import secrets
from sqlalchemy import Column, Integer, String, DateTime, Text, JSON, Boolean, Float, Index, UniqueConstraint, Date
from database import Base

def generate_base58_id(length=16):
    """Generate a 16-character Base58 random ID"""
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    return "".join(secrets.choice(alphabet) for _ in range(length))

class User(Base):
    """User model"""
    __tablename__ = "users"

    id = Column(String(16), primary_key=True, default=generate_base58_id, index=True)
    email = Column(String(100), unique=True, index=True, nullable=False)
    username = Column(String(100), nullable=True) # User's name from Google
    picture = Column(String(500), nullable=True)  # User's profile picture
    google_id = Column(String(100), unique=True, index=True, nullable=True) # Unique Google ID
    minecraft_skin_url = Column(String(500), nullable=True) # User's current Minecraft skin image URL

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc), onupdate=lambda: datetime.datetime.now(datetime.timezone.utc))
    priority_points = Column(Integer, default=0) # Priority points for generation queue
    credits = Column(Integer, default=0, nullable=False)
    last_login_date = Column(Date, nullable=True)
    terms_agreed = Column(Boolean, default=False) # Whether user agreed to terms of service
    
    # Subscription fields
    pro_expires_at = Column(DateTime(timezone=True), nullable=True)
    pro_level = Column(String(20), default="free") # free, pro-plus, pro-max
    paypal_subscription_id = Column(String(100), unique=True, index=True, nullable=True)
    paypal_subscription_status = Column(String(50), nullable=True) # ACTIVE, CANCELLED, etc
    
    @property
    def is_pro(self):
        if not self.pro_expires_at:
            return False
        if self.pro_level == "free":
            return False
        # Ensure comparison is done with aware datetimes
        now = datetime.datetime.now(datetime.timezone.utc)
        expires_at = self.pro_expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)
        return expires_at > now

    @property
    def is_admin(self):
        from config import settings
        admin_emails = [e.strip().lower() for e in settings.ADMIN_EMAILS.split(",") if e.strip()]
        return self.email and self.email.lower() in admin_emails

    @property
    def is_pro_active(self):
        return self.is_pro

class GenerationLog(Base):
    """Generation log model"""
    __tablename__ = "generation_logs"

    __table_args__ = (
        Index('idx_gen_log_public_created', 'is_public', 'created_at'),
        Index('idx_gen_log_name_trgm', 'name', postgresql_using='gin', postgresql_ops={'name': 'gin_trgm_ops'}),
    )

    id = Column(String(16), primary_key=True, default=generate_base58_id, index=True)
    prompt = Column(String(500), nullable=True)
    name = Column(String(100), nullable=True)
    mode = Column(String(50), nullable=False) # 'aigc_image_to_image', 'aigc_text_to_image', 'aigc_image_edit', 'human_edit', 'human_upload'
    source = Column(String(500), nullable=True) # Source image key in S3
    result = Column(String(500), nullable=True) # Generated image key, can be null (pending)
    edited_result = Column(String(500), nullable=True) # Intermediate edited image key
    edit_source_type = Column(String(50), nullable=True) # 'source' or 'intermediate'
    user_id = Column(String(16), index=True, nullable=True) # Associated user ID
    is_public = Column(Boolean, default=True, index=True)
    likes_count = Column(Integer, default=0)
    model_version = Column(String(50), nullable=True) # Large model version identifier
    parent = Column(String(16), index=True, nullable=True) # Parent log ID if derived from another item
    seed = Column(Integer, nullable=True)
    n_step = Column(Integer, nullable=True)
    guidance = Column(Float, nullable=True)
    status = Column(String(20), default="pending", index=True) # pending, processing, success, failed
    error_msg = Column(Text, nullable=True)
    is_deleted = Column(Boolean, default=False, index=True)
    is_pro = Column(Boolean, default=False, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc), index=True)

    @property
    def result_url(self):
        from s3_utils import get_s3_url
        return get_s3_url(self.result, self.is_public)

    @property
    def source_url(self):
        from s3_utils import get_s3_url
        return get_s3_url(self.source, self.is_public)

    @property
    def edited_image_url(self):
        from s3_utils import get_s3_url
        return get_s3_url(self.edited_result, self.is_public)


class Collection(Base):
    """Collection model (directory)"""
    __tablename__ = "collections"

    id = Column(String(16), primary_key=True, default=generate_base58_id, index=True)
    name = Column(String(100), nullable=False)
    user_id = Column(String(16), index=True, nullable=False)
    is_public = Column(Boolean, default=True, index=True)
    
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc), onupdate=lambda: datetime.datetime.now(datetime.timezone.utc))

class CollectionItem(Base):
    """Collection item model"""
    __tablename__ = "collection_items"

    id = Column(String(16), primary_key=True, default=generate_base58_id, index=True)
    collection_id = Column(String(16), index=True, nullable=False) # Associated collection.id
    type = Column(String(50), default="image") # 'image', 'model', etc.
    log_id = Column(String(16), index=True, nullable=True) # Associated generation_log.id
    data = Column(JSON, nullable=True) # Store image URL, metadata, etc.
    
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc))

class UserLike(Base):
    """User like model"""
    __tablename__ = "user_likes"

    __table_args__ = (
        Index('idx_user_like_user_log', 'user_id', 'log_id'),
    )

    id = Column(String(16), primary_key=True, default=generate_base58_id, index=True)
    user_id = Column(String(16), index=True, nullable=False)
    log_id = Column(String(16), index=True, nullable=False)
    
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc))

class ShippingAddress(Base):
    """Shipping address model"""
    __tablename__ = "shipping_addresses"

    id = Column(String(16), primary_key=True, default=generate_base58_id, index=True)
    user_id = Column(String(16), index=True, nullable=False)
    country = Column(String(100), nullable=False)
    phone = Column(String(50), nullable=False)  # Phone with country code prefix
    zip_code = Column(String(20), nullable=False)
    state = Column(String(100), nullable=False)
    city = Column(String(100), nullable=False)
    detail_address = Column(Text, nullable=False)
    is_default = Column(Boolean, default=False)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc), onupdate=lambda: datetime.datetime.now(datetime.timezone.utc))


class Order(Base):
    """Order model"""
    __tablename__ = "orders"

    id = Column(String(16), primary_key=True, default=generate_base58_id, index=True)
    user_id = Column(String(16), index=True, nullable=False)
    address_id = Column(String(16), index=True, nullable=True) # Shipping address ID (none for subscriptions)
    
    order_type = Column(String(20), default="print") # print, subscription

    # Order status: pending_payment, paid, shipping, completed, cancelled
    status = Column(String(50), default="pending_payment", index=True) 

    # Amounts and pricing
    price = Column(Float, nullable=False, default=60.0)
    shipping_fee = Column(Float, nullable=False, default=0.0)
    total_price = Column(Float, nullable=False, default=60.0)

    paid_at = Column(DateTime(timezone=True), nullable=True) # Timestamp of successful payment
    paypal_order_id = Column(String(100), unique=True, nullable=True, index=True)
    goods_status = Column(String(50), nullable=True, index=True) # shipping, preparing, printing

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc), index=True)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc), onupdate=lambda: datetime.datetime.now(datetime.timezone.utc))

class OrderItem(Base):
    """Order item model"""
    __tablename__ = "order_items"

    id = Column(String(16), primary_key=True, default=generate_base58_id, index=True)
    order_id = Column(String(16), index=True, nullable=False)
    skin_url = Column(String(255), nullable=True)
    model_type = Column(String(100), nullable=False, default="10cm Model V1")
    price = Column(Float, nullable=False, default=60.0)
    refer_log_id = Column(String(16), nullable=True, index=True)


    created_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc))


class ModelSalesLimit(Base):
    """Model sales limit configuration"""
    __tablename__ = "model_sales_limits"

    id = Column(String(16), primary_key=True, default=generate_base58_id, index=True)
    model_type = Column(String(100), unique=True, index=True, nullable=False)
    order_type = Column(String(20), nullable=False, default="print") # 'subscription' or 'print'
    stock = Column(Integer, default=100) # Remaining stock / quota
    price = Column(Float, nullable=False, default=0.0)

    
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc), onupdate=lambda: datetime.datetime.now(datetime.timezone.utc))


class UserFeedback(Base):
    """User feedback model for AIGC generation quality"""
    __tablename__ = "user_feedbacks"

    id = Column(String(16), primary_key=True, default=generate_base58_id, index=True)
    user_id = Column(String(16), index=True, nullable=True) # Optional (allows anonymous feedback)
    log_id = Column(String(16), index=True, nullable=False)
    is_good = Column(Boolean, nullable=False) # True for thumbs up, False for thumbs down
    
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc))


class ExternalLedgerEntry(Base):
    """Standardized external API ledger entry for public financial transparency."""
    __tablename__ = "external_ledger_entries"

    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_external_ledger_provider_external_id"),
        Index("idx_external_ledger_provider_posted", "provider", "posted_at"),
        Index("idx_external_ledger_type_posted", "entry_type", "posted_at"),
    )

    id = Column(String(220), primary_key=True, index=True)
    provider = Column(String(50), nullable=False, index=True) # paypal, aws, bank, etc.
    provider_account = Column(String(120), nullable=True)
    external_id = Column(String(180), nullable=False, index=True)
    entry_type = Column(String(50), nullable=False, index=True) # revenue, expense, refund, fee, transfer
    category = Column(String(50), nullable=True, index=True)

    amount = Column(Float, nullable=False)
    gross_amount = Column(Float, nullable=True)
    fee_amount = Column(Float, nullable=True)
    net_amount = Column(Float, nullable=True)
    currency = Column(String(10), default="USD")

    description = Column(String(500), nullable=True)
    public_description = Column(String(500), nullable=True)
    status = Column(String(50), default="posted", index=True) # posted, pending, estimated, failed
    source = Column(String(120), nullable=False)

    posted_at = Column(DateTime(timezone=True), nullable=False, index=True)
    period_start = Column(DateTime(timezone=True), nullable=True)
    period_end = Column(DateTime(timezone=True), nullable=True)
    synced_at = Column(DateTime(timezone=True), nullable=True)
    raw_payload = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc), onupdate=lambda: datetime.datetime.now(datetime.timezone.utc))


class LedgerSyncRun(Base):
    """Records each external ledger API synchronization run."""
    __tablename__ = "ledger_sync_runs"

    id = Column(String(16), primary_key=True, default=generate_base58_id, index=True)
    provider = Column(String(50), nullable=False, index=True)
    source = Column(String(120), nullable=False)
    status = Column(String(50), nullable=False, index=True) # ok, error, not_configured

    range_start = Column(DateTime(timezone=True), nullable=True)
    range_end = Column(DateTime(timezone=True), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=False, index=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    records_inserted = Column(Integer, default=0)
    records_updated = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    metadata_json = Column(JSON, nullable=True)


class ForumPost(Base):
    """Figure Forum Post model"""
    __tablename__ = "forum_posts"

    id = Column(String(16), primary_key=True, default=generate_base58_id, index=True)
    title = Column(String(100), nullable=False)
    content = Column(Text, nullable=False)
    category = Column(String(50), default="discussions", index=True) # 'discussions' or 'showcase'
    image = Column(String(500), nullable=True) # S3/CDN URL
    tags = Column(JSON, nullable=True) # JSON list of strings
    user_id = Column(String(16), index=True, nullable=False)
    likes_count = Column(Integer, default=0)
    views_count = Column(Integer, default=0)
    body_type = Column(String(50), nullable=True)
    multi_color_type = Column(String(50), nullable=True)
    
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc), index=True)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc), onupdate=lambda: datetime.datetime.now(datetime.timezone.utc))


class ForumComment(Base):
    """Figure Forum Comment model"""
    __tablename__ = "forum_comments"

    id = Column(String(16), primary_key=True, default=generate_base58_id, index=True)
    post_id = Column(String(16), index=True, nullable=False)
    parent_id = Column(String(16), index=True, nullable=True) # self-referential parent comment
    user_id = Column(String(16), index=True, nullable=False)
    content = Column(Text, nullable=False)
    
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc), index=True)


class ForumPostLike(Base):
    """Figure Forum Post Like model"""
    __tablename__ = "forum_post_likes"

    __table_args__ = (
        UniqueConstraint("user_id", "post_id", name="uq_forum_post_like_user_post"),
    )

    id = Column(String(16), primary_key=True, default=generate_base58_id, index=True)
    user_id = Column(String(16), index=True, nullable=False)
    post_id = Column(String(16), index=True, nullable=False)
    
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc))


class ForumNotification(Base):
    """Figure Forum Notification / Message model"""
    __tablename__ = "forum_notifications"

    id = Column(String(16), primary_key=True, default=generate_base58_id, index=True)
    user_id = Column(String(16), index=True, nullable=False) # recipient
    sender_id = Column(String(16), index=True, nullable=True) # initiator
    type = Column(String(50), nullable=False) # 'like', 'comment', 'reply'
    post_id = Column(String(16), index=True, nullable=True)
    comment_id = Column(String(16), index=True, nullable=True)
    is_read = Column(Boolean, default=False, index=True)
    
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc), index=True)


class ForumVideo(Base):
    """Figure Forum Youtube Video model"""
    __tablename__ = "forum_videos"

    id = Column(String(16), primary_key=True, default=generate_base58_id, index=True)
    youtube_id = Column(String(50), nullable=False, index=True)
    
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc), index=True)

class CreditLog(Base):
    """Credit transaction log model"""
    __tablename__ = "credit_logs"

    id = Column(String(16), primary_key=True, default=generate_base58_id, index=True)
    user_id = Column(String(16), index=True, nullable=False)
    amount = Column(Integer, nullable=False)
    action = Column(String(50), nullable=False)
    source = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc), index=True)







