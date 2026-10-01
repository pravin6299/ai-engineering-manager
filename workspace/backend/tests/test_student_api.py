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
    
    # Create a student user and profile with grades and attendance
    hashed_pwd = get_password_hash("password123")
    user = User(email="student@test.com", hashed_password=hashed_pwd, role="student")
    db.add(user)
    db.commit()
    db.refresh(user)
    
    profile = StudentProfile(
        user_id=user.id,
        first_name="Jane",
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
        course_name="Mathematics"
    )
    db.add(grade)
    db.add(attendance)
    db.commit()
    db.close()
    yield
    Base.metadata.drop_all(bind=engine)

client = TestClient(app)

def test_get_student_dashboard_success():
    # Login to get token
    response = client.post("/api/auth/login", json={"email": "student@test.com", "password": "password123"})
    assert response.status_code == 200
    token = response.json()["access_token"]
    
    # Fetch student dashboard
    headers = {"Authorization": f"Bearer {token}"}
    res = client.get("/api/students/dashboard", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["profile"]["first_name"] == "Jane"
    assert len(data["grades"]) == 1
    assert data["grades"][0]["score"] == 95.0
    assert len(data["attendance"]) == 1
    assert data["attendance"][0]["status"] == "Present"

def test_get_student_grades_unauthorized():
    # Access without token should fail
    res = client.get("/api/students/grades")
    assert res.status_code == 401

def test_get_student_profile_success():
    response = client.post("/api/auth/login", json={"email": "student@test.com", "password": "password123"})
    token = response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    
    res = client.get("/api/students/profile", headers=headers)
    assert res.status_code == 200
    assert res.json()["enrollment_number"] == "EN12345"
