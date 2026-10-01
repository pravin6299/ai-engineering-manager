import pytest

from app.models.task import FailureCategory, RunEvidence
from app.services.failure_classifier import FailureClassifier


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (
            "ModuleNotFoundError: No module named 'package_name'",
            FailureCategory.DEPENDENCY_FAILURE,
        ),
        (
            "Permission denied while creating the test database",
            FailureCategory.ENVIRONMENT_FAILURE,
        ),
        (
            "backend/tests/test_api.py: fixture 'client' not found",
            FailureCategory.TEST_FAILURE,
        ),
        (
            "site-packages/starlette/testclient.py\nassert 404 == 200",
            FailureCategory.CODE_FAILURE,
        ),
        (
            "assert 400 == 201\n================ warnings summary ================\npasslib/bcrypt backend version warning",
            FailureCategory.CODE_FAILURE,
        ),
        ("Error: Failed to resolve import ./Missing from src/App.jsx", FailureCategory.IMPORT_ERROR),
        ("Unable to find role=button", FailureCategory.RENDER_ERROR),
        ("node_modules/vitest/index.js\nprocess exited without diagnostic context", FailureCategory.UNKNOWN_FAILURE),
        ("process exited without diagnostic context", FailureCategory.UNKNOWN_FAILURE),
    ],
)
def test_failure_classifier_uses_error_context_not_path_alone(
    output: str, expected: FailureCategory
) -> None:
    evidence = RunEvidence(
        passed=False,
        exit_code=1,
        summary="failed",
        stdout=output,
    )

    assert FailureClassifier().classify(evidence).category == expected
