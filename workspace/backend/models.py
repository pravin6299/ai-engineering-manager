from sqlalchemy import Column, Integer, String, ForeignKey, Table, Boolean, DateTime, func
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()

# Association table for many-to-many relationship between students and courses (enrollments)
enrollment_table = Table(
    "enrollments",
    Base.metadata,
    Column("student_id", Integer, ForeignKey("students.id"), primary_key=True),
    Column("course_id", Integer, ForeignKey("courses.id"), primary_key=True),
    Column("enrolled_at", DateTime(timezone=True), server_default=func.now()),
)

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    is_active = Column(Boolean, default=True)
    is_superuser = Column(Boolean, default=False)
    # Relationship to Student (one-to-one optional)
    student = relationship("Student", back_populates="user", uselist=False)

class Student(Base):
    __tablename__ = "students"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True)
    full_name = Column(String, nullable=False)
    # Relationship back to User
    user = relationship("User", back_populates="student")
    # Enrolled courses via association table
    courses = relationship(
        "Course",
        secondary=enrollment_table,
        back_populates="students",
    )

class Course(Base):
    __tablename__ = "courses"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(String, nullable=True)
    # Enrolled students via association table
    students = relationship(
        "Student",
        secondary=enrollment_table,
        back_populates="courses",
    )
