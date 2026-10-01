import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.main import app
from backend.app.database import Base, get_db

SQLALCHEMY_DATABASE_URL = "sqlite:///./test_student_data_api.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)

@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)

def get_auth_token(email, password, role="admin"):
    client.post("/api/auth/register", json={"email": email, "password": password, "role": role})
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    return response.json()["access_token"]

def test_student_profiles_crud_and_dashboard():
    admin_token = get_auth_token("admin@test.com", "password123", "admin")
    student_token = get_auth_token("student@test.com", "password123", "student")
    
    # Get user ids
    res = client.get("/api/auth/me", headers={"Authorization": f"Bearer {student_token}"})
    student_user_id = res.json()["id"]
    
    # Create student profile
    profile_res = client.post(
        "/api/management/students/profiles",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "user_id": student_user_id,
            "first_name": "John",
            "last_name": "Doe",
            "enrollment_number": "EN12345",
            "grade_level": "10"
        }
    )
    assert profile_res.status_code == 201
    profile_id = profile_res.json()["id"]
    
    # Create Course
    course_res = client.post(
        "/api/management/courses",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "title": "Mathematics",
            "code": "MATH101",
            "description": "Basic Math"
        }
    )
    assert course_res.status_code == 201
    course_id = course_res.json()["id"]
    
    # Create Grade
    grade_res = client.post(
        "/api/management/grades",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "student_id": profile_id,
            "course_id": course_id,
            "score": 95.0,
            "max_score": 100.0,
            "letter_grade": "A",
            "term": "Fall"
        }
    )
    assert grade_res.status_code == 201
    
    # Create Attendance
    att_res = client.post(
        "/api/management/attendance",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "student_id": profile_id,
            "status": "Present",
            "course_name": "Mathematics"
        }
    )
    assert att_res.status_code == 201
    
    # Test Student Dashboard API
    dash_res = client.get(
        "/api/students/dashboard",
        headers={"Authorization": f"Bearer {student_token}"}
    )
    assert dash_res.status_code == 200
    data = dash_res.json()
    assert data["profile"]["enrollment_number"] == "EN12345"
    assert len(data["grades"]) == 1
    assert data["grades"][0]["score"] == 95.0
    assert len(data["attendance"]) == 1
    assert data["attendance"][0]["status"] == "Present"
