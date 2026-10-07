from sqlalchemy import Column, Integer, String, ForeignKey, Table, UniqueConstraint
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    role = Column(String, nullable=False)  # e.g., 'admin', 'teacher', 'student'

    # Relationships
    student = relationship("Student", back_populates="user", uselist=False)
    taught_classes = relationship("Class", back_populates="teacher")

    def __repr__(self):
        return f"<User id={self.id} email={self.email} role={self.role}>"

class Student(Base):
    __tablename__ = "students"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True)
    grade_level = Column(String, nullable=False)
    # Additional student-specific fields can be added here (e.g., address, phone)

    # Relationships
    user = relationship("User", back_populates="student")
    enrollments = relationship("Enrollment", back_populates="student", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Student id={self.id} user_id={self.user_id} grade_level={self.grade_level}>"

class Class(Base):
    __tablename__ = "classes"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    teacher_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    # Relationships
    teacher = relationship("User", back_populates="taught_classes")
    enrollments = relationship("Enrollment", back_populates="class_", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Class id={self.id} name={self.name} teacher_id={self.teacher_id}>"

class Enrollment(Base):
    __tablename__ = "enrollments"
    student_id = Column(Integer, ForeignKey("students.id"), primary_key=True)
    class_id = Column(Integer, ForeignKey("classes.id"), primary_key=True)

    # Relationships
    student = relationship("Student", back_populates="enrollments")
    class_ = relationship("Class", back_populates="enrollments")

    __table_args__ = (
        UniqueConstraint('student_id', 'class_id', name='uq_student_class'),
    )

    def __repr__(self):
        return f"<Enrollment student_id={self.student_id} class_id={self.class_id}>"
