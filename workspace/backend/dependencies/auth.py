from pydantic import BaseModel

class User(BaseModel):
    """Simple User model used for authentication dependencies in tests.

    The actual application may have a richer model; this stub provides the
    minimal fields required by the test suite.
    """

    id: int
    username: str
    email: str | None = None

    class Config:
        from_attributes = True
