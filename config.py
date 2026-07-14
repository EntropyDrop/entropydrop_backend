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
    
    # JWT Auth Config
    JWT_SECRET_KEY: str = ""
    JWT_ALGORITHM: str = ""
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 0
    
    # Google OAuth
    GOOGLE_CLIENT_ID: str = ""

    # AWS S3 / CDN
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_REGION: str = ""
    AWS_BUCKET_NAME: str = ""
    AWS_PRIVATE_BUCKET_NAME: str = ""
    AWS_CDN_DOMAIN: str = ""
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

    if not settings.DATABASE_URL or settings.DATABASE_URL.startswith("sqlite"):
        errors.append("DATABASE_URL must point to a production database")

    if not settings.REDIS_URL:
        errors.append("REDIS_URL must be configured")

    if not settings.GOOGLE_CLIENT_ID:
        errors.append("GOOGLE_CLIENT_ID must be configured")

    if errors:
        raise RuntimeError("Invalid production configuration: " + "; ".join(errors))


validate_runtime_settings()
