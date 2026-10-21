import datetime
from datetime import timezone

import uuid
import random
import secrets
from sqlalchemy import Column, Integer, BigInteger, SmallInteger, String, DateTime, Text, JSON, Boolean, Float, Numeric, Index, UniqueConstraint, CheckConstraint, Date, ForeignKey, Uuid, LargeBinary
from database import Base

from ids import generate_base58_id

class User(Base):
    """User model"""
    __tablename__ = "users"

    id = Column(String(16), primary_key=True, default=generate_base58_id, index=True)
    email = Column(String(100), unique=True, index=True, nullable=False)
    username = Column(String(100), nullable=True) # User's name from Google
    picture = Column(String(500), nullable=True)  # User's profile picture
    google_id = Column(String(100), unique=True, index=True, nullable=True) # Unique Google ID
    skin_url = Column(String(500), nullable=True) # User's current Minecraft skin image URL
    skin_type = Column(String(20), default="strong", nullable=False, server_default="strong") # User's Minecraft skin type: 'strong' (Steve) or 'slim' (Alex)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc), onupdate=lambda: datetime.datetime.now(datetime.timezone.utc))
    priority_points = Column(Integer, default=0) # Priority points for generation queue
    credits = Column(Integer, default=0, nullable=False)
    last_login_date = Column(Date, nullable=True)
    terms_agreed = Column(Boolean, default=False) # Whether user agreed to terms of service
    is_creator = Column(Boolean, nullable=False, default=False, server_default="0")
    figure_print_terms_version = Column(String(32), nullable=True)
    figure_print_terms_accepted_at = Column(DateTime(timezone=True), nullable=True)
    
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


class AuthSession(Base):
    """Server-revocable browser login session; only a token hash is persisted."""
    __tablename__ = "auth_sessions"

    id = Column(String(32), primary_key=True, default=lambda: secrets.token_urlsafe(24)[:32])
    user_id = Column(
        String(16),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash = Column(LargeBinary(32), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
    last_used_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    absolute_expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_auth_sessions_user_expires", "user_id", "expires_at"),
    )


class SpaceApiKey(Base):
    """Long-lived, revocable account credential for the public Space API."""
    __tablename__ = "space_api_keys"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_space_api_key_token_hash"),
        Index("ix_space_api_keys_owner", "user_id", "created_at"),
    )

    id = Column(String(16), primary_key=True, default=generate_base58_id)
    user_id = Column(String(16), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(80), nullable=False)
    key_prefix = Column(String(40), nullable=False)
    token_hash = Column(LargeBinary(32), nullable=False)
    scopes = Column(JSON, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
        nullable=False,
    )
    last_used_at = Column(DateTime(timezone=True), nullable=True)


