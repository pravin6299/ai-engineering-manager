import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.main import app
from backend.app.database import Base, get_db
from backend.app.models import User, StudentProfile, Grade, Attendance, Course
from backend.app.auth import get_password_hash

SQLALCHEMY_DATABASE_URL = "sqlite:///./test_student_dashboard.db"
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
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    
    # Create student user & profile
    student_user = User(email="student@school.com", hashed_password=get_password_hash("password123"), role="student")
    db.add(student_user)
    db.commit()
    db.refresh(student_user)

    profile = StudentProfile(user_id=student_user.id, first_name="Alice", last_name="Smith", enrollment_number="STU001", grade_level="10")
    db.add(profile)
    db.commit()
    db.refresh(profile)

    course = Course(title="Mathematics", code="MATH101", description="Algebra & Geometry")
    db.add(course)
    db.commit()
    db.refresh(course)

    grade = Grade(student_id=profile.id, course_id=course.id, score=95.0, max_score=100.0, letter_grade="A", term="Fall")
    attendance = Attendance(student_id=profile.id, status="Present", course_name="Mathematics")
    db.add(grade)
    db.add(attendance)
    db.commit()
    db.close()
    yield
    Base.metadata.drop_all(bind=engine)

client = TestClient(app)

def test_get_student_dashboard_success():
    # Login as student
    response = client.post("/api/auth/login", json={"email": "student@school.com", "password": "password123"})
    assert response.status_code == 200
    token = response.json()["access_token"]

    headers = {"Authorization": f"Bearer {token}"}
    dash_response = client.get("/api/students/dashboard", headers=headers)
    assert dash_response.status_code == 200
    data = dash_response.json()
    assert data["profile"]["first_name"] == "Alice"
    assert len(data["grades"]) == 1
    assert data["grades"][0]["score"] == 95.0
    assert len(data["attendance"]) == 1
    assert data["attendance"][0]["status"] == "Present"

def test_get_student_grades():
    response = client.post("/api/auth/login", json={"email": "student@school.com", "password": "password123"})
    token = response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    
    grades_response = client.get("/api/students/grades", headers=headers)
    assert grades_response.status_code == 200
    assert len(grades_response.json()) == 1
    assert grades_response.json()[0]["letter_grade"] == "A"

def test_get_student_attendance():
    response = client.post("/api/auth/login", json={"email": "student@school.com", "password": "password123"})
    token = response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    
    att_response = client.get("/api/students/attendance", headers=headers)
    assert att_response.status_code == 200
    assert len(att_response.json()) == 1
    assert att_response.json()[0]["status"] == "Present"
