import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.main import app
from backend.app.database import Base, get_db

SQLALCHEMY_DATABASE_URL = "sqlite:///./test_auth_api_task.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base.metadata.create_all(bind=engine)

def override_get_db():
    try:
        db = TestingSessionLocal()
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db

client = TestClient(app)

def test_register_and_login_flow():
    # Register new user
    response = client.post(
        "/api/auth/register",
        json={"email": "testuser@example.com", "password": "secretpassword", "role": "student"}
    )
    assert response.status_code == 201
    data = response.json()
    assert data["email"] == "testuser@example.com"
    assert data["role"] == "student"

    # Login with registered credentials
    response = client.post(
        "/api/auth/login",
        json={"email": "testuser@example.com", "password": "secretpassword"}
    )
    assert response.status_code == 200
    token_data = response.json()
    assert "access_token" in token_data
    assert token_data["token_type"] == "bearer"

    # Access /me using access token
    access_token = token_data["access_token"]
    response = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    assert response.status_code == 200
    me_data = response.json()
    assert me_data["email"] == "testuser@example.com"

def test_login_invalid_credentials():
    response = client.post(
        "/api/auth/login",
        json={"email": "nonexistent@example.com", "password": "wrongpassword"}
    )
    assert response.status_code == 401