class SpaceAgentAuthorization(Base):
    """Short-lived pairing grant. Neither device codes nor API-key secrets are stored."""
    __tablename__ = "space_agent_authorizations"
    device_hash = Column(LargeBinary(32), primary_key=True)
    user_code_hash = Column(LargeBinary(32), nullable=False, unique=True)
    name = Column(String(80), nullable=False)
    status = Column(String(16), nullable=False, default="pending")
    user_id = Column(String(16), ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    # Retain the ID after revocation so a token retry cannot recreate the key.
    api_key_id = Column(String(16), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    next_poll_at = Column(DateTime(timezone=True), nullable=False)
    poll_interval = Column(Integer, nullable=False, default=5)


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
    mode = Column(String(50), nullable=False) # 'aigc_image_to_skin', 'aigc_text_to_skin', 'aigc_image_edit_to_skin', 'human_edit', 'human_upload'
    source = Column(String(500), nullable=True) # Source image key in S3
    result = Column(String(500), nullable=True) # Generated image key, can be null (pending)
    edited_result = Column(String(500), nullable=True) # Intermediate edited image key
    image_to_skin_edited_result = Column(String(500), nullable=True) # Image-to-skin pipeline intermediate key
    withdrawal = Column(JSON(none_as_null=True), nullable=True) # Durable, retryable storage/CDN withdrawal manifest
    user_id = Column(String(16), index=True, nullable=True) # Associated user ID
    is_public = Column(Boolean, default=True, index=True)
    likes_count = Column(Integer, default=0)
    model_version = Column(String(50), nullable=True) # Large model version identifier
    aux_model_version = Column(String(50), nullable=True) # Large model auxiliary/base version identifier
    provider_task_id = Column(String(100), nullable=True, index=True) # External stage-1 task ID
    provider_submission_state = Column(
        String(20),
        nullable=True,
        index=True,
    ) # in_flight, accepted, unknown
    parent = Column(String(16), index=True, nullable=True) # Parent log ID if derived from another item
    recoverable = Column(Boolean, default=False, index=True, server_default="0")
    credits_charged = Column(Integer, default=0, nullable=False, server_default="0")
    credits_refunded = Column(Boolean, default=False, nullable=False, server_default="0")
    seed = Column(Integer, nullable=True)
    n_step = Column(Integer, nullable=True)
    guidance = Column(Float, nullable=True)
    status = Column(String(20), default="pending", index=True) # pending, processing, success, failed
    error_msg = Column(Text, nullable=True)
    is_deleted = Column(Boolean, default=False, index=True)
    is_pro = Column(Boolean, default=False, index=True)
    # Queue entitlement can be upgraded after submission; is_pro and license
    # remain snapshots of the account/rights at creation time.
    pro_priority = Column(Boolean, default=False, nullable=False, server_default="0")
    # Immutable license snapshot assigned when the log is created. Visibility
    # and the public reuse license are intentionally stored separately.
    license = Column(String(64), nullable=False, default="unknown", server_default="unknown", index=True)
    public_license = Column(String(64), nullable=True)
    license_version = Column(Integer, nullable=False, default=1, server_default="1")
    license_granted_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc), index=True)

    @property
    def result_url(self):
        from s3_utils import get_s3_url
        return None if self.withdrawal or self.is_deleted else get_s3_url(self.result, self.is_public)

    @property
    def source_url(self):
        from s3_utils import get_s3_url
        return None if self.withdrawal or self.is_deleted else get_s3_url(self.source, self.is_public)

    @property
    def edited_image_url(self):
        from s3_utils import get_s3_url
        return None if self.withdrawal or self.is_deleted else get_s3_url(self.edited_result, self.is_public)

    @property
    def image_to_skin_edited_image_url(self):
        from s3_utils import get_s3_url
        return None if self.withdrawal or self.is_deleted else get_s3_url(self.image_to_skin_edited_result, self.is_public)


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

    recipient_name = Column(String(300), nullable=False, default="", server_default="")

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
    address_snapshot = Column(JSON, nullable=True)
    shipping_source = Column(String(20), nullable=False, default="address_book", server_default="address_book")
    checkout_mode = Column(String(20), nullable=False, default="cart", server_default="cart")
    inventory_reserved = Column(Boolean, nullable=False, default=False, server_default="0")
    inventory_reserved_until = Column(DateTime(timezone=True), nullable=True)
    capture_started = Column(Boolean, nullable=False, default=False, server_default="0")
    
    order_type = Column(String(20), default="print") # print, subscription

    # Order status: pending_payment, paid, shipping, completed, cancelled
    status = Column(String(50), default="pending_payment", index=True) 

    # Amounts and pricing
    price = Column(Float, nullable=False, default=60.0)
    shipping_fee = Column(Float, nullable=False, default=0.0)
    total_price = Column(Float, nullable=False, default=60.0)
    creator_code = Column(String(32), nullable=True, index=True)
    creator_user_id = Column(String(16), ForeignKey("users.id"), nullable=True, index=True)
    global_discount_bps = Column(Integer, nullable=False, default=0, server_default="0")
    global_discount_version = Column(Integer, nullable=False, default=0, server_default="0")
    creator_snapshot = Column(JSON, nullable=True)
    discount_amount = Column(Float, nullable=False, default=0.0, server_default="0")
    creator_commission = Column(Float, nullable=False, default=0.0, server_default="0")
    creator_commission_cents = Column(Integer, nullable=False, default=0, server_default="0")

    paid_at = Column(DateTime(timezone=True), nullable=True) # Timestamp of successful payment
    paypal_order_id = Column(String(100), unique=True, nullable=True, index=True)
    goods_status = Column(String(50), nullable=True, index=True)
    figure_review_status = Column(String(20), nullable=True, index=True)
    figure_review_reason = Column(Text, nullable=True)
    figure_reviewed_by = Column(String(16), nullable=True)
    figure_reviewed_at = Column(DateTime(timezone=True), nullable=True)
    paypal_capture_id = Column(String(100), nullable=True)
    paypal_refund_id = Column(String(100), nullable=True, unique=True)
    refund_status = Column(String(30), nullable=True)
    refund_error = Column(Text, nullable=True)
    refund_requested_at = Column(DateTime(timezone=True), nullable=True)
    refund_checked_at = Column(DateTime(timezone=True), nullable=True)
    inventory_consumed = Column(Boolean, default=False, nullable=False, server_default="0")
    tracking_number = Column(String(200), nullable=True)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc), index=True)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc), onupdate=lambda: datetime.datetime.now(datetime.timezone.utc))

class FigureShipping(Base):
    __tablename__ = "figure_shipping"
    id = Column(Integer, primary_key=True, default=1)
    shipping_fee = Column(Numeric(8, 2), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_figure_shipping_singleton"),
        CheckConstraint("shipping_fee >= 0 AND shipping_fee <= 999999.99", name="ck_figure_shipping_fee"),
    )


class FigureDiscount(Base):
    __tablename__ = "figure_discounts"
    id = Column(Integer, primary_key=True, default=1)
    discount_bps = Column(Integer, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_figure_discount_singleton"),
        CheckConstraint("discount_bps >= 0 AND discount_bps <= 9900", name="ck_figure_discount_rate"),
    )


