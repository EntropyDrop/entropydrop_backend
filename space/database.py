"""A standalone Space process opens only its local database."""
from config import settings

if not settings.SPACE_STANDALONE:
    from database import Base, SessionLocal, get_db, engine
else:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import declarative_base, sessionmaker

    Base = declarative_base()
    options = {} if settings.DATABASE_URL.startswith("sqlite") else {
        "pool_pre_ping": True, "pool_size": settings.DB_POOL_SIZE,
        "max_overflow": settings.DB_MAX_OVERFLOW, "pool_timeout": settings.DB_POOL_TIMEOUT,
        "pool_recycle": settings.DB_POOL_RECYCLE,
    }
    engine = create_engine(settings.DATABASE_URL, **options)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def get_db():
        with SessionLocal() as db:
            yield db
