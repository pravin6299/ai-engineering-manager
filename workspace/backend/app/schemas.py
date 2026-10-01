from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List
from datetime import datetime

class UserCreate(BaseModel):
    email: EmailStr
    password: str
    role: Optional[str] = "student"
    is_active: Optional[bool] = True

class UserResponse(BaseModel):
    id: int
    email: EmailStr
    role: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class Token(BaseModel):
    access_token: str
    refresh_token: Optional[str] = None
    token_type: str = "bearer"

class TokenRefreshRequest(BaseModel):
    refresh_token: str

class StudentProfileCreate(BaseModel):
    user_id: int
    first_name: str
    last_name: str
    enrollment_number: str
    grade_level: Optional[str] = None

class StudentProfileUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    enrollment_number: Optional[str] = None
    grade_level: Optional[str] = None

class StudentProfileResponse(BaseModel):
    id: int
    user_id: int
    first_name: str
    last_name: str
    enrollment_number: str
    grade_level: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True

class CourseCreate(BaseModel):
    title: str
    code: str
    description: Optional[str] = None
    teacher_id: Optional[int] = None

class CourseUpdate(BaseModel):
    title: Optional[str] = None
    code: Optional[str] = None
    description: Optional[str] = None
    teacher_id: Optional[int] = None

class CourseResponse(BaseModel):
    id: int
    title: str
    code: str
    description: Optional[str] = None
    teacher_id: Optional[int] = None

    class Config:
        from_attributes = True

class GradeCreate(BaseModel):
    student_id: int
    course_id: int
    score: float
    max_score: float = 100.0
    letter_grade: Optional[str] = None
    term: Optional[str] = None

class GradeUpdate(BaseModel):
    score: Optional[float] = None
    max_score: Optional[float] = None
    letter_grade: Optional[str] = None
    term: Optional[str] = None

class GradeResponse(BaseModel):
    id: int
    student_id: int
    course_id: int
    score: float
    max_score: float
    letter_grade: Optional[str] = None
    term: Optional[str] = None
    date_recorded: datetime

    class Config:
        from_attributes = True

class AttendanceCreate(BaseModel):
    student_id: int
    status: str
    course_name: Optional[str] = None
    date: Optional[datetime] = None

class AttendanceUpdate(BaseModel):
    status: Optional[str] = None
    course_name: Optional[str] = None
    date: Optional[datetime] = None

class AttendanceResponse(BaseModel):
    id: int
    student_id: int
    status: str
    course_name: Optional[str] = None
    date: datetime

    class Config:
        from_attributes = True

class StudentDashboardResponse(BaseModel):
    profile: StudentProfileResponse
    grades: List[GradeResponse]
    attendance: List[AttendanceResponse]
