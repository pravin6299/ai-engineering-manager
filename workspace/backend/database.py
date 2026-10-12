from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from backend.models import Base

# Example SQLite URL; replace with actual DB URL in production
SQLALCHEMY_DATABASE_URL = "sqlite:///./school.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db():
    """Create all tables based on the models if they do not exist."""
    Base.metadata.create_all(bind=engine)

def get_db():
    """FastAPI dependency that provides a DB session and ensures cleanup."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
