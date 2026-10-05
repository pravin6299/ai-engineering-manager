import os
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# Default to a local SQLite database; can be overridden with DATABASE_URL env var
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./test.db")

# SQLite requires a special connect_args setting
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for declarative models
Base = declarative_base()

def get_db():
    """Yield a database session for FastAPI dependency injection.
    The caller is responsible for closing the session.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
