from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Table, Float
from sqlalchemy.orm import relationship
from datetime import datetime
from backend.app.database import Base

student_courses = Table(
    "student_courses",
    Base.metadata,
    Column("student_id", Integer, ForeignKey("students.id"), primary_key=True),
    Column("course_id", Integer, ForeignKey("courses.id"), primary_key=True),
)

class Role(Base):
    __tablename__ = "roles"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    description = Column(String, nullable=True)

    users = relationship("User", back_populates="role_rel")

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    role = Column(String, default="student", nullable=False)
    role_id = Column(Integer, ForeignKey("roles.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    role_rel = relationship("Role", back_populates="users")
    student_profile = relationship("Student", back_populates="user", uselist=False, cascade="all, delete-orphan")
    teacher_profile = relationship("Teacher", back_populates="user", uselist=False, cascade="all, delete-orphan")
    student_profile_alias = relationship("StudentProfile", back_populates="user", uselist=False, cascade="all, delete-orphan")

class Student(Base):
    __tablename__ = "students"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    first_name = Column(String, nullable=False)
    last_name = Column(String, nullable=False)
    enrollment_number = Column(String, unique=True, index=True, nullable=False)
    grade_level = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="student_profile")
    courses = relationship("Course", secondary=student_courses, back_populates="students")

class Teacher(Base):
    __tablename__ = "teachers"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    first_name = Column(String, nullable=False)
    last_name = Column(String, nullable=False)
    department = Column(String, nullable=True)
    employee_id = Column(String, unique=True, index=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="teacher_profile")
    courses = relationship("Course", back_populates="teacher")

class Course(Base):
    __tablename__ = "courses"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False, index=True)
    description = Column(String, nullable=True)
    code = Column(String, unique=True, index=True, nullable=False)
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=True)

    teacher = relationship("Teacher", back_populates="courses")
    students = relationship("Student", secondary=student_courses, back_populates="courses")

class StudentProfile(Base):
    __tablename__ = "student_profiles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    first_name = Column(String, nullable=False)
    last_name = Column(String, nullable=False)
    enrollment_number = Column(String, unique=True, index=True, nullable=False)
    grade_level = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="student_profile_alias")
    grades = relationship("Grade", back_populates="student", cascade="all, delete-orphan")
    attendance = relationship("Attendance", back_populates="student", cascade="all, delete-orphan")

class Grade(Base):
    __tablename__ = "grades"

    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("student_profiles.id"), nullable=False)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    score = Column(Float, nullable=False)
    max_score = Column(Float, nullable=False, default=100.0)
    letter_grade = Column(String, nullable=True)
    term = Column(String, nullable=True)
    date_recorded = Column(DateTime, default=datetime.utcnow, nullable=False)

    student = relationship("StudentProfile", back_populates="grades")

class Attendance(Base):
    __tablename__ = "attendance"

    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("student_profiles.id"), nullable=False)
    date = Column(DateTime, default=datetime.utcnow, nullable=False)
    status = Column(String, nullable=False)
    course_name = Column(String, nullable=True)

    student = relationship("StudentProfile", back_populates="attendance")
