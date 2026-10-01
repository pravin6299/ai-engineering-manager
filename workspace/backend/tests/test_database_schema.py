import pytest
from sqlalchemy import inspect
from backend.app.database import Base, engine
from backend.app.models import User, Role, Student, Teacher, StudentProfile, Course, Grade, Attendance

@pytest.fixture(scope="function", autouse=True)
def setup_database():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)

def test_database_tables_exist():
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    assert "users" in tables
    assert "roles" in tables
    assert "students" in tables
    assert "teachers" in tables
    assert "student_profiles" in tables
    assert "courses" in tables
    assert "grades" in tables
    assert "attendance" in tables

def test_user_and_student_profile_models():
    from backend.app.database import SessionLocal
    db = SessionLocal()
    try:
        role = Role(name="student", description="Student Role")
        db.add(role)
        db.commit()

        user = User(email="student1@test.com", hashed_password="fakehash", role="student", role_id=role.id)
        db.add(user)
        db.commit()
        db.refresh(user)

        profile = StudentProfile(
            user_id=user.id,
            first_name="John",
            last_name="Doe",
            enrollment_number="EN12345",
            grade_level="10"
        )
        db.add(profile)
        db.commit()
        db.refresh(profile)

        assert profile.id is not None
        assert profile.user_id == user.id
        assert profile.user.email == "student1@test.com"
        assert user.student_profile_alias.first_name == "John"
    finally:
        db.close()
