import pytest
from fastapi.testclient import TestClient
from fastapi import status
from backend.app.main import app
from backend.app.database import Base, engine
from backend.app import models

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module", autouse=True)
def create_test_db():
    # Re‑create tables for a clean test run
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)

# ---------------------------------------------------------------------------
# Helper to get a fresh client for each test
# ---------------------------------------------------------------------------
client = TestClient(app)

# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------
def test_register_success():
    payload = {
        "email": "alice@example.com",
        "password": "StrongPass123",
        "full_name": "Alice"
    }
    response = client.post("/api/register", json=payload)
    assert response.status_code == status.HTTP_201_CREATED
    data = response.json()
    assert data["email"] == payload["email"]
    assert data["full_name"] == payload["full_name"]
    assert "id" in data
    assert data["role"] == "user"

def test_register_duplicate_email():
    payload = {
        "email": "bob@example.com",
        "password": "AnotherStrong1",
        "full_name": "Bob"
    }
    client.post("/api/register", json=payload)
    response = client.post("/api/register", json=payload)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "Email already registered"

# ---------------------------------------------------------------------------
# Login tests
# ---------------------------------------------------------------------------
def test_login_success():
    register_payload = {
        "email": "charlie@example.com",
        "password": "Charli3Pass!",
        "full_name": "Charlie"
    }
    client.post("/api/register", json=register_payload)
    login_payload = {
        "email": register_payload["email"],
        "password": register_payload["password"]
    }
    response = client.post("/api/login", json=login_payload)
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"

def test_login_invalid_credentials():
    login_payload = {"email": "nonexistent@example.com", "password": "doesntmatter"}
    response = client.post("/api/login", json=login_payload)
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json()["detail"] == "Invalid credentials"
