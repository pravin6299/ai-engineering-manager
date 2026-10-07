from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from pydantic import BaseModel
from backend import models
from backend.database import get_db
from backend.dependencies import get_current_user

router = APIRouter(prefix="/api/student", tags=["student"])


class StudentProfileSchema(BaseModel):
    id: int
    email: str
    full_name: str | None = None

    class Config:
        orm_mode = True


class GradeItemSchema(BaseModel):
    course_id: int
    course_name: str
    grade: str | None = None

    class Config:
        orm_mode = True


class ScheduleItemSchema(BaseModel):
    course_id: int
    course_name: str
    day: str
    start_time: str
    end_time: str

    class Config:
        orm_mode = True


@router.get("/profile", response_model=StudentProfileSchema)
def get_profile(current_user: models.User = Depends(get_current_user)):
    return StudentProfileSchema(
        id=current_user.id,
        email=current_user.email,
        full_name=getattr(current_user, "full_name", None),
    )


@router.get("/grades", response_model=list[GradeItemSchema])
def get_grades(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    # TODO: Replace with actual query once Grade models are defined.
    return []


@router.get("/schedule", response_model=list[ScheduleItemSchema])
def get_schedule(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    # TODO: Replace with actual query once Schedule models are defined.
    return []
