from pydantic import BaseModel, ConfigDict, Field


class WriteFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    content: str
    allow_modify: bool = False
    expected_sha256: str | None = None


class WriteFilesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(pattern=r"^(?:TASK|TEST)-\d{3}$")
    agent: str
    files: list[WriteFile] = Field(min_length=1, max_length=50)


class WriteFilesResponse(BaseModel):
    task_id: str
    success: bool
    files_written: list[str]
    files_rejected: list[str]
    message: str
