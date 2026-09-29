from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class AgentName(StrEnum):
    BACKEND = "backend"
    FRONTEND = "frontend"


class Priority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TaskStatus(StrEnum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    BLOCKED = "blocked"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    COMPLETED = "completed"
    FAILED = "failed"


class ProjectRequest(BaseModel):
    requirement: str = Field(..., min_length=1)


class Task(BaseModel):
    id: str = Field(..., pattern=r"^TASK-\d{3}$")
    title: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    assigned_agent: AgentName
    priority: Priority
    status: TaskStatus = TaskStatus.ASSIGNED
    depends_on: list[str] = Field(default_factory=list)

    @field_validator("depends_on")
    @classmethod
    def validate_dependency_ids(cls, value: list[str]) -> list[str]:
        invalid = [task_id for task_id in value if not task_id.startswith("TASK-")]
        if invalid:
            raise ValueError(f"Invalid dependency ids: {', '.join(invalid)}")
        return value


class RunEvidence(BaseModel):
    passed: bool
    exit_code: int
    summary: str
    stdout: str = ""
    stderr: str = ""


class GeneratedFile(BaseModel):
    path: str = Field(..., min_length=1)
    content: str


class BackendImplementation(BaseModel):
    files: list[GeneratedFile] = Field(..., min_length=1)
    task_test_paths: list[str] = Field(default_factory=list)


class DependencyEvidence(BaseModel):
    success: bool
    requested: list[str] = Field(default_factory=list)
    approved: list[str] = Field(default_factory=list)
    already_installed: list[str] = Field(default_factory=list)
    installed: list[str] = Field(default_factory=list)
    summary: str
    stdout: str = ""
    stderr: str = ""
    python_executable: str | None = None


class AgentResult(BaseModel):
    task_id: str
    agent: AgentName
    status: TaskStatus
    summary: str
    proposed_implementation: dict[str, Any] = Field(default_factory=dict)
    safety_notes: list[str] = Field(default_factory=list)
    files_changed: list[str] = Field(default_factory=list)
    files_created: list[str] = Field(default_factory=list)
    files_modified: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    acceptance_criteria_satisfied: bool = False
    task_test_paths: list[str] = Field(default_factory=list)
    task_tests: RunEvidence | None = None
    tests: RunEvidence | None = None
    dependencies: DependencyEvidence | None = None
    attempts: int = Field(default=0, ge=0, le=3)


class ProjectResponse(BaseModel):
    requirement: str
    tasks: list[Task]
    results: list[AgentResult]
