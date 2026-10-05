import pytest
from httpx import AsyncClient
from fastapi import status
from app.main import app
from app.database import Base, engine
from app import models

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
@pytest.fixture
def anyio_backend():
    return "asyncio"

# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_register_success():
    async with AsyncClient(app=app, base_url="http://test") as ac:
        payload = {
            "email": "alice@example.com",
            "password": "StrongPass123",
            "full_name": "Alice"
        }
        response = await ac.post("/api/register", json=payload)
    assert response.status_code == status.HTTP_201_CREATED
    data = response.json()
    assert data["email"] == payload["email"]
    assert data["full_name"] == payload["full_name"]
    assert "id" in data
    assert data["role"] == "user"

@pytest.mark.anyio
async def test_register_duplicate_email():
    async with AsyncClient(app=app, base_url="http://test") as ac:
        payload = {
            "email": "bob@example.com",
            "password": "AnotherStrong1",
            "full_name": "Bob"
        }
        # first registration
        await ac.post("/api/register", json=payload)
        # second registration with same email
        response = await ac.post("/api/register", json=payload)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "Email already registered"

# ---------------------------------------------------------------------------
# Login tests
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_login_success():
    async with AsyncClient(app=app, base_url="http://test") as ac:
        # Ensure user exists
        register_payload = {
            "email": "charlie@example.com",
            "password": "Charli3Pass!",
            "full_name": "Charlie"
        }
        await ac.post("/api/register", json=register_payload)
        # Attempt login
        login_payload = {
            "email": register_payload["email"],
            "password": register_payload["password"]
        }
        response = await ac.post("/api/login", json=login_payload)
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"

@pytest.mark.anyio
async def test_login_invalid_credentials():
    async with AsyncClient(app=app, base_url="http://test") as ac:
        login_payload = {"email": "nonexistent@example.com", "password": "doesntmatter"}
        response = await ac.post("/api/login", json=login_payload)
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json()["detail"] == "Invalid credentials"
