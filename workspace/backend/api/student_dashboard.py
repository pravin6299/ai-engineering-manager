from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime

from backend.models import User, Course, Enrollment, Grade, Notification
from backend.database import get_db
from backend.auth import get_current_user
from pydantic import BaseModel

router = APIRouter(prefix="/api/student", tags=["Student Dashboard"])

# Pydantic response schemas
class UserProfile(BaseModel):
    id: int
    email: str
    full_name: str | None = None

    class Config:
        orm_mode = True

class CourseInfo(BaseModel):
    id: int
    title: str
    description: str | None = None

    class Config:
        orm_mode = True

class GradeInfo(BaseModel):
    course_id: int
    grade: str

    class Config:
        orm_mode = True

class NotificationInfo(BaseModel):
    id: int
    message: str
    created_at: datetime

    class Config:
        orm_mode = True

class StudentDashboardResponse(BaseModel):
    profile: UserProfile
    courses: List[CourseInfo]
    grades: List[GradeInfo]
    notifications: List[NotificationInfo]

@router.get("/dashboard", response_model=StudentDashboardResponse)
def get_dashboard(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    # Profile information
    profile = UserProfile.from_orm(current_user)

    # Enrolled courses
    enrolled_courses = (
        db.query(Course)
        .join(Enrollment, Enrollment.course_id == Course.id)
        .filter(Enrollment.student_id == current_user.id)
        .all()
    )
    courses = [CourseInfo.from_orm(c) for c in enrolled_courses]

    # Grades
    grade_records = (
        db.query(Grade)
        .filter(Grade.student_id == current_user.id)
        .all()
    )
    grades = [GradeInfo.from_orm(g) for g in grade_records]

    # Recent notifications (limit to latest 5)
    recent_notifications = (
        db.query(Notification)
        .filter(Notification.user_id == current_user.id)
        .order_by(Notification.created_at.desc())
        .limit(5)
        .all()
    )
    notifications = [NotificationInfo.from_orm(n) for n in recent_notifications]

    return StudentDashboardResponse(
        profile=profile,
        courses=courses,
        grades=grades,
        notifications=notifications,
    )
