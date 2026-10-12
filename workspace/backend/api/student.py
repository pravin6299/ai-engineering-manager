from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

from backend.models import StudentProfile, Grade, Schedule
from backend.database import get_db
from backend.auth import get_current_user
from pydantic import BaseModel

router = APIRouter(prefix="/api/student", tags=["student"])

# Pydantic response schemas
class StudentProfileResponse(BaseModel):
    id: int
    user_id: int
    full_name: str
    email: str
    major: str | None = None

    class Config:
        orm_mode = True

class GradeResponse(BaseModel):
    course_code: str
    course_name: str
    grade: str

    class Config:
        orm_mode = True

class ScheduleItemResponse(BaseModel):
    course_code: str
    course_name: str
    day: str
    start_time: str
    end_time: str
    location: str

    class Config:
        orm_mode = True

@router.get("/profile", response_model=StudentProfileResponse)
def get_student_profile(current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    profile = db.query(StudentProfile).filter(StudentProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")
    return profile

@router.get("/grades", response_model=List[GradeResponse])
def get_student_grades(current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    grades = db.query(Grade).filter(Grade.student_id == current_user.id).all()
    return grades

@router.get("/schedule", response_model=List[ScheduleItemResponse])
def get_student_schedule(current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    schedule_items = db.query(Schedule).filter(Schedule.student_id == current_user.id).all()
    return schedule_items