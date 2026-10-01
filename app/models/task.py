from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentName(str, Enum):
    BACKEND = "backend"
    FRONTEND = "frontend"


class Priority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TaskStatus(str, Enum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    BLOCKED = "blocked"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    COMPLETED = "completed"
    FAILED = "failed"


class ExecutionStage(str, Enum):
    GENERATION = "GENERATION"
    FORMAT_VALIDATION = "FORMAT_VALIDATION"
    IMPLEMENTATION_VALIDATION = "IMPLEMENTATION_VALIDATION"
    DEPENDENCY_PREPARATION = "DEPENDENCY_PREPARATION"
    TEST_EXECUTION = "TEST_EXECUTION"
    CODE_REPAIR = "CODE_REPAIR"
    MANAGER_REVIEW = "MANAGER_REVIEW"


class FailureCategory(str, Enum):
    CODE_FAILURE = "CODE_FAILURE"
    IMPORT_ERROR = "IMPORT_ERROR"
    BUILD_ERROR = "BUILD_ERROR"
    ROUTING_ERROR = "ROUTING_ERROR"
    API_INTEGRATION_ERROR = "API_INTEGRATION_ERROR"
    RENDER_ERROR = "RENDER_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    DEPENDENCY_FAILURE = "DEPENDENCY_FAILURE"
    ENVIRONMENT_FAILURE = "ENVIRONMENT_FAILURE"
    TEST_FAILURE = "TEST_FAILURE"
    UNKNOWN_FAILURE = "UNKNOWN_FAILURE"


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
    provides: list[str] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list)

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


class FailureClassification(BaseModel):
    category: FailureCategory
    reason: str


class GeneratedFile(BaseModel):
    path: str = Field(..., min_length=1)
    content: str


class BackendImplementation(BaseModel):
    files: list[GeneratedFile] = Field(..., min_length=1)
    task_test_paths: list[str] = Field(default_factory=list)


class RepairFailureCategory(str, Enum):
    ORM_CONFIGURATION = "ORM_CONFIGURATION"
    IMPORT_ERROR = "IMPORT_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    ROUTING_ERROR = "ROUTING_ERROR"
    AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
    DATABASE_ERROR = "DATABASE_ERROR"
    RUNTIME_ERROR = "RUNTIME_ERROR"
    ASSERTION_FAILURE = "ASSERTION_FAILURE"
    BUILD_ERROR = "BUILD_ERROR"
    API_INTEGRATION_ERROR = "API_INTEGRATION_ERROR"
    RENDER_ERROR = "RENDER_ERROR"
    UNKNOWN = "UNKNOWN"


class FrontendImplementation(BaseModel):
    summary: str = Field(..., min_length=1)
    files: list[GeneratedFile] = Field(..., min_length=1)
    dependencies: list[str] = Field(default_factory=list)
    task_test_paths: list[str] = Field(default_factory=list)

class FrontendImplementationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(..., min_length=1)
    files_to_create: list[str] = Field(default_factory=list)
    files_to_modify: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    task_test_paths: list[str] = Field(default_factory=list)


class FrontendGeneratedFile(GeneratedFile):
    model_config = ConfigDict(extra="forbid")



class FrontendRepair(BaseModel):
    failure_category: RepairFailureCategory
    root_cause: str = Field(..., min_length=1)
    files_to_modify: list[str] = Field(..., min_length=1)
    repair_strategy: str = Field(..., min_length=1)
    files: list[GeneratedFile] = Field(..., min_length=1)
    task_test_paths: list[str] = Field(default_factory=list)
    test_change_justification: str | None = None


class BackendRepair(BaseModel):
    failure_category: RepairFailureCategory
    root_cause: str = Field(..., min_length=1)
    files_to_modify: list[str] = Field(..., min_length=1)
    repair_strategy: str = Field(..., min_length=1)
    files: list[GeneratedFile] = Field(..., min_length=1)
    task_test_paths: list[str] = Field(default_factory=list)
    test_change_justification: str | None = None


class RepairAttemptEvidence(BaseModel):
    repair_attempt: int = Field(..., ge=1, le=3)
    failure_category: RepairFailureCategory
    root_cause: str
    repair_strategy: str
    files_modified: list[str] = Field(default_factory=list)
    test_result_before: str
    test_result_after: str | None = None
    failure_signature_before: str
    failure_signature_after: str | None = None


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


class TaskTestOwnership(BaseModel):
    task_id: str = Field(..., pattern=r"^TASK-\d{3}$")
    test_paths: list[str] = Field(default_factory=list)
    completion_status: TaskStatus


class AcceptanceResult(BaseModel):
    criterion: str
    required: bool = True
    applicable: bool = True
    satisfied: bool
    evidence: list[str] = Field(default_factory=list)


class CompletionGateResult(BaseModel):
    implementation_evidence: bool
    task_specific_changes: bool
    task_tests: bool
    regression_tests: bool
    dependencies: bool
    acceptance_criteria: bool
    no_unresolved_failure: bool
    passed: bool
    rejection_reason: str | None = None


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
    acceptance_results: list[AcceptanceResult] = Field(default_factory=list)
    acceptance_criteria_satisfied: bool = False
    completion_gate: CompletionGateResult | None = None
    rejection_reason: str | None = None
    task_test_paths: list[str] = Field(default_factory=list)
    regression_test_paths: list[str] = Field(default_factory=list)
    task_tests: RunEvidence | None = None
    tests: RunEvidence | None = None
    dependencies: DependencyEvidence | None = None
    generation_attempts: int = Field(default=1, ge=1)
    format_correction_attempts: int = Field(default=0, ge=0)
    generation_correction_attempts: int = Field(default=0, ge=0)
    provider_fallback_count: int = Field(default=0, ge=0)
    code_repair_attempts: int = Field(default=0, ge=0, le=3)
    attempts: int = Field(default=0, ge=0, le=3)
    execution_stage: ExecutionStage = ExecutionStage.GENERATION
    failure_stage: ExecutionStage | None = None
    repair_history: list[RepairAttemptEvidence] = Field(default_factory=list)


class ProjectResponse(BaseModel):
    requirement: str
    tasks: list[Task]
    results: list[AgentResult]
