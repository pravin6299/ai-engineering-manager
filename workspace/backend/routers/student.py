from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

from backend import models
from backend.database import get_db
from backend.auth import verify_jwt_token, JWTError

router = APIRouter(prefix="/api/student", tags=["student"])

# Pydantic response schemas
from pydantic import BaseModel

class StudentProfile(BaseModel):
    id: int
    full_name: str
    email: str
    enrollment_year: int

    class Config:
        orm_mode = True

class GradeItem(BaseModel):
    course_code: str
    course_name: str
    grade: str

    class Config:
        orm_mode = True

class ScheduleItem(BaseModel):
    day: str
    start_time: str
    end_time: str
    course_code: str
    location: str

    class Config:
        orm_mode = True

# Dependency to get current student from JWT
async def get_current_student(token: str = Depends(verify_jwt_token), db: Session = Depends(get_db)) -> models.Student:
    try:
        payload = token
        user_id: int = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials")

    student = db.query(models.Student).filter(models.Student.user_id == user_id).first()
    if not student:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Student not found")
    return student

@router.get("/profile", response_model=StudentProfile)
def get_profile(current_student: models.Student = Depends(get_current_student)):
    return current_student

@router.get("/grades", response_model=List[GradeItem])
def get_grades(db: Session = Depends(get_db), current_student: models.Student = Depends(get_current_student)):
    grades = (
        db.query(models.Grade)
        .filter(models.Grade.student_id == current_student.id)
        .all()
    )
    return grades

@router.get("/schedule", response_model=List[ScheduleItem])
def get_schedule(db: Session = Depends(get_db), current_student: models.Student = Depends(get_current_student)):
    schedule_entries = (
        db.query(models.Schedule)
        .filter(models.Schedule.student_id == current_student.id)
        .all()
    )
    return schedule_entries