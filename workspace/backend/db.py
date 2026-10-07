from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# Database URL – using a file‑based SQLite database for simplicity.
# In a testing environment this can be overridden if needed.
SQLALCHEMY_DATABASE_URL = "sqlite:///./test.db"

# The engine is configured for SQLite; the ``check_same_thread`` flag is required
# for usage with FastAPI's default thread‑per‑request model.
engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)

# Session factory used throughout the application to obtain database sessions.
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for all ORM models.
Base = declarative_base()

# Optional helper to provide a dependency for FastAPI routes.
def get_db():
    """Yield a SQLAlchemy session and ensure it is closed after use.

    This function can be used as a FastAPI dependency:
        ``db: Session = Depends(get_db)``
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
