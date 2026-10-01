import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from backend.app.database import Base
from backend.app.models import User, Student

SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

@pytest.fixture(scope="function")
def db():
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)

def test_create_user_and_student(db):
    user = User(email="test@example.com", hashed_password="fakepassword")
    db.add(user)
    db.commit()
    db.refresh(user)

    assert user.id is not None
    assert user.email == "test@example.com"

    student = Student(
        user_id=user.id,
        first_name="John",
        last_name="Doe",
        enrollment_number="EN12345"
    )
    db.add(student)
    db.commit()
    db.refresh(student)

    assert student.id is not None
    assert student.user_id == user.id
    assert student.user.email == "test@example.com"
    assert student.first_name == "John"
