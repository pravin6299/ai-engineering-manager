from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, validator

from backend import models
from backend.database import get_db
from backend.auth import create_access_token

router = APIRouter(prefix="/api", tags=["auth"])

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

class RegisterSchema(BaseModel):
    username: str
    email: EmailStr
    password: str

    @validator('username')
    def username_not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError('Username must not be empty')
        return v

class LoginSchema(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"

@router.post("/register", status_code=status.HTTP_201_CREATED)
def register_user(payload: RegisterSchema, db: Session = Depends(get_db)):
    # Check if user exists
    existing_user = db.query(models.User).filter(
        (models.User.username == payload.username) | (models.User.email == payload.email)
    ).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username or email already registered"
        )
    # Hash password
    hashed_password = pwd_context.hash(payload.password)
    # Create user instance
    user = models.User(
        username=payload.username,
        email=payload.email,
        password_hash=hashed_password,
        role="user"  # default role, adjust as needed
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"id": user.id, "username": user.username, "email": user.email}

@router.post("/login", response_model=TokenResponse)
def login_user(payload: LoginSchema, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.username == payload.username).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"}
        )
    if not pwd_context.verify(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"}
        )
    token_data = {"sub": str(user.id), "username": user.username, "role": getattr(user, "role", "user")}
    access_token = create_access_token(data=token_data)
    return {"access_token": access_token, "token_type": "bearer"}
