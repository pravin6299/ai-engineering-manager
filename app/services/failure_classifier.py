import logging

from app.models.task import FailureCategory, FailureClassification, RunEvidence

logger = logging.getLogger(__name__)


class FailureClassifier:
    def classify(self, evidence: RunEvidence) -> FailureClassification:
        text = f"{evidence.stdout}\n{evidence.stderr}".lower()
        failure_text = text.split("warnings summary", 1)[0]

        frontend_signals = (
            (FailureCategory.IMPORT_ERROR, ("failed to resolve import", "cannot find module ./"), "Frontend source contains an unresolved local import."),
            (FailureCategory.BUILD_ERROR, ("transform failed", "build failed", "parse failure", "unexpected token"), "The frontend toolchain could not parse or build the generated source."),
            (FailureCategory.ROUTING_ERROR, ("no routes matched location", "use location() may be used only", "use navigate() may be used only"), "The failure is attributable to frontend route configuration."),
            (FailureCategory.API_INTEGRATION_ERROR, ("failed to fetch", "networkerror", "response status", "mock fetch"), "The frontend API integration returned an unexpected result."),
            (FailureCategory.RENDER_ERROR, ("unable to find an element", "unable to find role", "unable to find text", "invalid hook call"), "The rendered component does not satisfy the expected UI behavior."),
            (FailureCategory.VALIDATION_ERROR, ("validation error", "invalid form", "required field"), "The frontend validation behavior does not satisfy the test."),
        )
        for category, signals, reason in frontend_signals:
            if any(signal in text for signal in signals):
                return self._result(category, reason)

        dependency_signals = (
            "cannot find package",
            "cannot find module .react",
            "modulenotfounderror",
            "distributionnotfound",
            "dependency conflict",
            "incompatible installed package",
            "package metadata incompatibility",
            "trapped error reading bcrypt version",
        )
        compatibility_context = any(
            signal in failure_text
            for signal in (
                "trapped error reading bcrypt version",
                "bcrypt backend compatibility error",
                "password cannot be longer than 72 bytes",
            )
        )
        if any(signal in failure_text for signal in dependency_signals) or compatibility_context:
            return self._result(
                FailureCategory.DEPENDENCY_FAILURE,
                "Traceback indicates a missing or incompatible Python dependency.",
            )

        if any(
            signal in text
            for signal in (
                "permission denied",
                "no space left on device",
                "read-only file system",
                "tests timed out",
                "executable not found",
            )
        ):
            return self._result(
                FailureCategory.ENVIRONMENT_FAILURE,
                "Failure originates from the execution environment.",
            )

        if (
            "fixture " in text and " not found" in text
        ) or ("syntaxerror" in text and "backend/tests/" in text):
            return self._result(
                FailureCategory.TEST_FAILURE,
                "The generated test or test fixture is invalid.",
            )

        if any(
            signal in text
            for signal in (
                "backend/app/",
                "assert ",
                "assertionerror",
                "typeerror",
                "validationerror",
                "attributeerror",
                "keyerror",
                "runtimeerror",
                "operationalerror",
                "noforeignkeyserror",
                "sqlalchemy.exc.argumenterror",
                "sqlalchemy.exc.invalidrequesterror",
            )
        ):
            return self._result(
                FailureCategory.CODE_FAILURE,
                "Failure is attributable to generated application behavior.",
            )

        return self._result(
            FailureCategory.UNKNOWN_FAILURE,
            "Failure could not be classified deterministically.",
        )

    @staticmethod
    def _result(
        category: FailureCategory, reason: str
    ) -> FailureClassification:
        logger.info("FAILURE CLASSIFIER: %s", category.value)
        return FailureClassification(category=category, reason=reason)
