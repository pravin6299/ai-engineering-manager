import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.main import app
from backend.app.database import Base, get_db

SQLALCHEMY_DATABASE_URL = "sqlite:///./test_auth_api.db"
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

def test_register_and_login():
    # Register
    response = client.post("/api/auth/register", json={
        "email": "testuser@example.com",
        "password": "securepassword123",
        "role": "student"
    })
    assert response.status_code == 201
    data = response.json()
    assert data["email"] == "testuser@example.com"
    assert data["role"] == "student"

    # Login
    login_response = client.post("/api/auth/login", json={
        "email": "testuser@example.com",
        "password": "securepassword123"
    })
    assert login_response.status_code == 200
    token_data = login_response.json()
    assert "access_token" in token_data
    assert "refresh_token" in token_data
    assert token_data["token_type"] == "bearer"

    # Access /me with token
    token = token_data["access_token"]
    me_response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_response.status_code == 200
    assert me_response.json()["email"] == "testuser@example.com"

def test_login_invalid_password():
    client.post("/api/auth/register", json={
        "email": "user2@example.com",
        "password": "correctpassword"
    })
    response = client.post("/api/auth/login", json={
        "email": "user2@example.com",
        "password": "wrongpassword"
    })
    assert response.status_code == 401
