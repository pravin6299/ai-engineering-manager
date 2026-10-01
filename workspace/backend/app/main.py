from fastapi import FastAPI, Depends
from sqlalchemy.orm import Session
from . import models, database
from backend.app.routers import auth as auth_router
from backend.app.routers import student as student_router
from backend.app.routers import student_management as student_management_router

app = FastAPI(title="Educational Platform API")

models.Base.metadata.create_all(bind=database.engine)

app.include_router(auth_router.router)
app.include_router(student_router.router)
app.include_router(student_management_router.router)

@app.get("/health")
def health():
    return {"status": "ok"}
