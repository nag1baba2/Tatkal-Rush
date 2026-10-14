"""Small JWT auth: bcrypt password hashes, signed bearer tokens.

No refresh tokens, roles or password reset; those do not affect correctness
under concurrency.
"""

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import User

bearer_scheme = HTTPBearer(auto_error=False)

# Checked when the email is unknown so login timing does not reveal which
# emails exist.
_DUMMY_HASH = bcrypt.hashpw(b"dummy-password", bcrypt.gensalt()).decode()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def verify_against_dummy(password: str) -> None:
    verify_password(password, _DUMMY_HASH)


def create_access_token(user_id: int, expires_minutes: int | None = None) -> str:
    minutes = settings.jwt_expire_minutes if expires_minutes is None else expires_minutes
    now = datetime.now(timezone.utc)
    claims = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=minutes),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def user_id_from_token(token: str) -> int | None:
    """Verify a token's signature and expiry and return its user id, without
    touching the database (used by the rate limiter, which must stay cheap and
    must not hold a DB connection)."""
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        return int(claims["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return None


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    unauthorized = HTTPException(
        status_code=401,
        detail="invalid_or_missing_token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if creds is None:
        raise unauthorized
    try:
        claims = jwt.decode(
            creds.credentials, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
        user_id = int(claims["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise unauthorized
    user = db.get(User, user_id)
    # End this read-only transaction now so the DB connection goes back to the
    # pool. Otherwise the request keeps it while waiting for a worker thread to
    # run the endpoint, and under a burst every connection is held by a request
    # that cannot get a thread: a threads-vs-connections deadlock.
    db.commit()
    if user is None:
        raise unauthorized
    return user
