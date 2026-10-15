from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional

from pydantic import BaseModel

from backend import models
from backend.database import get_db
from backend.auth import get_current_user


class ProfileSchema(BaseModel):
    id: int
    email: str
    full_name: Optional[str]

    class Config:
        orm_mode = True


class CourseSchema(BaseModel):
    id: int
    title: str
    description: Optional[str]

    class Config:
        orm_mode = True


class GradeSchema(BaseModel):
    course_id: int
    grade: Optional[str]

    class Config:
        orm_mode = True


class NotificationSchema(BaseModel):
    id: int
    message: str
    created_at: Optional[str]

    class Config:
        orm_mode = True


class DashboardResponse(BaseModel):
    profile: ProfileSchema
    courses: List[CourseSchema] = []
    grades: List[GradeSchema] = []
    notifications: List[NotificationSchema] = []


router = APIRouter(prefix="/api/student", tags=["student"])


@router.get("/dashboard", response_model=DashboardResponse)
def get_dashboard(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    # Profile information is the current user
    profile = current_user

    # Enrolled courses
    enrollments = (
        db.query(models.Enrollment)
        .filter(models.Enrollment.student_id == current_user.id)
        .all()
    )
    course_ids = [e.course_id for e in enrollments]
    courses = (
        db.query(models.Course)
        .filter(models.Course.id.in_(course_ids))
        .all()
        if course_ids
        else []
    )

    # Grades
    grades = (
        db.query(models.Grade)
        .filter(models.Grade.student_id == current_user.id)
        .all()
    )

    # Recent notifications (latest 10)
    notifications = (
        db.query(models.Notification)
        .filter(models.Notification.user_id == current_user.id)
        .order_by(models.Notification.created_at.desc())
        .limit(10)
        .all()
    )

    return DashboardResponse(
        profile=profile,
        courses=courses,
        grades=grades,
        notifications=notifications,
    )
