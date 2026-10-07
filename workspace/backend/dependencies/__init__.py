from typing import Generator

# Import the sessionmaker (SessionLocal) from the project's database module.
# It is expected that `backend.app.database` defines `SessionLocal` which creates
# new SQLAlchemy Session objects bound to the engine.
from backend.app.database import SessionLocal


def get_db() -> Generator:
    """FastAPI dependency that provides a SQLAlchemy session.

    The function yields a session and ensures it is closed after the request
    finishes. This implementation matches the typical pattern used in FastAPI
    tutorials and satisfies the import required by the router modules.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
