import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.main import app
from backend.app.database import Base, get_db
from backend.app.models import User, StudentProfile, Grade, Attendance
from backend.app.auth import get_password_hash

SQLALCHEMY_DATABASE_URL = "sqlite:///./test_student_api_task.db"
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
    
    # Create a student user and profile
    hashed_pwd = get_password_hash("password123")
    user = User(email="student@school.com", hashed_password=hashed_pwd, role="student")
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
    
    grade = Grade(
        student_id=profile.id,
        course_id=1,
        score=95.0,
        max_score=100.0,
        letter_grade="A",
        term="Fall"
    )
    attendance = Attendance(
        student_id=profile.id,
        status="Present",
        course_name="Math"
    )
    db.add(grade)
    db.add(attendance)
    db.commit()
    db.close()
    yield
    Base.metadata.drop_all(bind=engine)

client = TestClient(app)

def get_auth_token():
    response = client.post("/api/auth/login", json={"email": "student@school.com", "password": "password123"})
    return response.json()["access_token"]

def test_get_student_dashboard():
    token = get_auth_token()
    headers = {"Authorization": f"Bearer {token}"}
    response = client.get("/api/students/dashboard", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert "profile" in data
    assert "grades" in data
    assert "attendance" in data
    assert data["profile"]["first_name"] == "John"
    assert len(data["grades"]) == 1
    assert len(data["attendance"]) == 1

def test_get_student_grades():
    token = get_auth_token()
    headers = {"Authorization": f"Bearer {token}"}
    response = client.get("/api/students/grades", headers=headers)
    assert response.status_code == 200
    grades = response.json()
    assert len(grades) == 1
    assert grades[0]["score"] == 95.0

def test_get_student_attendance():
    token = get_auth_token()
    headers = {"Authorization": f"Bearer {token}"}
    response = client.get("/api/students/attendance", headers=headers)
    assert response.status_code == 200
    attendance = response.json()
    assert len(attendance) == 1
    assert attendance[0]["status"] == "Present"

def test_get_student_profile():
    token = get_auth_token()
    headers = {"Authorization": f"Bearer {token}"}
    response = client.get("/api/students/profile", headers=headers)
    assert response.status_code == 200
    profile = response.json()
    assert profile["enrollment_number"] == "EN12345"
