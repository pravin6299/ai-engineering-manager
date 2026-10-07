from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

# OAuth2 scheme used for token authentication. The tokenUrl should match the endpoint that provides tokens.
auth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


def get_current_user(token: str = Depends(auth2_scheme)):
    """Retrieve the current user based on the provided OAuth2 token.

    For testing purposes this function simply returns the raw token string.
    In a production setting you would decode the JWT, validate it, and fetch
    the associated user record.
    """
    # Placeholder logic for tests – return token directly.
    return token
