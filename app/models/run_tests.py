from pydantic import BaseModel, ConfigDict, Field


class RunTestsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(pattern=r"^(?:TASK|TEST)-\d{3}$")
    agent: str


class RunTestsResponse(BaseModel):
    task_id: str
    success: bool
    exit_code: int | None
    stdout: str
    stderr: str
    message: str
