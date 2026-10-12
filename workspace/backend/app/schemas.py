from pydantic import BaseModel, EmailStr
from typing import Optional

class UserBase(BaseModel):
    email: EmailStr
    full_name: Optional[str] = None

class UserCreate(UserBase):
    password: str

class UserOut(UserBase):
    id: int
    role: str

    class Config:
        orm_mode = True


class OrmResponse(BaseModel):
    class Config:
        from_attributes = True


class StudentProfileCreate(BaseModel):
    user_id: int
    first_name: str
    last_name: str
    enrollment_number: str
    grade_level: str | None = None


class StudentProfileUpdate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    enrollment_number: str | None = None
    grade_level: str | None = None


class StudentProfileResponse(OrmResponse, StudentProfileCreate):
    id: int


class CourseCreate(BaseModel):
    title: str
    code: str
    description: str | None = None


class CourseUpdate(BaseModel):
    title: str | None = None
    code: str | None = None
    description: str | None = None


class CourseResponse(OrmResponse, CourseCreate):
    id: int


class GradeCreate(BaseModel):
    student_id: int
    course_id: int
    score: float
    max_score: float
    letter_grade: str | None = None
    term: str | None = None


class GradeUpdate(BaseModel):
    score: float | None = None
    max_score: float | None = None
    letter_grade: str | None = None
    term: str | None = None


class GradeResponse(OrmResponse, GradeCreate):
    id: int


class AttendanceCreate(BaseModel):
    student_id: int
    status: str
    course_name: str | None = None


class AttendanceUpdate(BaseModel):
    status: str | None = None
    course_name: str | None = None


class AttendanceResponse(OrmResponse, AttendanceCreate):
    id: int


class StudentDashboardResponse(BaseModel):
    profile: StudentProfileResponse
    grades: list[GradeResponse]
    attendance: list[AttendanceResponse]
