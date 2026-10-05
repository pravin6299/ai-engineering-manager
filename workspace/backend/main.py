'''backend/main.py

Entry point for the FastAPI application.  The file registers the
Student Dashboard router created for TASK‑003.
''' 

from fastapi import FastAPI

from backend.api.student_dashboard import router as student_dashboard_router

app = FastAPI(title="University Platform API")

# Register routers – other routers would be added here as the project grows.
app.include_router(student_dashboard_router)
