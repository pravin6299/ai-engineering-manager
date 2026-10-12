import pytest

from backend.app.database import get_db
from backend.app.main import app


@pytest.fixture(autouse=True)
def isolate_dependency_overrides(request):
    app.dependency_overrides.clear()
    override = getattr(request.module, "override_get_db", None)
    if override is not None:
        app.dependency_overrides[get_db] = override
    yield
    app.dependency_overrides.clear()
