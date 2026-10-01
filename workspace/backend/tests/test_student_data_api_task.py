import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.main import app
from backend.app.database import Base, get_db
from backend.app.models import User, StudentProfile, Grade, Attendance, Course
from backend.app.auth import get_password_hash, create_access_token

SQLALCHEMY_DATABASE_URL = "sqlite:///./test_student_data_api_task.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db

@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)

client = TestClient(app)

def test_get_student_profile_grades_attendance():
    db = TestingSessionLocal()
    
    # Create test user with student role
    user = User(
        email="student@example.com",
        hashed_password=get_password_hash("password123"),
        role="student"
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    
    # Create student profile
    profile = StudentProfile(
        user_id=user.id,
        first_name="Alice",
        last_name="Smith",
        enrollment_number="EN12345",
        grade_level="10"
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    
    # Create course
    course = Course(
        title="Mathematics",
        code="MATH101",
        description="Algebra and Geometry"
    )
    db.add(course)
    db.commit()
    db.refresh(course)
    
    # Create grade
    grade = Grade(
        student_id=profile.id,
        course_id=course.id,
        score=95.0,
        max_score=100.0,
        letter_grade="A",
        term="Fall"
    )
    db.add(grade)
    
    # Create attendance
    attendance = Attendance(
        student_id=profile.id,
        status="Present",
        course_name="Mathematics"
    )
    db.add(attendance)
    db.commit()
    db.close()
    
    # Generate token
    token = create_access_token({"sub": "student@example.com", "role": "student"})
    headers = {"Authorization": f"Bearer {token}"}
    
    # Test profile endpoint
    response = client.get("/api/students/profile", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["first_name"] == "Alice"
    assert data["enrollment_number"] == "EN12345"
    
    # Test grades endpoint
    response = client.get("/api/students/grades", headers=headers)
    assert response.status_code == 200
    grades_data = response.json()
    assert len(grades_data) == 1
    assert grades_data[0]["score"] == 95.0
    assert grades_data[0]["letter_grade"] == "A"
    
    # Test attendance endpoint
    response = client.get("/api/students/attendance", headers=headers)
    assert response.status_code == 200
    att_data = response.json()
    assert len(att_data) == 1
    assert att_data[0]["status"] == "Present"
    
    # Test dashboard endpoint
    response = client.get("/api/students/dashboard", headers=headers)
    assert response.status_code == 200
    dash_data = response.json()
    assert dash_data["profile"]["first_name"] == "Alice"
    assert len(dash_data["grades"]) == 1
    assert len(dash_data["attendance"]) == 1
