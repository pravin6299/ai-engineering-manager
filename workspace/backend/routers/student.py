from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from typing import List

from backend.models import User, Schedule, Grade
from backend.database import get_db
from backend.auth import get_current_user
from pydantic import BaseModel

router = APIRouter(prefix="/api/student", tags=["student"])

# ---------- Response Schemas ----------
class StudentProfile(BaseModel):
    id: int
    email: str
    full_name: str | None = None

    class Config:
        orm_mode = True

class ScheduleItem(BaseModel):
    id: int
    course_name: str
    start_time: str
    end_time: str
    location: str | None = None

    class Config:
        orm_mode = True

class GradeItem(BaseModel):
    id: int
    course_name: str
    grade: str

    class Config:
        orm_mode = True

# ---------- Endpoints ----------
@router.get("/profile", response_model=StudentProfile)
def get_profile(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Return the logged‑in student's profile information."""
    # Assuming the User model stores the needed profile fields.
    return StudentProfile.from_orm(current_user)

@router.get("/schedule", response_model=List[ScheduleItem])
def get_schedule(
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Return the current class schedule for the logged‑in student, paginated."""
    offset = (page - 1) * size
    schedule_q = (
        db.query(Schedule)
        .filter(Schedule.student_id == current_user.id)
        .order_by(Schedule.start_time)
        .offset(offset)
        .limit(size)
    )
    items = schedule_q.all()
    return [ScheduleItem.from_orm(item) for item in items]

@router.get("/grades", response_model=List[GradeItem])
def get_grades(
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Return a list of courses with grades for the logged‑in student, paginated."""
    offset = (page - 1) * size
    grades_q = (
        db.query(Grade)
        .filter(Grade.student_id == current_user.id)
        .order_by(Grade.course_name)
        .offset(offset)
        .limit(size)
    )
    items = grades_q.all()
    return [GradeItem.from_orm(item) for item in items]
