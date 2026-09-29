import json
import logging
from pathlib import PurePosixPath
from typing import Any

from pydantic import ValidationError

from app.models.task import (
    AgentName,
    AgentResult,
    BackendImplementation,
    DependencyEvidence,
    RunEvidence,
    Task,
    TaskStatus,
)
from app.services.llm import LLMService
from app.tools.dependency_manager import DependencyManager
from app.tools.test_runner import BackendTestRunner
from app.tools.workspace import WorkspacePathError, WorkspaceTool

logger = logging.getLogger(__name__)


class BackendDeveloperAgent:
    name = AgentName.BACKEND

    def __init__(
        self,
        llm_service: LLMService,
        workspace: WorkspaceTool,
        test_runner: BackendTestRunner,
        dependency_manager: DependencyManager,
        max_attempts: int = 3,
    ) -> None:
        self.llm_service = llm_service
        self.workspace = workspace
        self.test_runner = test_runner
        self.dependency_manager = dependency_manager
        self.max_repair_attempts = min(max(max_attempts, 1), 3)

    async def process(
        self,
        task: Task,
        completed_dependencies: list[dict[str, Any]] | None = None,
    ) -> AgentResult:
        logger.info("BACKEND AGENT: processing %s", task.id)
        logger.info("BACKEND AGENT: inspecting workspace")
        baseline = self._workspace_snapshot()
        acceptance_criteria = self._derive_acceptance_criteria(task)
        dependency_context = completed_dependencies or []
        task_tests: RunEvidence | None = None
        regression_tests: RunEvidence | None = None
        dependency_evidence: DependencyEvidence | None = None
        failure_context: dict[str, Any] = {}
        task_test_paths: list[str] = []
        touched_paths: set[str] = set()
        attempted_changes: list[dict[str, Any]] = []

        for repair_attempt in range(0, self.max_repair_attempts + 1):
            current_snapshot = self._workspace_snapshot()
            workspace_file_list = self._workspace_file_list()
            for test_path in task_test_paths:
                if test_path not in current_snapshot:
                    try:
                        current_snapshot[test_path] = self.workspace.read_file(test_path)
                    except (OSError, UnicodeError):
                        pass
            is_repair = repair_attempt > 0
            phase = (
                f"repair_attempt_{repair_attempt}"
                if is_repair
                else "initial_generation"
            )
            if is_repair:
                logger.info("BACKEND AGENT: repair attempt %d", repair_attempt)
            else:
                logger.info("BACKEND AGENT: initial generation")
            try:
                needs_initial_tests = not task_test_paths
                response = await self.llm_service.generate_json(
                    self._system_prompt(is_repair),
                    self._user_prompt(
                        task=task,
                        workspace_snapshot=current_snapshot,
                        workspace_file_list=workspace_file_list,
                        acceptance_criteria=acceptance_criteria,
                        completed_dependencies=dependency_context,
                        task_test_paths=task_test_paths,
                        failure_context=failure_context,
                        dependency_evidence=dependency_evidence,
                        repair_attempt=repair_attempt,
                        attempted_changes=attempted_changes,
                    ),
                )
                implementation = BackendImplementation.model_validate(response)
                self._validate_implementation(
                    implementation=implementation,
                    existing_task_tests=task_test_paths,
                    require_generated_test=needs_initial_tests,
                    workspace_files=set(current_snapshot),
                )
                attempt_changes = self._changed_paths(
                    implementation, current_snapshot
                )
                if not attempt_changes:
                    raise ValueError(
                        "Repair returned no meaningful file content changes"
                        if is_repair
                        else "Implementation returned no meaningful file content changes"
                    )
                changed_implementation = [
                    path
                    for path in attempt_changes
                    if not path.startswith("backend/tests/")
                ]
                if not changed_implementation:
                    raise ValueError(
                        "A repair must modify at least one implementation file"
                        if is_repair
                        else "Initial implementation must create or modify implementation files"
                    )

                supplied_tests = implementation.task_test_paths
                if needs_initial_tests:
                    changed_tests = set(supplied_tests) & set(attempt_changes)
                    if not supplied_tests or changed_tests != set(supplied_tests):
                        raise ValueError(
                            "A valid initial implementation must create a task-specific test"
                        )
                    task_test_paths = list(supplied_tests)
                elif supplied_tests:
                    task_test_paths = list(
                        dict.fromkeys(task_test_paths + supplied_tests)
                    )
                if not task_test_paths:
                    raise ValueError(
                        "A task-specific backend/tests/test_*.py path is required"
                    )

                self._write_changed_files(implementation, current_snapshot)
                touched_paths.update(attempt_changes)
                attempted_changes.append(
                    {
                        "phase": phase,
                        "changed_files": attempt_changes,
                        "implementation_files": changed_implementation,
                        "outcome": "written",
                    }
                )
            except (ValidationError, WorkspacePathError, OSError, ValueError) as exc:
                attempted_changes.append(
                    {
                        "phase": phase,
                        "proposed_files": [
                            item.get("path", "")
                            for item in response.get("files", [])
                            if isinstance(item, dict)
                        ]
                        if "response" in locals() and isinstance(response, dict)
                        else [],
                        "outcome": "rejected",
                        "reason": str(exc),
                    }
                )
                failure_context = {
                    "message": f"Generation or validation failed: {exc}",
                    "pytest_stdout": "",
                    "pytest_stderr": "",
                }
                logger.warning("BACKEND AGENT: %s rejected: %s", phase, exc)
                continue

            dependency_evidence = await self.dependency_manager.prepare()
            if not dependency_evidence.success:
                logger.warning(
                    "BACKEND AGENT: dependency preparation failed: %s",
                    dependency_evidence.summary,
                )
                files_created, files_modified = self._classify_task_changes(
                    baseline, touched_paths
                )
                return self._result(
                    task,
                    TaskStatus.FAILED,
                    repair_attempt,
                    files_created,
                    files_modified,
                    acceptance_criteria,
                    task_test_paths,
                    task_tests,
                    regression_tests,
                    dependency_evidence,
                )

            python_executable = dependency_evidence.python_executable
            task_tests = await self.test_runner.run(
                task_test_paths, python_executable=python_executable
            )
            if not task_tests.passed:
                failure_context = self._failure_context(
                    "Task-specific tests failed", task_tests
                )
                attempted_changes[-1]["outcome"] = "task_tests_failed"
                logger.info("BACKEND AGENT: analyzing test failure")
                continue

            regression_tests = await self.test_runner.run(
                python_executable=python_executable
            )
            if regression_tests.passed:
                logger.info("BACKEND AGENT: task ready for review")
                files_created, files_modified = self._classify_task_changes(
                    baseline, touched_paths
                )
                return self._result(
                    task,
                    TaskStatus.REVIEW,
                    repair_attempt,
                    files_created,
                    files_modified,
                    acceptance_criteria,
                    task_test_paths,
                    task_tests,
                    regression_tests,
                    dependency_evidence,
                )
            failure_context = self._failure_context(
                "Complete backend regression suite failed", regression_tests
            )
            attempted_changes[-1]["outcome"] = "regression_tests_failed"
            logger.info("BACKEND AGENT: analyzing test failure")

        files_created, files_modified = self._classify_task_changes(
            baseline, touched_paths
        )
        return self._result(
            task,
            TaskStatus.FAILED,
            self.max_repair_attempts,
            files_created,
            files_modified,
            acceptance_criteria,
            task_test_paths,
            task_tests,
            regression_tests,
            dependency_evidence,
        )

    @staticmethod
    def _system_prompt(is_repair: bool) -> str:
        common = (
            "You are a controlled backend coding agent. Implement only the CURRENT TASK. "
            "Return strict JSON with a non-empty 'files' array and a 'task_test_paths' "
            "array. Every file needs a workspace-relative path under backend/. Never "
            "return markdown, commands, secrets, deployment configuration, or paths "
            "outside backend/. "
        )
        if not is_repair:
            return common + (
                "This is the initial implementation. Generate task-specific implementation, "
                "backend/requirements.txt, and at least one task test under "
                "backend/tests/test_*.py. Include each task test in task_test_paths."
            )
        return common + (
            "This is a repair attempt. Analyze the failing assertion or exception and modify "
            "the existing implementation so the existing task tests pass. Reason generically "
            "about missing endpoints, model or schema mismatches, relationships, imports, "
            "validation errors, and runtime exceptions. Do not regenerate unrelated files. "
            "Do not change a valid test merely to make it pass. Reuse the existing task tests "
            "without returning them in files. Do not create numbered duplicate tests. Return "
            "at least one meaningfully changed non-test implementation file."
        )

    @staticmethod
    def _user_prompt(
        task: Task,
        workspace_snapshot: dict[str, str],
        workspace_file_list: list[str],
        acceptance_criteria: list[str],
        completed_dependencies: list[dict[str, Any]],
        task_test_paths: list[str],
        failure_context: dict[str, Any],
        dependency_evidence: DependencyEvidence | None,
        repair_attempt: int,
        attempted_changes: list[dict[str, Any]],
    ) -> str:
        existing_tests = {
            path: workspace_snapshot[path]
            for path in task_test_paths
            if path in workspace_snapshot
        }
        return json.dumps(
            {
                "operation": "backend_implementation",
                "attempt_number": repair_attempt,
                "attempt_type": (
                    f"repair_attempt_{repair_attempt}"
                    if repair_attempt
                    else "initial_generation"
                ),
                "original_task": task.model_dump(mode="json"),
                "current_task": {
                    "id": task.id,
                    "title": task.title,
                    "description": task.description,
                },
                "acceptance_criteria": acceptance_criteria,
                "existing_workspace_file_list": workspace_file_list,
                "relevant_existing_file_contents": workspace_snapshot,
                "existing_task_test_paths": task_test_paths,
                "existing_task_tests": existing_tests,
                "completed_dependencies": completed_dependencies,
                "pytest_stdout": failure_context.get("pytest_stdout", ""),
                "pytest_stderr": failure_context.get("pytest_stderr", ""),
                "failure_message": failure_context.get("message", ""),
                "previous_attempted_changes": attempted_changes,
                "repair_instructions": (
                    "Analyze the failing assertion or exception and modify the existing "
                    "implementation so the existing task tests pass. Do not regenerate "
                    "unrelated files. Do not change a valid test merely to make it pass."
                    if repair_attempt
                    else "Generate the initial task implementation and task-specific tests."
                ),
                "dependency_installation": (
                    dependency_evidence.model_dump(mode="json")
                    if dependency_evidence
                    else None
                ),
            }
        )

    @staticmethod
    def _derive_acceptance_criteria(task: Task) -> list[str]:
        return [
            f"Implement the current task: {task.title}.",
            f"Satisfy the requested behavior: {task.description}",
            "Register the task behavior with the FastAPI application when routing is required.",
            "Add task-specific pytest coverage for successful behavior and key validation.",
            "Pass the task-specific tests and the complete backend regression test suite.",
        ]

    def _workspace_snapshot(self) -> dict[str, str]:
        snapshot: dict[str, str] = {}
        remaining = 20_000
        for path in self._workspace_file_list()[:80]:
            if remaining <= 0:
                break
            if (
                "__pycache__" in path
                or "/.venv/" in f"/{path}"
                or path.endswith((".pyc", ".pyo"))
            ):
                continue
            try:
                content = self.workspace.read_file(path)[:remaining]
            except (OSError, UnicodeError):
                continue
            snapshot[path] = content
            remaining -= len(content)
        return snapshot

    def _workspace_file_list(self) -> list[str]:
        return [
            path
            for path in self.workspace.list_files()
            if "__pycache__" not in path
            and "/.venv/" not in f"/{path}"
            and not path.endswith((".pyc", ".pyo"))
        ]

    def _validate_implementation(
        self,
        implementation: BackendImplementation,
        existing_task_tests: list[str],
        require_generated_test: bool,
        workspace_files: set[str],
    ) -> None:
        generated_paths = {item.path for item in implementation.files}
        for item in implementation.files:
            self._validate_generated_path(item.path)
        for test_path in implementation.task_test_paths:
            self._validate_generated_path(test_path)
            path = PurePosixPath(test_path)
            if (
                not test_path.startswith("backend/tests/test_")
                or path.suffix != ".py"
                or (
                    test_path not in generated_paths
                    and test_path not in workspace_files
                    and test_path not in existing_task_tests
                )
            ):
                raise ValueError(
                    "Task tests must be generated or existing backend/tests/test_*.py files"
                )
        if require_generated_test and not set(
            implementation.task_test_paths
        ).issubset(generated_paths):
            raise ValueError(
                "Initial task tests must be generated backend/tests/test_*.py files"
            )

    @staticmethod
    def _changed_paths(
        implementation: BackendImplementation,
        current_snapshot: dict[str, str],
    ) -> list[str]:
        return [
            item.path
            for item in implementation.files
            if current_snapshot.get(item.path) != item.content
        ]

    def _write_changed_files(
        self,
        implementation: BackendImplementation,
        current_snapshot: dict[str, str],
    ) -> None:
        for generated_file in implementation.files:
            if current_snapshot.get(generated_file.path) == generated_file.content:
                continue
            self.workspace.write_file(generated_file.path, generated_file.content)

    def _classify_task_changes(
        self, baseline: dict[str, str], touched_paths: set[str]
    ) -> tuple[list[str], list[str]]:
        current = self._workspace_snapshot()
        created = sorted(
            path for path in touched_paths if path not in baseline and path in current
        )
        modified = sorted(
            path
            for path in touched_paths
            if path in baseline and current.get(path) != baseline[path]
        )
        return created, modified

    @staticmethod
    def _validate_generated_path(path: str) -> None:
        normalized = path.replace("\\", "/")
        parsed = PurePosixPath(normalized)
        if (
            parsed.is_absolute()
            or ".." in parsed.parts
            or not parsed.parts
            or parsed.parts[0] != "backend"
        ):
            raise WorkspacePathError("Backend agent may only write under backend/")

    @staticmethod
    def _failure_context(label: str, evidence: RunEvidence) -> dict[str, str]:
        return {
            "message": label,
            "pytest_stdout": evidence.stdout,
            "pytest_stderr": evidence.stderr,
        }

    def _result(
        self,
        task: Task,
        status: TaskStatus,
        attempts: int,
        files_created: list[str],
        files_modified: list[str],
        acceptance_criteria: list[str],
        task_test_paths: list[str],
        task_tests: RunEvidence | None,
        regression_tests: RunEvidence | None,
        dependencies: DependencyEvidence | None,
    ) -> AgentResult:
        files_changed = files_created + files_modified
        criteria_satisfied = bool(
            acceptance_criteria
            and task_test_paths
            and task_tests
            and task_tests.passed
            and regression_tests
            and regression_tests.passed
            and dependencies
            and dependencies.success
        )
        return AgentResult(
            task_id=task.id,
            agent=self.name,
            status=status,
            summary=(
                "Task-specific backend implementation ready after initial generation "
                f"and {attempts} repair attempt(s)."
                if status == TaskStatus.REVIEW
                else "Backend implementation failed after initial generation and "
                f"{attempts} repair attempt(s)."
            ),
            proposed_implementation={
                "scope": "backend",
                "current_task": task.id,
                "dangerous_actions_performed": False,
            },
            safety_notes=[
                "Writes were confined to the configured workspace.",
                "Dependencies were restricted to an approved allowlist.",
                "Only validated task tests and the fixed backend pytest suite were executed.",
                "No deployment, production access, Git operation, or arbitrary shell was used.",
            ],
            files_changed=files_changed,
            files_created=files_created,
            files_modified=files_modified,
            acceptance_criteria=acceptance_criteria,
            acceptance_criteria_satisfied=criteria_satisfied,
            task_test_paths=task_test_paths,
            task_tests=task_tests,
            tests=regression_tests,
            dependencies=dependencies,
            attempts=attempts,
        )
