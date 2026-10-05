'''backend/tests/test_student_dashboard.py

Integration tests for the ``GET /api/student/dashboard`` endpoint.
The tests verify:
* 200 response with correctly shaped data for an authenticated student.
* 401 when no token is supplied.
* 403 when the token belongs to a user without the ``student`` role.

The FastAPI ``TestClient`` (via ``httpx.AsyncClient``) is used together
with ``dependency_overrides`` to inject a mock ``User`` object without
having to generate real JWTs.
''' 

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from backend.main import app
from backend.dependencies.auth import User

# ---------------------------------------------------------------------------
# Helper – overrides the authentication dependency for the duration of a test.
# ---------------------------------------------------------------------------

def override_get_current_user(user: User):
    async def _override():
        return user
    return _override


@pytest.fixture
def anyio_backend():
    # Required by pytest‑asyncio for async tests.
    return "asyncio"


@pytest.mark.anyio
async def test_dashboard_success():
    # Mock a student user.
    mock_user = User(
        id=1,
        email="student@example.com",
        full_name="Student One",
        roles=["student"],
    )
    app.dependency_overrides["backend.dependencies.auth.get_current_user"] = (
        override_get_current_user(mock_user)
    )

    async with AsyncClient(app=app, base_url="http://testserver") as client:
        response = await client.get("/api/student/dashboard")
        assert response.status_code == 200
        data = response.json()
        # Verify top‑level keys.
        assert "profile" in data
        assert "academic_records" in data
        # Verify profile fields.
        profile = data["profile"]
        assert profile["student_id"] == mock_user.id
        assert profile["full_name"] == mock_user.full_name
        assert profile["email"] == mock_user.email
        # Verify at least one academic record exists.
        assert isinstance(data["academic_records"], list)
        assert len(data["academic_records"]) > 0

    # Clean up overrides.
    app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_dashboard_unauthenticated():
    # No override – the real dependency will raise 401 because no header.
    async with AsyncClient(app=app, base_url="http://testserver") as client:
        response = await client.get("/api/student/dashboard")
        assert response.status_code == 401


@pytest.mark.anyio
async def test_dashboard_forbidden_role():
    # Mock a user that lacks the student role.
    mock_user = User(
        id=2,
        email="teacher@example.com",
        full_name="Teacher Two",
        roles=["teacher"],
    )
    app.dependency_overrides["backend.dependencies.auth.get_current_user"] = (
        override_get_current_user(mock_user)
    )

    async with AsyncClient(app=app, base_url="http://testserver") as client:
        response = await client.get("/api/student/dashboard")
        assert response.status_code == 403

    app.dependency_overrides.clear()
