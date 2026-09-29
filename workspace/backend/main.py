from fastapi import FastAPI
from .routers import schema

app = FastAPI(title="Educational Platform API")

# Include routers
app.include_router(schema.router)
