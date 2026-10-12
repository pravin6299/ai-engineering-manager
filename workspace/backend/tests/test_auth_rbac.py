import pytest
from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.database import Base, engine
from backend.app.database import SessionLocal
from backend.app.models import User
from backend.app.auth import get_password_hash

@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)

client = TestClient(app)

def test_registration_login_and_rbac():
    # Privileged roles must be provisioned, not selected at public registration.
    res = client.post("/api/auth/register", json={
        "email": "admin@test.com",
        "password": "adminpass",
        "role": "admin"
    })
    assert res.status_code == 403

    res = client.post("/api/auth/register", json={
        "email": "teacher@test.com",
        "password": "teacherpass",
        "role": "teacher"
    })
    assert res.status_code == 403

    with SessionLocal() as db:
        db.add_all([
            User(email="admin@test.com", hashed_password=get_password_hash("adminpass"), role="admin"),
            User(email="teacher@test.com", hashed_password=get_password_hash("teacherpass"), role="teacher"),
        ])
        db.commit()

    # 3. Register Student
    res = client.post("/api/auth/register", json={
        "email": "student@test.com",
        "password": "studentpass",
        "role": "student"
    })
    assert res.status_code == 201
    assert res.json()["role"] == "student"

    # 4. Login Student and test endpoints
    student_login = client.post("/api/auth/login", json={
        "email": "student@test.com",
        "password": "studentpass"
    })
    assert student_login.status_code == 200
    student_token = student_login.json()["access_token"]

    student_headers = {"Authorization": f"Bearer {student_token}"}
    assert client.get("/api/auth/student-only", headers=student_headers).status_code == 200
    assert client.get("/api/auth/teacher-only", headers=student_headers).status_code == 403
    assert client.get("/api/auth/admin-only", headers=student_headers).status_code == 403

    # 5. Login Teacher and test endpoints
    teacher_login = client.post("/api/auth/login", json={
        "email": "teacher@test.com",
        "password": "teacherpass"
    })
    assert teacher_login.status_code == 200
    teacher_token = teacher_login.json()["access_token"]

    teacher_headers = {"Authorization": f"Bearer {teacher_token}"}
    assert client.get("/api/auth/student-only", headers=teacher_headers).status_code == 200
    assert client.get("/api/auth/teacher-only", headers=teacher_headers).status_code == 200
    assert client.get("/api/auth/admin-only", headers=teacher_headers).status_code == 403

    # 6. Login Admin and test endpoints
    admin_login = client.post("/api/auth/login", json={
        "email": "admin@test.com",
        "password": "adminpass"
    })
    assert admin_login.status_code == 200
    admin_token = admin_login.json()["access_token"]

    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    assert client.get("/api/auth/student-only", headers=admin_headers).status_code == 200
    assert client.get("/api/auth/teacher-only", headers=admin_headers).status_code == 200
    assert client.get("/api/auth/admin-only", headers=admin_headers).status_code == 200
