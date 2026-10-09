"""Database schema for the education platform.

Entity Relationship Diagram (simplified):

User 1---1 StudentProfile
User (id) <--- StudentProfile (user_id)

StudentProfile *---* Class via Enrollment
StudentProfile (id) <--- Enrollment (student_id)
Class (id) <--- Enrollment (class_id)

Tables:
- users: stores authentication data; password stored as secure hash in `password_hash`.
- student_profiles: extended profile for users who are students.
- classes: definition of a class/course.
- enrollments: many‑to‑many link between students and classes.
"""

from datetime import datetime
from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    Text,
    ForeignKey,
    UniqueConstraint,
    Index,
    Table,
)
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()

class User(Base):
    """Core authentication entity.

    Fields:
        id: Primary key.
        email: Unique user email used for login.
        password_hash: Securely hashed password (e.g., bcrypt).
        created_at: Timestamp of record creation.
    """

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), nullable=False, unique=True, index=True)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # One‑to‑one relationship to StudentProfile (optional)
    student_profile = relationship(
        "StudentProfile",
        uselist=False,
        back_populates="user",
        cascade="all, delete-orphan",
    )

class StudentProfile(Base):
    """Extended information for a user who is a student.

    Fields:
        id: Primary key.
        user_id: FK to users.id (one‑to‑one).
        full_name: Student's full name.
        enrollment_date: Date when the student enrolled in the system.
    """

    __tablename__ = "student_profiles"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_student_user_id"),
        Index("ix_student_full_name", "full_name"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    full_name = Column(String(255), nullable=False)
    enrollment_date = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="student_profile")
    classes = relationship(
        "Class",
        secondary="enrollments",
        back_populates="students",
    )

class Class(Base):
    """Represents a class/course offered.

    Fields:
        id: Primary key.
        name: Human readable class name.
        description: Optional detailed description.
        start_date / end_date: Schedule boundaries.
    """

    __tablename__ = "classes"
    __table_args__ = (
        UniqueConstraint("name", name="uq_class_name"),
        Index("ix_class_start_date", "start_date"),
    )

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    start_date = Column(DateTime, nullable=False)
    end_date = Column(DateTime, nullable=False)

    students = relationship(
        "StudentProfile",
        secondary="enrollments",
        back_populates="classes",
    )

# Association table for many‑to‑many relationship between students and classes
enrollments = Table(
    "enrollments",
    Base.metadata,
    Column("student_id", Integer, ForeignKey("student_profiles.id", ondelete="CASCADE"), primary_key=True),
    Column("class_id", Integer, ForeignKey("classes.id", ondelete="CASCADE"), primary_key=True),
    Column("enrolled_at", DateTime, default=datetime.utcnow, nullable=False),
    Index("ix_enrollment_student_class", "student_id", "class_id"),
)
