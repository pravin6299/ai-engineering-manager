from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

from backend import models
from backend.database import get_db
from backend.auth import get_current_user  # assumes existing JWT auth dependency
from pydantic import BaseModel

router = APIRouter(prefix="/api/student", tags=["student"])


class ClassInfo(BaseModel):
    id: int
    name: str

    class Config:
        orm_mode = True


class GradeInfo(BaseModel):
    class_id: int
    grade: float

    class Config:
        orm_mode = True


class StudentProfile(BaseModel):
    id: int
    email: str
    full_name: str

    class Config:
        orm_mode = True


class StudentDashboardResponse(BaseModel):
    student: StudentProfile
    classes: List[ClassInfo]
    grades: List[GradeInfo]

    class Config:
        orm_mode = True


@router.get("/dashboard", response_model=StudentDashboardResponse)
def get_student_dashboard(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Ensure the user is a student (optional, based on role field)
    if getattr(current_user, "role", "student") != "student":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access forbidden: not a student",
        )

    # Student profile
    student_profile = StudentProfile(
        id=current_user.id,
        email=current_user.email,
        full_name=getattr(current_user, "full_name", ""),
    )

    # Enrolled classes
    classes = (
        db.query(models.Class)
        .join(models.Enrollment, models.Class.id == models.Enrollment.class_id)
        .filter(models.Enrollment.student_id == current_user.id)
        .all()
    )
    class_list = [ClassInfo.from_orm(cls) for cls in classes]

    # Grades
    grades_query = (
        db.query(models.Grade, models.Enrollment.class_id)
        .join(models.Enrollment, models.Grade.enrollment_id == models.Enrollment.id)
        .filter(models.Enrollment.student_id == current_user.id)
        .all()
    )
    grade_list = [GradeInfo(class_id=cls_id, grade=grade.value) for grade, cls_id in grades_query]

    return StudentDashboardResponse(
        student=student_profile,
        classes=class_list,
        grades=grade_list,
    )
