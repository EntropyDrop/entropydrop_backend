import os
from pydantic_settings import BaseSettings, SettingsConfigDict



class Settings(BaseSettings):
    # Runtime mode. Production enables stricter startup validation.
    ENVIRONMENT: str = "development"
    STRICT_CONFIG_VALIDATION: bool = False

    # Redis Config
    REDIS_URL: str = "redis://localhost:6379/0"
    GLOBAL_SOCKS_PROXY: str = ""
    # Default values, should be configured in .env file
    # Format: postgresql://[user]:[password]@[host]:[port]/[db_name]
    DATABASE_URL: str = ""
    AUTO_CREATE_TABLES: bool = False
    DB_POOL_SIZE: int = 3
    DB_MAX_OVERFLOW: int = 2
    DB_POOL_TIMEOUT: int = 30
    DB_POOL_RECYCLE: int = 1800

    # Per-instance API resource monitoring. Each API task publishes a short
    # heartbeat to Redis so the admin monitor can aggregate all replicas.
    BACKEND_METRICS_INTERVAL_SECONDS: int = 10
    BACKEND_METRICS_STALE_AFTER_SECONDS: int = 30
    BACKEND_METRICS_TTL_SECONDS: int = 120
    BACKEND_METRICS_HISTORY_HOURS: int = 12
    BACKEND_METRICS_HISTORY_BUCKET_SECONDS: int = 300
    
    # JWT Auth Config
    JWT_SECRET_KEY: str = ""
    JWT_ALGORITHM: str = ""
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    AUTH_SESSION_IDLE_DAYS: int = 7
    AUTH_SESSION_ABSOLUTE_DAYS: int = 90
    AUTH_SESSION_MAX_PER_USER: int = 10
    AUTH_SESSION_COOKIE_NAME: str = "ed_session"
    AUTH_SESSION_COOKIE_SECURE: bool = False
    AUTH_SESSION_COOKIE_DOMAIN: str = ""

    # Space uses the same JWT/users table. These settings identify the initial
    # persistent world and tune the integrated realtime WebSocket gateway.
    SPACE_DEFAULT_WORLD_ID: str = "00000000-0000-4000-8000-000000000001"
    SPACE_WORLD_SEED: int = 20260827
    SPACE_WS_URL: str = "/space/ws/v2"
    SPACE_WS_ALLOWED_ORIGINS: str = ""
    SPACE_REALTIME_INPUT_HZ: int = 20
    SPACE_REALTIME_SNAPSHOT_HZ: int = 10
    SPACE_REALTIME_PERSIST_SECONDS: int = 5
    SPACE_REALTIME_AOI_RADIUS_CHUNKS: int = 16
    SPACE_REALTIME_REDIS_FANOUT_ENABLED: bool = True
    # Durable Space quotas. Short windows protect the shared write path while
    # the UTC-day budget is the player-facing allowance.
    SPACE_TERRAIN_BURST_LIMIT: int = 5_000
    SPACE_TERRAIN_HOURLY_LIMIT: int = 80_000
    SPACE_TERRAIN_DAILY_LIMIT: int = 100_000
    SPACE_TERRAIN_WORLD_SECOND_LIMIT: int = 5_000
    SPACE_TERRAIN_MAX_CHUNKS_PER_BATCH: int = 16
    SPACE_TERRAIN_MAX_ZONES_PER_BATCH: int = 4
    SPACE_TERRAIN_EDIT_RADIUS_CHUNKS: int = 8
    SPACE_TERRAIN_POSITION_GRACE_SECONDS: int = 30
    SPACE_TERRAIN_MAX_EVENT_BYTES: int = 16 * 1024 * 1024
    SPACE_TERRAIN_MAX_RESPONSE_BYTES: int = 16 * 1024 * 1024
    SPACE_ENTITY_MAX_TOTAL_BYTES_PER_OWNER: int = 128 * 1024 * 1024
    SPACE_ENTITY_MAX_RUNNING_PER_OWNER: int = 8
    SPACE_ENTITY_MAX_RUNNING_PER_WORLD: int = 64
    SPACE_ENTITY_MAX_RUNNING_PER_CHUNK: int = 16
    SPACE_ENTITY_CHECKPOINT_MINUTE_BYTES: int = 16 * 1024 * 1024
    SPACE_ENTITY_CHECKPOINT_DAILY_BYTES: int = 512 * 1024 * 1024
    SPACE_MARKET_MAX_RESOURCES_PER_OWNER: int = 100
    SPACE_MARKET_MAX_TOTAL_BYTES_PER_OWNER: int = 256 * 1024 * 1024
    SPACE_MARKET_DAILY_UPLOAD_BYTES: int = 64 * 1024 * 1024
    # Epoch-1 terrain batches carry a stable client timestamp. Their dedupe
    # receipts can be removed after this window; older epoch-0 receipts remain
    # indefinitely for compatibility with already-persisted browser outboxes.
    SPACE_TERRAIN_BATCH_RECEIPT_RETENTION_DAYS: int = 30
    
    # Google OAuth
    GOOGLE_CLIENT_ID: str = ""

    # AWS S3 / CDN
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_REGION: str = ""
    AWS_BUCKET_NAME: str = ""
    AWS_PRIVATE_BUCKET_NAME: str = ""
    AWS_CDN_DOMAIN: str = ""
    AWS_CLOUDFRONT_DISTRIBUTION_ID: str = ""
    AWS_DOMAIN_NAME: str = ""

    # AWS Infrastructure (Self-managed)
    AWS_RDS_DB_IDENTIFIER: str = "ed-db"
    AWS_RDS_MASTER_USERNAME: str = "postgres"
    AWS_RDS_MASTER_PASSWORD: str = ""
    AWS_EC2_KEY_NAME: str = "ed-redis-key"
    AWS_EC2_SG_NAME: str = "ed-redis-sg"
    AWS_REDIS_PASSWORD: str = ""

    # PayPal Config
    PAYPAL_CLIENT_ID: str = ""
    PAYPAL_SECRET: str = ""
    PAYPAL_API_BASE: str = "" # https://api-m.sandbox.paypal.com for sandbox, https://api-m.paypal.com for prod
    PAYPAL_PRO_PLUS_PLAN_ID: str = ""
    PAYPAL_PRO_MAX_PLAN_ID: str = ""
    PAYPAL_WEBHOOK_ID: str = ""

    # Public ledger sync
    LEDGER_SYNC_INTERVAL_SECONDS: int = 86400
    LEDGER_PAYPAL_LOOKBACK_DAYS: int = 3
    LEDGER_AWS_LOOKBACK_DAYS: int = 30

    ADMIN_EMAILS: str = "" # Comma separated list of admin emails
    TRUSTED_PROXY_CIDRS: str = "" # Comma separated CIDRs allowed to supply X-Forwarded-For/X-Real-IP

    model_config = SettingsConfigDict(
        env_file=os.getenv("ENV_FILE", ".env"), 
        env_file_encoding='utf-8',
        extra='ignore'
    )

