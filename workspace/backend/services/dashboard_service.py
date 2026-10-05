from typing import Dict, List

# Import any required database utilities or models here
# For example:
# from backend.db import get_db


def get_student_dashboard(student_id: int) -> Dict:
    """Retrieve dashboard data for a given student.

    This function returns a plain dictionary containing the data required
    by the ``DashboardResponse`` model defined in the API layer. Returning a
    dict avoids a circular import between the service and the API module.

    Args:
        student_id: The unique identifier of the student.

    Returns:
        A dictionary with keys matching the fields of ``DashboardResponse``.
    """
    # Placeholder implementation – replace with real database queries.
    # Example structure expected by DashboardResponse:
    # {
    #     "student_id": student_id,
    #     "name": "John Doe",
    #     "courses": ["Math", "Science"]
    # }
    # For now, return static data for demonstration/testing purposes.
    return {
        "student_id": student_id,
        "name": f"Student {student_id}",
        "courses": ["Course A", "Course B", "Course C"]
    }
