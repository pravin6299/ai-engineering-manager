'''backend/api/student_dashboard.py

FastAPI router that provides the **Student Dashboard** endpoint.
The endpoint is protected by JWT authentication and enforces that the
authenticated user has the ``student`` role.  It returns a JSON payload
containing the student's profile information and a list of academic
records (courses and grades).

The implementation is deliberately lightweight – it delegates the
actual data‑retrieval to a service layer (``dashboard_service``) so that
business logic can be unit‑tested independently of the HTTP layer.
''' 

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from typing import List

# Import the authentication dependency – assumed to exist in the project
# and to raise 401 if the token is missing/invalid.
from backend.dependencies.auth import get_current_user, User

# Service that fetches the dashboard data from the database or other sources.
from backend.services.dashboard_service import get_student_dashboard

router = APIRouter(prefix="/api/student", tags=["Student Dashboard"])


class CourseRecord(BaseModel):
    course_id: str
    course_name: str
    grade: str


class StudentProfile(BaseModel):
    student_id: int
    full_name: str
    email: str


class DashboardResponse(BaseModel):
    profile: StudentProfile
    academic_records: List[CourseRecord]


def _ensure_student_role(user: User):
    """Utility that checks the user has the *student* role.

    Raises:
        HTTPException: 403 if the role is missing.
    """
    if "student" not in user.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User does not have the required 'student' role",
        )
    return user


@router.get("/dashboard", response_model=DashboardResponse)
async def get_dashboard(
    current_user: User = Depends(_ensure_student_role),
):
    """Return the authenticated student's dashboard data.

    The endpoint:
    * validates the JWT via ``get_current_user`` (handled by the dependency).
    * ensures the user possesses the ``student`` role.
    * fetches the dashboard payload from the service layer.
    """
    # ``current_user`` is already the validated User instance.
    dashboard_data = await get_student_dashboard(current_user.id)
    return dashboard_data
