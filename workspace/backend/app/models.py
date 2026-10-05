"""SQLAlchemy ORM models and corresponding Pydantic schemas for the backend.

The original implementation was missing several model classes required by the test suite
(`Student`, `StudentProfile`, `Role`, `Course`, `Grade`, `Attendance`).  It also referenced
`BaseModel` without importing it, causing a `NameError` during import time.

This file now defines a minimal yet functional set of ORM models using SQLAlchemy's
`declarative_base` and provides matching Pydantic schemas for data validation/serialization.
Only the fields required by the tests are included; additional fields can be added later
without breaking existing functionality.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# ---------------------------------------------------------------------------
# SQLAlchemy base
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass

# ---------------------------------------------------------------------------
# Association table for many‑to‑many relationship between students and courses
# ---------------------------------------------------------------------------

student_course_association = Table(
    "student_course_association",
    Base.metadata,
    Column("student_id", ForeignKey("students.id"), primary_key=True),
    Column("course_id", ForeignKey("courses.id"), primary_key=True),
)

# ---------------------------------------------------------------------------
# ORM models
# ---------------------------------------------------------------------------

class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)

    users: Mapped[List["User"]] = relationship("User", back_populates="role")

    def __repr__(self) -> str:
        return f"Role(id={self.id!r}, name={self.name!r})"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), nullable=False)

    role: Mapped[Role] = relationship("Role", back_populates="users")
    student: Mapped[Optional["Student"]] = relationship(
        "Student", back_populates="user", uselist=False
    )

    def __repr__(self) -> str:
        return f"User(id={self.id!r}, email={self.email!r})"


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(255))

    students: Mapped[List["Student"]] = relationship(
        "Student",
        secondary=student_course_association,
        back_populates="courses",
    )

    def __repr__(self) -> str:
        return f"Course(id={self.id!r}, name={self.name!r})"


class Grade(Base):
    __tablename__ = "grades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    value: Mapped[str] = mapped_column(String(10), nullable=False)  # e.g., "A", "B+"

    profile: Mapped[Optional["StudentProfile"]] = relationship(
        "StudentProfile", back_populates="grade", uselist=False
    )

    def __repr__(self) -> str:
        return f"Grade(id={self.id!r}, value={self.value!r})"


class Attendance(Base):
    __tablename__ = "attendance"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    present_days: Mapped[int] = mapped_column(Integer, default=0)
    total_days: Mapped[int] = mapped_column(Integer, default=0)

    profile: Mapped[Optional["StudentProfile"]] = relationship(
        "StudentProfile", back_populates="attendance", uselist=False
    )

    def __repr__(self) -> str:
        return (
            f"Attendance(id={self.id!r}, present_days={self.present_days!r}, "
            f"total_days={self.total_days!r})"
        )


class Student(Base):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, unique=True)
    enrollment_date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped[User] = relationship("User", back_populates="student")
    profile: Mapped[Optional["StudentProfile"]] = relationship(
        "StudentProfile", back_populates="student", uselist=False
    )
    courses: Mapped[List[Course]] = relationship(
        "Course",
        secondary=student_course_association,
        back_populates="students",
    )

    def __repr__(self) -> str:
        return f"Student(id={self.id!r}, user_id={self.user_id!r})"


class StudentProfile(Base):
    __tablename__ = "student_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), nullable=False, unique=True)
    grade_id: Mapped[Optional[int]] = mapped_column(ForeignKey("grades.id"))
    attendance_id: Mapped[Optional[int]] = mapped_column(ForeignKey("attendance.id"))
    bio: Mapped[Optional[str]] = mapped_column(String(500))

    student: Mapped[Student] = relationship("Student", back_populates="profile")
    grade: Mapped[Optional[Grade]] = relationship("Grade", back_populates="profile")
    attendance: Mapped[Optional[Attendance]] = relationship(
        "Attendance", back_populates="profile"
    )

    def __repr__(self) -> str:
        return f"StudentProfile(id={self.id!r}, student_id={self.student_id!r})"

# ---------------------------------------------------------------------------
# Pydantic schemas (used by FastAPI endpoints & tests)
# ---------------------------------------------------------------------------

class RoleSchema(BaseModel):
    id: int
    name: str

    class Config:
        from_attributes = True


class UserSchema(BaseModel):
    id: int
    email: str
    is_active: bool
    role: RoleSchema

    class Config:
        from_attributes = True


class CourseSchema(BaseModel):
    id: int
    name: str
    description: Optional[str] = None

    class Config:
        from_attributes = True


class GradeSchema(BaseModel):
    id: int
    value: str

    class Config:
        from_attributes = True


class AttendanceSchema(BaseModel):
    id: int
    present_days: int
    total_days: int

    class Config:
        from_attributes = True


class StudentProfileSchema(BaseModel):
    id: int
    bio: Optional[str] = None
    grade: Optional[GradeSchema] = None
    attendance: Optional[AttendanceSchema] = None

    class Config:
        from_attributes = True


class StudentSchema(BaseModel):
    id: int
    enrollment_date: datetime
    user: UserSchema
    profile: Optional[StudentProfileSchema] = None
    courses: List[CourseSchema] = []

    class Config:
        from_attributes = True
