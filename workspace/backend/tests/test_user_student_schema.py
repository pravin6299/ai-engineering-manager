import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.database import Base
from backend.app.models import User, Student, Role, Course, Grade, Attendance

SQLALCHEMY_DATABASE_URL = "sqlite://"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

@pytest.fixture(scope="function")
def db_session():
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)

def test_schema_creation_and_relationships(db_session):
    role_admin = Role(name="admin", description="Administrator role")
    role_student = Role(name="student", description="Student role")
    db_session.add_all([role_admin, role_student])
    db_session.commit()

    user = User(email="student@school.com", hashed_password="fakepass", role="student", role_id=role_student.id)
    db_session.add(user)
    db_session.commit()

    student = Student(
        user_id=user.id,
        first_name="John",
        last_name="Doe",
        enrollment_number="ENR12345",
        grade_level="10"
    )
    db_session.add(student)
    db_session.commit()

    fetched_user = db_session.query(User).filter(User.email == "student@school.com").first()
    assert fetched_user is not None
    assert fetched_user.student_profile is not None
    assert fetched_user.student_profile.first_name == "John"
    assert fetched_user.student_profile.enrollment_number == "ENR12345"

    fetched_student = db_session.query(Student).filter(Student.enrollment_number == "ENR12345").first()
    assert fetched_student is not None
    assert fetched_student.user.email == "student@school.com"
