from sqlalchemy import Column, Integer, String, ForeignKey, Table
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()

# Association tables
student_class_association = Table(
    'student_class_association',
    Base.metadata,
    Column('student_id', Integer, ForeignKey('students.id'), primary_key=True),
    Column('class_id', Integer, ForeignKey('classes.id'), primary_key=True)
)

teacher_class_association = Table(
    'teacher_class_association',
    Base.metadata,
    Column('teacher_id', Integer, ForeignKey('teachers.id'), primary_key=True),
    Column('class_id', Integer, ForeignKey('classes.id'), primary_key=True)
)

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    role = Column(String, nullable=False)  # e.g., 'student', 'teacher', 'admin'

    student = relationship('Student', uselist=False, back_populates='user')
    teacher = relationship('Teacher', uselist=False, back_populates='user')

class Student(Base):
    __tablename__ = 'students'
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), unique=True, nullable=False)
    enrollment_year = Column(Integer, nullable=True)

    user = relationship('User', back_populates='student')
    classes = relationship('Class', secondary=student_class_association, back_populates='students')

class Teacher(Base):
    __tablename__ = 'teachers'
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), unique=True, nullable=False)
    hire_date = Column(String, nullable=True)

    user = relationship('User', back_populates='teacher')
    classes = relationship('Class', secondary=teacher_class_association, back_populates='teachers')

class Class(Base):
    __tablename__ = 'classes'
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)

    students = relationship('Student', secondary=student_class_association, back_populates='classes')
    teachers = relationship('Teacher', secondary=teacher_class_association, back_populates='classes')
