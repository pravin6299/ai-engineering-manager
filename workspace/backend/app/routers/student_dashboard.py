'''backend/app/routers/student_dashboard.py

FastAPI router that provides the ``GET /api/student/dashboard`` endpoint.
The endpoint:
- Requires a valid JWT (handled by ``get_current_user`` dependency from TASK‑001).
- Ensures the authenticated user has the ``student`` role.
- Returns the student profile and academic data in a format expected by the frontend.

The actual data‑access layer is abstracted behind simple helper functions
(``get_student_profile`` and ``get_academic_data``). In a real project these would
query the database; for the purpose of this task they return static example
objects.
''' 

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from typing import List

# Import the authentication dependency that validates JWT and returns a user object.
# This dependency is defined in TASK‑001.
from backend.app.auth import get_current_user
from backend.app.models import User

router = APIRouter()

# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------
class Profile(BaseModel):
    id: int
    name: str
    email: str

class CourseGrade(BaseModel):
    course_code: str
    course_name: str
    grade: str

class AcademicData(BaseModel):
    courses: List[CourseGrade]
    gpa: float

class StudentDashboardResponse(BaseModel):
    profile: Profile
    academic: AcademicData

# ---------------------------------------------------------------------------
# Mock data‑access helpers (replace with real DB calls in production)
# ---------------------------------------------------------------------------
def get_student_profile(user: User) -> Profile:
    """Return a dummy profile for the given user id.
    In production this would fetch data from the ``students`` table.
    """
    return Profile(
        id=user.id,
        name=user.full_name or user.email,
        email=user.email
    )

def get_academic_data(user_id: int) -> AcademicData:
    """Return dummy academic data.
    In production this would aggregate enrolments, grades, GPA, etc.
    """
    courses = [
        CourseGrade(course_code="CS101", course_name="Intro to CS", grade="A"),
        CourseGrade(course_code="MATH201", course_name="Calculus II", grade="B+"),
    ]
    return AcademicData(courses=courses, gpa=3.7)

# ---------------------------------------------------------------------------
# Endpoint implementation
# ---------------------------------------------------------------------------
@router.get(
    "/api/student/dashboard",
    response_model=StudentDashboardResponse,
    status_code=status.HTTP_200_OK,
    summary="Return the authenticated student's dashboard data",
    tags=["Student Dashboard"]
)
async def get_student_dashboard(current_user: User = Depends(get_current_user)):
    """Return profile and academic data for the authenticated student.

    * ``current_user`` is injected by ``get_current_user`` and is expected to be a
      dictionary (or Pydantic model) containing at least ``id`` and ``role``.
    * If the role is not ``student`` a ``403`` error is raised.
    """
    if current_user.role != "student":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User does not have permission to access student dashboard"
        )

    profile = get_student_profile(current_user)
    academic = get_academic_data(current_user.id)
    return StudentDashboardResponse(profile=profile, academic=academic)
