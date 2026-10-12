'''backend/tests/test_student_dashboard.py

Integration tests for the ``GET /api/student/dashboard`` endpoint.
The tests verify:
* 200 response with correctly shaped data for an authenticated student.
* 401 when no token is supplied.
* 403 when the token belongs to a user without the ``student`` role.

The FastAPI ``TestClient`` is used together
with ``dependency_overrides`` to inject a mock ``User`` object without
having to generate real JWTs.
''' 

import pytest
from fastapi.testclient import TestClient
from types import SimpleNamespace
from backend.app.main import app
from backend.app.auth import get_current_user

# ---------------------------------------------------------------------------
# Helper – overrides the authentication dependency for the duration of a test.
# ---------------------------------------------------------------------------

def override_get_current_user(user):
    async def _override():
        return user
    return _override


client = TestClient(app)


def test_dashboard_success():
    # Mock a student user.
    mock_user = SimpleNamespace(
        id=1,
        email="student@example.com",
        full_name="Student One",
        role="student",
    )
    app.dependency_overrides[get_current_user] = (
        override_get_current_user(mock_user)
    )

    response = client.get("/api/student/dashboard")
    assert response.status_code == 200
    data = response.json()
    assert "profile" in data
    assert "academic" in data
    profile = data["profile"]
    assert profile["id"] == mock_user.id
    assert profile["name"] == mock_user.full_name
    assert profile["email"] == mock_user.email
    assert isinstance(data["academic"]["courses"], list)
    assert len(data["academic"]["courses"]) > 0

    # Clean up overrides.
    app.dependency_overrides.clear()


def test_dashboard_unauthenticated():
    # No override – the real dependency will raise 401 because no header.
    response = client.get("/api/student/dashboard")
    assert response.status_code == 401


def test_dashboard_forbidden_role():
    # Mock a user that lacks the student role.
    mock_user = SimpleNamespace(
        id=2,
        email="teacher@example.com",
        full_name="Teacher Two",
        role="teacher",
    )
    app.dependency_overrides[get_current_user] = (
        override_get_current_user(mock_user)
    )

    response = client.get("/api/student/dashboard")
    assert response.status_code == 403

    app.dependency_overrides.clear()
