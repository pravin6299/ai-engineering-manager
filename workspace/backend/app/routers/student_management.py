from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

from backend.app.database import get_db
from backend.app.models import User, StudentProfile, Grade, Attendance, Course
from backend.app.schemas import (
    StudentProfileCreate,
    StudentProfileUpdate,
    StudentProfileResponse,
    CourseCreate,
    CourseUpdate,
    CourseResponse,
    GradeCreate,
    GradeUpdate,
    GradeResponse,
    AttendanceCreate,
    AttendanceUpdate,
    AttendanceResponse
)
from backend.app.auth import get_current_user, require_role

router = APIRouter(prefix="/api/management", tags=["student-management"])

# --- Student Profiles CRUD ---

@router.post("/students/profiles", response_model=StudentProfileResponse, status_code=status.HTTP_201_CREATED)
def create_student_profile(
    profile_in: StudentProfileCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "teacher"])) 
):
    user = db.query(User).filter(User.id == profile_in.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Associated user not found")
    existing = db.query(StudentProfile).filter(StudentProfile.user_id == profile_in.user_id).first()
    if existing:
        raise HTTPException(status_code=400, detail="Student profile already exists for this user")
    
    profile = StudentProfile(
        user_id=profile_in.user_id,
        first_name=profile_in.first_name,
        last_name=profile_in.last_name,
        enrollment_number=profile_in.enrollment_number,
        grade_level=profile_in.grade_level
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile

@router.get("/students/profiles", response_model=List[StudentProfileResponse])
def list_student_profiles(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "teacher"]))
):
    return db.query(StudentProfile).offset(skip).limit(limit).all()

@router.get("/students/profiles/{profile_id}", response_model=StudentProfileResponse)
def get_student_profile_by_id(
    profile_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    profile = db.query(StudentProfile).filter(StudentProfile.id == profile_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Student profile not found")
    if current_user.role == "student" and profile.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Unauthorized to view other student profiles")
    return profile

@router.put("/students/profiles/{profile_id}", response_model=StudentProfileResponse)
def update_student_profile(
    profile_id: int,
    profile_in: StudentProfileUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "teacher"]))
):
    profile = db.query(StudentProfile).filter(StudentProfile.id == profile_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Student profile not found")
    
    update_data = profile_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(profile, field, value)
    
    db.commit()
    db.refresh(profile)
    return profile

@router.delete("/students/profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_student_profile(
    profile_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["admin"]))
):
    profile = db.query(StudentProfile).filter(StudentProfile.id == profile_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Student profile not found")
    db.delete(profile)
    db.commit()
    return None

# --- Grades CRUD ---

@router.post("/grades", response_model=GradeResponse, status_code=status.HTTP_201_CREATED)
def create_grade(
    grade_in: GradeCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "teacher"]))
):
    student = db.query(StudentProfile).filter(StudentProfile.id == grade_in.student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student profile not found")
    course = db.query(Course).filter(Course.id == grade_in.course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    grade = Grade(**grade_in.model_dump())
    db.add(grade)
    db.commit()
    db.refresh(grade)
    return grade

@router.get("/grades", response_model=List[GradeResponse])
def list_grades(
    skip: int = 0,
    limit: int = 100,
    student_id: int = None,
    course_id: int = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(Grade)
    if student_id is not None:
        query = query.filter(Grade.student_id == student_id)
    if course_id is not None:
        query = query.filter(Grade.course_id == course_id)
    return query.offset(skip).limit(limit).all()

@router.get("/grades/{grade_id}", response_model=GradeResponse)
def get_grade(
    grade_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    grade = db.query(Grade).filter(Grade.id == grade_id).first()
    if not grade:
        raise HTTPException(status_code=404, detail="Grade not found")
    if current_user.role == "student":
        profile = db.query(StudentProfile).filter(StudentProfile.user_id == current_user.id).first()
        if not profile or grade.student_id != profile.id:
            raise HTTPException(status_code=403, detail="Unauthorized to view this grade")
    return grade

@router.put("/grades/{grade_id}", response_model=GradeResponse)
def update_grade(
    grade_id: int,
    grade_in: GradeUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "teacher"]))
):
    grade = db.query(Grade).filter(Grade.id == grade_id).first()
    if not grade:
        raise HTTPException(status_code=404, detail="Grade not found")
    
    update_data = grade_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(grade, field, value)
    
    db.commit()
    db.refresh(grade)
    return grade

@router.delete("/grades/{grade_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_grade(
    grade_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "teacher"]))
):
    grade = db.query(Grade).filter(Grade.id == grade_id).first()
    if not grade:
        raise HTTPException(status_code=404, detail="Grade not found")
    db.delete(grade)
    db.commit()
    return None

