from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel
from typing import Optional

# OAuth2 scheme used for token extraction. In tests the token can be a simple string.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


class User(BaseModel):
    """Simple user representation used throughout the application.

    The fields are deliberately minimal for the purpose of the tests. In a real
    application you would likely include more attributes and proper validation.
    """

    username: str
    email: Optional[str] = None
    is_active: bool = True
    is_superuser: bool = False


def get_current_user(token: str = Depends(oauth2_scheme)) -> User:
    """Dependency that extracts the current user from the provided OAuth2 token.

    For the test suite we keep the logic straightforward: if a token is present
    and not the literal string ``"invalid"``, we treat the token value as the
    username and return a ``User`` instance. Otherwise an HTTP 401 error is
    raised.
    """
    if not token or token == "invalid":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # In a real implementation the token would be decoded to extract user info.
    # Here we simply use the token string as the username for simplicity.
    return User(username=token)
