from fastapi import FastAPI
from .routers import auth, student, student_dashboard, student_management

app = FastAPI(title="Auth Service")

app.include_router(auth.router)
app.include_router(student_dashboard.router)
app.include_router(student.router)
app.include_router(student_management.router)