settings = Settings()


def _production_validation_enabled() -> bool:
    env_file = os.getenv("ENV_FILE", "")
    environment = settings.ENVIRONMENT.strip().lower()
    return (
        settings.STRICT_CONFIG_VALIDATION
        or environment in {"prod", "production"}
        or "prod" in os.path.basename(env_file).lower()
    )


def validate_runtime_settings() -> None:
    if not _production_validation_enabled():
        return

    errors = []
    placeholder_secrets = {
        "",
        "change-me",
        "change-me-to-a-long-random-secret",
        "secret",
        "jwt-secret",
    }

    jwt_secret = settings.JWT_SECRET_KEY.strip()
    if jwt_secret.lower() in placeholder_secrets or len(jwt_secret) < 32:
        errors.append("JWT_SECRET_KEY must be a non-placeholder secret of at least 32 characters")

    if settings.JWT_ALGORITHM not in {"HS256", "HS384", "HS512"}:
        errors.append("JWT_ALGORITHM must be one of HS256, HS384, or HS512")

    if settings.ACCESS_TOKEN_EXPIRE_MINUTES <= 0:
        errors.append("ACCESS_TOKEN_EXPIRE_MINUTES must be greater than 0")

    if settings.AUTH_SESSION_IDLE_DAYS <= 0:
        errors.append("AUTH_SESSION_IDLE_DAYS must be greater than 0")

    if settings.AUTH_SESSION_ABSOLUTE_DAYS < settings.AUTH_SESSION_IDLE_DAYS:
        errors.append("AUTH_SESSION_ABSOLUTE_DAYS must be at least AUTH_SESSION_IDLE_DAYS")

    if settings.AUTH_SESSION_MAX_PER_USER <= 0:
        errors.append("AUTH_SESSION_MAX_PER_USER must be greater than 0")

    if not settings.AUTH_SESSION_COOKIE_SECURE:
        errors.append("AUTH_SESSION_COOKIE_SECURE must be enabled in production")

    if not settings.DATABASE_URL or settings.DATABASE_URL.startswith("sqlite"):
        errors.append("DATABASE_URL must point to a production database")

    if not settings.REDIS_URL:
        errors.append("REDIS_URL must be configured")

    positive_space_limits = {
        "SPACE_TERRAIN_BURST_LIMIT": settings.SPACE_TERRAIN_BURST_LIMIT,
        "SPACE_TERRAIN_HOURLY_LIMIT": settings.SPACE_TERRAIN_HOURLY_LIMIT,
        "SPACE_TERRAIN_DAILY_LIMIT": settings.SPACE_TERRAIN_DAILY_LIMIT,
        "SPACE_TERRAIN_WORLD_SECOND_LIMIT": settings.SPACE_TERRAIN_WORLD_SECOND_LIMIT,
        "SPACE_TERRAIN_MAX_CHUNKS_PER_BATCH": settings.SPACE_TERRAIN_MAX_CHUNKS_PER_BATCH,
        "SPACE_TERRAIN_MAX_ZONES_PER_BATCH": settings.SPACE_TERRAIN_MAX_ZONES_PER_BATCH,
        "SPACE_TERRAIN_EDIT_RADIUS_CHUNKS": settings.SPACE_TERRAIN_EDIT_RADIUS_CHUNKS,
        "SPACE_TERRAIN_MAX_EVENT_BYTES": settings.SPACE_TERRAIN_MAX_EVENT_BYTES,
        "SPACE_TERRAIN_MAX_RESPONSE_BYTES": settings.SPACE_TERRAIN_MAX_RESPONSE_BYTES,
        "SPACE_ENTITY_MAX_TOTAL_BYTES_PER_OWNER": settings.SPACE_ENTITY_MAX_TOTAL_BYTES_PER_OWNER,
        "SPACE_ENTITY_MAX_RUNNING_PER_OWNER": settings.SPACE_ENTITY_MAX_RUNNING_PER_OWNER,
        "SPACE_ENTITY_MAX_RUNNING_PER_WORLD": settings.SPACE_ENTITY_MAX_RUNNING_PER_WORLD,
        "SPACE_ENTITY_MAX_RUNNING_PER_CHUNK": settings.SPACE_ENTITY_MAX_RUNNING_PER_CHUNK,
        "SPACE_ENTITY_CHECKPOINT_MINUTE_BYTES": settings.SPACE_ENTITY_CHECKPOINT_MINUTE_BYTES,
        "SPACE_ENTITY_CHECKPOINT_DAILY_BYTES": settings.SPACE_ENTITY_CHECKPOINT_DAILY_BYTES,
        "SPACE_MARKET_MAX_RESOURCES_PER_OWNER": settings.SPACE_MARKET_MAX_RESOURCES_PER_OWNER,
        "SPACE_MARKET_MAX_TOTAL_BYTES_PER_OWNER": settings.SPACE_MARKET_MAX_TOTAL_BYTES_PER_OWNER,
        "SPACE_MARKET_DAILY_UPLOAD_BYTES": settings.SPACE_MARKET_DAILY_UPLOAD_BYTES,
    }
    for name, value in positive_space_limits.items():
        if value <= 0:
            errors.append(f"{name} must be greater than 0")

    if not settings.GOOGLE_CLIENT_ID:
        errors.append("GOOGLE_CLIENT_ID must be configured")

    if errors:
        raise RuntimeError("Invalid production configuration: " + "; ".join(errors))


validate_runtime_settings()
