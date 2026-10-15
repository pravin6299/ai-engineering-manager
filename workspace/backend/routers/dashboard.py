from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional

from backend import models, schemas, deps
from backend.auth import get_current_user

router = APIRouter(prefix="/api/student", tags=["student-dashboard"])

# Pydantic response schemas
class ProfileSchema(schemas.BaseModel):
    id: int
    email: str
    full_name: Optional[str] = None

    class Config:
        orm_mode = True

class CourseSchema(schemas.BaseModel):
    id: int
    title: str
    description: Optional[str] = None

    class Config:
        orm_mode = True

class GradeSchema(schemas.BaseModel):
    course_id: int
    grade: Optional[float] = None

    class Config:
        orm_mode = True

class NotificationSchema(schemas.BaseModel):
    id: int
    message: str
    created_at: Optional[str] = None

    class Config:
        orm_mode = True

class DashboardResponse(schemas.BaseModel):
    profile: ProfileSchema
    courses: List[CourseSchema]
    grades: List[GradeSchema]
    notifications: List[NotificationSchema]

@router.get("/dashboard", response_model=DashboardResponse)
def get_dashboard(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(deps.get_db)
):
    # Profile information (reuse User model fields)
    profile_data = ProfileSchema.from_orm(current_user)

    # Enrolled courses
    enrollments = (
        db.query(models.Course)
        .join(models.Enrollment, models.Course.id == models.Enrollment.course_id)
        .filter(models.Enrollment.student_id == current_user.id)
        .all()
    )
    courses = [CourseSchema.from_orm(c) for c in enrollments]

    # Grades for enrolled courses
    grades_query = (
        db.query(models.Grade)
        .filter(models.Grade.student_id == current_user.id)
        .all()
    )
    grades = [GradeSchema.from_orm(g) for g in grades_query]

    # Recent notifications (limit to latest 10)
    notifications_query = (
        db.query(models.Notification)
        .filter(models.Notification.user_id == current_user.id)
        .order_by(models.Notification.created_at.desc())
        .limit(10)
        .all()
    )
    notifications = [NotificationSchema.from_orm(n) for n in notifications_query]

    return DashboardResponse(
        profile=profile_data,
        courses=courses,
        grades=grades,
        notifications=notifications,
    )