# --- Attendance CRUD ---

@router.post("/attendance", response_model=AttendanceResponse, status_code=status.HTTP_201_CREATED)
def create_attendance(
    att_in: AttendanceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "teacher"]))
):
    student = db.query(StudentProfile).filter(StudentProfile.id == att_in.student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student profile not found")
    
    attendance = Attendance(**att_in.model_dump())
    db.add(attendance)
    db.commit()
    db.refresh(attendance)
    return attendance

@router.get("/attendance", response_model=List[AttendanceResponse])
def list_attendance(
    skip: int = 0,
    limit: int = 100,
    student_id: int = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(Attendance)
    if student_id is not None:
        query = query.filter(Attendance.student_id == student_id)
    return query.offset(skip).limit(limit).all()

@router.get("/attendance/{attendance_id}", response_model=AttendanceResponse)
def get_attendance(
    attendance_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    att = db.query(Attendance).filter(Attendance.id == attendance_id).first()
    if not att:
        raise HTTPException(status_code=404, detail="Attendance record not found")
    if current_user.role == "student":
        profile = db.query(StudentProfile).filter(StudentProfile.user_id == current_user.id).first()
        if not profile or att.student_id != profile.id:
            raise HTTPException(status_code=403, detail="Unauthorized to view this attendance record")
    return att

@router.put("/attendance/{attendance_id}", response_model=AttendanceResponse)
def update_attendance(
    attendance_id: int,
    att_in: AttendanceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "teacher"]))
):
    att = db.query(Attendance).filter(Attendance.id == attendance_id).first()
    if not att:
        raise HTTPException(status_code=404, detail="Attendance record not found")
    
    update_data = att_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(att, field, value)
    
    db.commit()
    db.refresh(att)
    return att

@router.delete("/attendance/{attendance_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_attendance(
    attendance_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "teacher"]))
):
    att = db.query(Attendance).filter(Attendance.id == attendance_id).first()
    if not att:
        raise HTTPException(status_code=404, detail="Attendance record not found")
    db.delete(att)
    db.commit()
    return None

# --- Courses CRUD ---

@router.post("/courses", response_model=CourseResponse, status_code=status.HTTP_201_CREATED)
def create_course(
    course_in: CourseCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "teacher"]))
):
    existing = db.query(Course).filter(Course.code == course_in.code).first()
    if existing:
        raise HTTPException(status_code=400, detail="Course with this code already exists")
    
    course = Course(**course_in.model_dump())
    db.add(course)
    db.commit()
    db.refresh(course)
    return course

@router.get("/courses", response_model=List[CourseResponse])
def list_courses(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return db.query(Course).offset(skip).limit(limit).all()

@router.get("/courses/{course_id}", response_model=CourseResponse)
def get_course(
    course_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    return course

@router.put("/courses/{course_id}", response_model=CourseResponse)
def update_course(
    course_id: int,
    course_in: CourseUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "teacher"]))
):
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    
    update_data = course_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(course, field, value)
    
    db.commit()
    db.refresh(course)
    return course

@router.delete("/courses/{course_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_course(
    course_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["admin"]))
):
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    db.delete(course)
    db.commit()
    return None
