'''SQLAlchemy ORM models for the school management system.

This file defines the `User` and `Student` models that map to the
corresponding tables created by the migration script.
''' 

from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Enum
from sqlalchemy.orm import relationship, declarative_base
import enum

Base = declarative_base()


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    TEACHER = "teacher"
    STUDENT = "student"
    STAFF = "staff"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(Enum(UserRole), nullable=False)

    # One‑to‑one relationship with Student (if role == student)
    student = relationship("Student", back_populates="user", uselist=False)

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email} role={self.role}>"


class Student(Base):
    __tablename__ = "students"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    name = Column(String(255), nullable=False)
    class_name = Column(String(50), nullable=False)  # e.g., "10A"
    enrollment_date = Column(DateTime, default=datetime.utcnow, nullable=False)
    # Additional optional fields can be added later (e.g., date_of_birth)

    user = relationship("User", back_populates="student")

    def __repr__(self) -> str:
        return f"<Student id={self.id} name={self.name} class={self.class_name}>"
