"""
Authentication and Authorization Utilities
Provides JWT token generation, validation, and user context
"""

import os
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel

# JWT Configuration
SECRET_KEY = os.getenv("JWT_SECRET_KEY", "your-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 hours

security = HTTPBearer()


class TokenData(BaseModel):
    """JWT token payload"""

    user_id: str
    username: str | None = None
    exp: datetime | None = None


class User(BaseModel):
    """User model"""

    user_id: str
    username: str
    email: str | None = None
    is_active: bool = True


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """
    Create JWT access token.

    Args:
        data: Payload data (must include 'user_id')
        expires_delta: Token expiration time

    Returns:
        Encoded JWT token
    """
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

    return encoded_jwt


def verify_token(token: str) -> TokenData:
    """
    Verify and decode JWT token.

    Args:
        token: JWT token string

    Returns:
        TokenData with user information

    Raises:
        HTTPException: If token is invalid or expired
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("user_id")

        if user_id is None:
            raise credentials_exception

        token_data = TokenData(
            user_id=user_id, username=payload.get("username"), exp=payload.get("exp")
        )

        return token_data

    except JWTError:
        raise credentials_exception


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> TokenData:
    """
    FastAPI dependency to get current authenticated user.

    Usage:
        @router.get("/protected")
        def protected_route(user: TokenData = Depends(get_current_user)):
            return {"user_id": user.user_id}
    """
    token = credentials.credentials
    return verify_token(token)


def get_current_user_id(user: TokenData = Depends(get_current_user)) -> str:
    """
    FastAPI dependency to get current user ID.

    Usage:
        @router.get("/my-data")
        def get_my_data(user_id: str = Depends(get_current_user_id)):
            return {"user_id": user_id}
    """
    return user.user_id


# Development mode: Allow bypassing auth with default user
def get_current_user_dev() -> TokenData:
    """
    Development-only: Returns default user without authentication.
    DO NOT USE IN PRODUCTION!
    """
    if os.getenv("ENVIRONMENT") != "development":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Development mode not enabled"
        )

    return TokenData(user_id="default_user", username="dev_user")


def get_user_id_with_fallback(
    credentials: HTTPAuthorizationCredentials | None = Depends(HTTPBearer(auto_error=False)),
) -> str:
    """
    Get user ID with fallback to default_user in development.
    Use this during migration period.

    TODO(#7): Remove this after implementing full authentication
    """
    if credentials:
        try:
            token_data = verify_token(credentials.credentials)
            return token_data.user_id
        except HTTPException:
            pass

    # Fallback to default_user (temporary)
    if os.getenv("ENVIRONMENT", "development") == "development":
        return "default_user"

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
