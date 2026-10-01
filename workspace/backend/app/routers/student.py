from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

from backend.app.database import get_db
from backend.app.models import User, StudentProfile, Grade, Attendance, Course
from backend.app.schemas import StudentDashboardResponse, GradeResponse, AttendanceResponse, StudentProfileResponse, CourseResponse
from backend.app.auth import get_current_user

router = APIRouter(prefix="/api/students", tags=["students"])

def verify_student_role(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "student" and current_user.role != "admin" and current_user.role != "teacher":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access student resources"
        )
    return current_user

@router.get("/dashboard", response_model=StudentDashboardResponse)
def get_student_dashboard(db: Session = Depends(get_db), current_user: User = Depends(verify_student_role)):
    profile = db.query(StudentProfile).filter(StudentProfile.user_id == current_user.id).first()
    if not profile:
        # If StudentProfile is missing, check old Student model or create a dummy/error
        raise HTTPException(status_code=404, detail="Student profile not found for current user")
    
    grades = db.query(Grade).filter(Grade.student_id == profile.id).all()
    attendance = db.query(Attendance).filter(Attendance.student_id == profile.id).all()
    
    return {
        "profile": profile,
        "grades": grades,
        "attendance": attendance
    }

@router.get("/grades", response_model=List[GradeResponse])
def get_student_grades(db: Session = Depends(get_db), current_user: User = Depends(verify_student_role)):
    profile = db.query(StudentProfile).filter(StudentProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Student profile not found")
    return db.query(Grade).filter(Grade.student_id == profile.id).all()

@router.get("/attendance", response_model=List[AttendanceResponse])
def get_student_attendance(db: Session = Depends(get_db), current_user: User = Depends(verify_student_role)):
    profile = db.query(StudentProfile).filter(StudentProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Student profile not found")
    return db.query(Attendance).filter(Attendance.student_id == profile.id).all()

@router.get("/profile", response_model=StudentProfileResponse)
def get_student_profile(db: Session = Depends(get_db), current_user: User = Depends(verify_student_role)):
    profile = db.query(StudentProfile).filter(StudentProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Student profile not found")
    return profile

@router.get("/courses", response_model=List[CourseResponse])
def get_student_enrolled_courses(db: Session = Depends(get_db), current_user: User = Depends(verify_student_role)):
    # Check student_profiles mapping or legacy student courses
    # For Task-002, we can fetch courses via Student model or student_courses table
    student_legacy = db.query(Student).filter(Student.user_id == current_user.id).first()
    if student_legacy:
        return student_legacy.courses
    
    # Alternatively check if student_profiles can access courses. Let's return empty list or linked courses if any.
    return []
