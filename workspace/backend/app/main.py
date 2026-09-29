from fastapi import FastAPI, Depends
from sqlalchemy.orm import Session
from . import models, database

app = FastAPI()

# Create tables on startup
models.Base.metadata.create_all(bind=database.engine)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/users")
def read_users(db: Session = Depends(database.get_db)):
    return db.query(models.User).all()
