import datetime
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, constr
from sqlalchemy.orm import Session

from .. import auth, models, schemas
from ..database import get_db

router = APIRouter(prefix="/api", tags=["auth"])

class RegisterRequest(BaseModel):
    email: EmailStr
    password: constr(min_length=8)
    full_name: str | None = None
    role: str = "user"

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"

@router.post("/auth/register", response_model=schemas.UserOut, status_code=status.HTTP_201_CREATED)
@router.post("/register", response_model=schemas.UserOut, status_code=status.HTTP_201_CREATED)
def register_user(
    payload: RegisterRequest,
    db: Session = Depends(get_db)
):
    if payload.role not in {"user", "student"}:
        raise HTTPException(status_code=403, detail="Role requires privileged registration")
    # Check if user already exists
    existing = db.query(models.User).filter(models.User.email == payload.email).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )
    hashed_pw = auth.get_password_hash(payload.password)
    user = models.User(
        email=payload.email,
        full_name=payload.full_name,
        hashed_password=hashed_pw,
        role=payload.role
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user

@router.post("/auth/login", response_model=TokenResponse)
@router.post("/login", response_model=TokenResponse)
def login_user(
    payload: LoginRequest,
    db: Session = Depends(get_db)
):
    user = db.query(models.User).filter(models.User.email == payload.email).first()
    if not user or not auth.verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"}
        )
    access_token = auth.create_access_token(
        data={"sub": user.email, "role": user.role}
    )
    refresh_token = auth.create_refresh_token({"sub": user.email})
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.get("/auth/me", response_model=schemas.UserOut)
def current_user(user: models.User = Depends(auth.get_current_user)):
    return user


@router.get("/auth/student-only")
def student_access(user: models.User = Depends(auth.require_role(["student", "teacher", "admin"]))):
    return {"role": user.role}


@router.get("/auth/teacher-only")
def teacher_access(user: models.User = Depends(auth.require_role(["teacher", "admin"]))):
    return {"role": user.role}


@router.get("/auth/admin-only")
def admin_access(user: models.User = Depends(auth.require_role(["admin"]))):
    return {"role": user.role}
