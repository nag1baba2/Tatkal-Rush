from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import (
    create_access_token,
    hash_password,
    verify_against_dummy,
    verify_password,
)
from app.db import get_db
from app.models import User
from app.schemas import Credentials, TokenOut, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=201, response_model=UserOut)
def register(body: Credentials, db: Session = Depends(get_db)):
    user = User(email=body.email, password_hash=hash_password(body.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="email_already_registered")
    return user


@router.post("/login", response_model=TokenOut)
def login(body: Credentials, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == body.email))
    db.commit()  # also: do not hold a connection while the slow bcrypt check runs
    if user is None:
        verify_against_dummy(body.password)
        raise HTTPException(status_code=401, detail="invalid_credentials")
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid_credentials")
    return TokenOut(access_token=create_access_token(user.id))
