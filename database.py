from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from config import settings


def get_engine_options():
    if settings.DATABASE_URL.startswith("sqlite"):
        return {}

    return {
        "pool_pre_ping": True,
        "pool_size": settings.DB_POOL_SIZE,
        "max_overflow": settings.DB_MAX_OVERFLOW,
        "pool_timeout": settings.DB_POOL_TIMEOUT,
        "pool_recycle": settings.DB_POOL_RECYCLE,
    }


# Create database engine
engine = create_engine(settings.DATABASE_URL, **get_engine_options())

# Create local session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Create model base class
Base = declarative_base()

# Dependency injection: get database session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
