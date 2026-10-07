from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List
from pydantic import BaseModel

from backend.auth import get_current_user  # returns authenticated User model
from backend.database import get_db
from backend.models import User, Course, Enrollment, Grade, Notification

router = APIRouter(prefix="/api/student", tags=["Student Dashboard"])

class CourseInfo(BaseModel):
    id: int
    name: str
    code: str

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
    read: bool

    class Config:
        orm_mode = True

class DashboardResponse(BaseModel):
    id: int
    email: str
    full_name: str
    courses: List[CourseInfo]
    grades: List[GradeInfo]
    notifications: List[NotificationInfo]

    class Config:
        orm_mode = True

@router.get("/dashboard", response_model=DashboardResponse)
def get_student_dashboard(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    # Optional role check if User model includes a role field
    # if getattr(current_user, "role", None) != "student":
    #     raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access forbidden")

    # Enrolled courses
    courses = (
        db.query(Course)
        .join(Enrollment, Enrollment.course_id == Course.id)
        .filter(Enrollment.student_id == current_user.id)
        .all()
    )

    # Grades linked via enrollment
    grades = (
        db.query(Grade)
        .join(Enrollment, Enrollment.id == Grade.enrollment_id)
        .filter(Enrollment.student_id == current_user.id)
        .all()
    )

    # Student notifications
    notifications = (
        db.query(Notification)
        .filter(Notification.user_id == current_user.id)
        .all()
    )

    return DashboardResponse(
        id=current_user.id,
        email=current_user.email,
        full_name=getattr(current_user, "full_name", ""),
        courses=courses,
        grades=grades,
        notifications=notifications,
    )
