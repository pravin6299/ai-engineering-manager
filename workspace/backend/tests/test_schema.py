import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from ..main import app
from ..database import Base, engine, SessionLocal
from .. import models

client = TestClient(app)

@pytest.fixture(scope="function", autouse=True)
def reset_database():
    # Drop and recreate all tables for each test to ensure isolation
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)

def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

def test_schema_tables_endpoint():
    response = client.get("/schema/tables")
    assert response.status_code == 200
    data = response.json()
    assert "tables" in data
    # Expected tables based on models
    expected_tables = {"users", "students", "courses", "enrollments"}
    assert expected_tables.issubset(set(data["tables"]))

def test_relationships_work():
    db = SessionLocal()
    # Create a user and linked student
    user = models.User(username="jdoe", email="jdoe@example.com")
    db.add(user)
    db.flush()  # assign id
    student = models.Student(user_id=user.id, major="Computer Science")
    db.add(student)
    # Create a course
    course = models.Course(title="Intro to Python", description="Learn Python basics")
    db.add(course)
    db.flush()
    # Enroll student in course
    enrollment = models.Enrollment(student_id=student.id, course_id=course.id)
    db.add(enrollment)
    db.commit()

    # Verify relationships via ORM
    db.refresh(student)
    assert len(student.enrollments) == 1
    assert student.enrollments[0].course.title == "Intro to Python"
    db.refresh(course)
    assert len(course.enrollments) == 1
    assert course.enrollments[0].student.user.username == "jdoe"
    db.close()
