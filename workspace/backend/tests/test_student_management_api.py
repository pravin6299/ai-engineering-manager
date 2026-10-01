import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.main import app
from backend.app.database import Base, get_db

SQLALCHEMY_DATABASE_URL = "sqlite:///./test_student_management.db"
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
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)

def get_token(email, password, role="admin"):
    client.post("/api/auth/register", json={"email": email, "password": password, "role": role})
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    return res.json()["access_token"]

def test_student_management_crud():
    admin_token = get_token("admin@school.com", "secret123", "admin")
    teacher_token = get_token("teacher@school.com", "secret123", "teacher")
    student_token = get_token("student@school.com", "secret123", "student")

    # Get student user ID
    me_res = client.get("/api/auth/me", headers={"Authorization": f"Bearer {student_token}"})
    student_user_id = me_res.json()["id"]

    # 1. Create Student Profile (Teacher/Admin)
    profile_res = client.post(
        "/api/management/students/profiles",
        json={
            "user_id": student_user_id,
            "first_name": "John",
            "last_name": "Doe",
            "enrollment_number": "ENR12345",
            "grade_level": "10"
        },
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert profile_res.status_code == 201
    profile_id = profile_res.json()["id"]

    # 2. Create Course
    course_res = client.post(
        "/api/management/courses",
        json={
            "title": "Mathematics 101",
            "code": "MATH101",
            "description": "Introductory mathematics"
        },
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert course_res.status_code == 201
    course_id = course_res.json()["id"]

    # 3. Create Grade
    grade_res = client.post(
        "/api/management/students/grades",
        json={
            "student_id": profile_id,
            "course_id": course_id,
            "score": 95.5,
            "max_score": 100.0,
            "letter_grade": "A",
            "term": "Fall"
        },
        headers={"Authorization": f"Bearer {teacher_token}"}
    )
    assert grade_res.status_code == 201
    grade_id = grade_res.json()["id"]

    # 4. Create Attendance
    att_res = client.post(
        "/api/management/students/attendance",
        json={
            "student_id": profile_id,
            "status": "Present",
            "course_name": "Mathematics 101"
        },
        headers={"Authorization": f"Bearer {teacher_token}"}
    )
    assert att_res.status_code == 201
    att_id = att_res.json()["id"]

    # 5. Read endpoints / student dashboard verification
    dash_res = client.get("/api/students/dashboard", headers={"Authorization": f"Bearer {student_token}"})
    assert dash_res.status_code == 200
    data = dash_res.json()
    assert data["profile"]["first_name"] == "John"
    assert len(data["grades"]) == 1
    assert len(data["attendance"]) == 1

    # 6. Update Profile
    upd_res = client.put(
        f"/api/management/students/profiles/{profile_id}",
        json={"grade_level": "11"},
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert upd_res.status_code == 200
    assert upd_res.json()["grade_level"] == "11"

    # 7. Delete Grade
    del_grade = client.delete(
        f"/api/management/students/grades/{grade_id}",
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert del_grade.status_code == 204