class CreatorCode(Base):
    __tablename__ = "creator_codes"
    code = Column(String(32), primary_key=True)
    creator_name = Column(String(100), nullable=False, default="") # Legacy display name; no longer accepted or exposed.
    creator_user_id = Column(String(16), ForeignKey("users.id"), nullable=True, index=True)
    discount_bps = Column(Integer, nullable=False)
    commission_bps = Column(Integer, nullable=False)
    active = Column(Boolean, nullable=False, default=True, server_default="1")
    expires_at = Column(DateTime(timezone=True), nullable=True)
    version = Column(Integer, nullable=False, default=1, server_default="1")
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.datetime.now(timezone.utc))
    __table_args__ = (
        CheckConstraint("discount_bps >= 0 AND discount_bps <= 9900", name="ck_creator_discount"),
        CheckConstraint("commission_bps >= 0 AND commission_bps <= 10000", name="ck_creator_commission"),
    )


class UserCreatorReferral(Base):
    __tablename__ = "user_creator_referrals"
    user_id = Column(String(16), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    original_code = Column(String(32), nullable=False, index=True)
    original_at = Column(DateTime(timezone=True), nullable=False)
    current_code = Column(String(32), nullable=False)
    current_clicked_at = Column(DateTime(timezone=True), nullable=False)
    # Retained receipt expiry for audit/compatibility; the bound default never ages out.
    current_expires_at = Column(DateTime(timezone=True), nullable=False)


class CreatorPayout(Base):
    __tablename__ = "creator_payouts"
    id = Column(String(16), primary_key=True, default=generate_base58_id)
    creator_user_id = Column(String(16), ForeignKey("users.id"), nullable=False, index=True)
    request_key = Column(String(64), nullable=False)
    amount_cents = Column(Integer, nullable=False)
    currency = Column(String(3), nullable=False, default="USD")
    paypal_email = Column(String(127), nullable=False)
    status = Column(String(32), nullable=False, default="pending_review", index=True)
    reviewed_by = Column(String(16), ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    review_note = Column(String(500), nullable=True)
    paypal_batch_id = Column(String(100), nullable=True, unique=True)
    paypal_item_id = Column(String(100), nullable=True, unique=True)
    paypal_transaction_id = Column(String(100), nullable=True)
    paypal_status = Column(String(32), nullable=True)
    first_sent_at = Column(DateTime(timezone=True), nullable=True)
    next_check_at = Column(DateTime(timezone=True), nullable=True, index=True)
    processing_until = Column(DateTime(timezone=True), nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    last_error = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.datetime.now(timezone.utc), onupdate=lambda: datetime.datetime.now(timezone.utc))
    __table_args__ = (
        UniqueConstraint("creator_user_id", "request_key", name="uq_creator_payout_request"),
        CheckConstraint("amount_cents > 0", name="ck_creator_payout_amount"),
        CheckConstraint("currency = 'USD'", name="ck_creator_payout_currency"),
    )


class CreatorPayoutEvent(Base):
    __tablename__ = "creator_payout_events"
    id = Column(String(16), primary_key=True, default=generate_base58_id)
    payout_id = Column(String(16), ForeignKey("creator_payouts.id"), nullable=False, index=True)
    status = Column(String(32), nullable=False)
    event = Column(String(100), nullable=False)
    actor_user_id = Column(String(16), ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.datetime.now(timezone.utc))


class OrderItem(Base):
    """Order item model"""
    __tablename__ = "order_items"

    id = Column(String(16), primary_key=True, default=generate_base58_id, index=True)
    order_id = Column(String(16), index=True, nullable=False)
    skin_url = Column(String(255), nullable=True)
    model_type = Column(String(100), nullable=False, default="10cm Model V1")
    price = Column(Float, nullable=False, default=60.0)
    refer_log_id = Column(String(16), nullable=True, index=True)
    source_snapshot = Column(JSON, nullable=True)
    kit_specifications_snapshot = Column(JSON, nullable=True)
    sticker_snapshot = Column(JSON, nullable=True)


    created_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc))


class ModelSalesLimit(Base):
    """Model sales limit configuration"""
    __tablename__ = "model_sales_limits"

    id = Column(String(16), primary_key=True, default=generate_base58_id, index=True)
    model_type = Column(String(100), unique=True, index=True, nullable=False)
    order_type = Column(String(20), nullable=False, default="print") # 'subscription' or 'print'
    stock = Column(Integer, default=100) # Remaining stock / quota
    price = Column(Float, nullable=False, default=0.0)
    kit_specifications = Column(JSON, nullable=True)

    
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
    type = Column(String(50), nullable=False)
    order_id = Column(String(16), nullable=True, index=True)
    message = Column(Text, nullable=True)
    event_key = Column(String(100), nullable=True, unique=True)
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
    idempotency_key = Column(String(180), nullable=True, unique=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc), index=True)

# Account-owned reservations remain local; world ORM models live in entropydrop_space.
from space.account_models import SpaceCreditAuthorization, SpaceCreditReservation
