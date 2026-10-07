from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

from backend.auth import get_current_user
from backend.database import get_db
from backend import models
from pydantic import BaseModel

router = APIRouter(prefix="/api/student", tags=["Student"])

# Pydantic response schemas
class StudentProfileResponse(BaseModel):
    id: int
    email: str
    full_name: str | None = None

    class Config:
        orm_mode = True

class ScheduleItemResponse(BaseModel):
    course_name: str
    day: str
    start_time: str
    end_time: str

    class Config:
        orm_mode = True

class GradeItemResponse(BaseModel):
    course_name: str
    grade: str

    class Config:
        orm_mode = True

# Endpoints
@router.get("/profile", response_model=StudentProfileResponse)
def get_profile(current_user: models.User = Depends(get_current_user)):
    """Return the authenticated student's profile information."""
    return current_user

@router.get("/schedule", response_model=List[ScheduleItemResponse])
def get_schedule(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    """Return the current class schedule for the authenticated student."""
    schedule_items = (
        db.query(models.Schedule)
        .filter(models.Schedule.student_id == current_user.id)
        .all()
    )
    if not schedule_items:
        return []
    return schedule_items

@router.get("/grades", response_model=List[GradeItemResponse])
def get_grades(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    """Return a list of courses and corresponding grades for the authenticated student."""
    grade_entries = (
        db.query(models.Grade)
        .filter(models.Grade.student_id == current_user.id)
        .all()
    )
    if not grade_entries:
        return []
    return grade_entries
