import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class LLMService(ABC):
    @abstractmethod
    async def generate_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """Return a JSON-compatible response from an LLM provider."""


class LLMRateLimitError(RuntimeError):
    """Raised when a provider remains rate limited after bounded retries."""


class LLMServiceUnavailableError(RuntimeError):
    """Raised when a provider remains unavailable after bounded retries."""


class ProviderLLMService(LLMService):
    max_transient_retries = 3

    def __init__(self, provider: str, api_key: str | None, model: str) -> None:
        self.provider = provider
        self.api_key = api_key
        self.model = model

    @property
    def configuration_detected(self) -> bool:
        return bool(self.api_key)

    def _fallback_response(self, user_prompt: str) -> dict[str, Any]:
        if not self.api_key:
            logger.info(
                "LLM: %s API key not set; using deterministic local fallback",
                self.provider,
            )
            try:
                request = json.loads(user_prompt)
            except json.JSONDecodeError:
                request = {}
            if request.get("operation") == "backend_implementation":
                return self._fallback_backend_implementation(request)
            return self._fallback_plan(user_prompt)
        raise RuntimeError("Fallback requested while an API key is configured")

    async def _post_json(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        retry_bad_request_once: bool = False,
    ) -> httpx.Response:
        transient_retries = 0
        bad_request_retried = False
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                response = await client.post(
                    url, headers=headers, json=payload
                )
                if response.status_code in {429, 503}:
                    if transient_retries >= self.max_transient_retries:
                        error_type = (
                            LLMRateLimitError
                            if response.status_code == 429
                            else LLMServiceUnavailableError
                        )
                        condition = (
                            "rate limit"
                            if response.status_code == 429
                            else "service unavailability"
                        )
                        raise error_type(
                            f"{self.provider} {condition} persisted after "
                            f"{self.max_transient_retries} retries for model {self.model}"
                        )
                    delay = self._retry_delay(
                        response.headers.get("Retry-After"), transient_retries
                    )
                    logger.warning(
                        "LLM: %s returned %d; retrying in %.1f seconds (%d/%d)",
                        self.provider,
                        response.status_code,
                        delay,
                        transient_retries + 1,
                        self.max_transient_retries,
                    )
                    transient_retries += 1
                    await asyncio.sleep(delay)
                    continue
                if (
                    response.status_code == 400
                    and retry_bad_request_once
                    and not bad_request_retried
                ):
                    bad_request_retried = True
                    logger.warning(
                        "LLM: %s returned 400; retrying once in 1 second",
                        self.provider,
                    )
                    await asyncio.sleep(1)
                    continue
                response.raise_for_status()
                return response

    @staticmethod
    def _retry_delay(retry_after: str | None, retry_index: int) -> float:
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    return max(
                        0.0,
                        (retry_at - datetime.now(timezone.utc)).total_seconds(),
                    )
                except (TypeError, ValueError):
                    pass
        return float(min(2**retry_index, 8))

    def _fallback_plan(self, requirement: str) -> dict[str, Any]:
        return {
            "tasks": [
                {
                    "id": "TASK-001",
                    "title": "Create authentication API",
                    "description": f"Design backend authentication endpoints, request models, and token flow for: {requirement}",
                    "assigned_agent": "backend",
                    "priority": "high",
                    "status": "assigned",
                    "depends_on": [],
                },
                {
                    "id": "TASK-002",
                    "title": "Create core backend domain APIs",
                    "description": f"Implement core backend domain APIs for: {requirement}",
                    "assigned_agent": "backend",
                    "priority": "high",
                    "status": "assigned",
                    "depends_on": ["TASK-001"],
                },
                {
                    "id": "TASK-003",
                    "title": "Build frontend authentication flow",
                    "description": "Propose login, session handling, and authenticated routing integration.",
                    "assigned_agent": "frontend",
                    "priority": "high",
                    "status": "assigned",
                    "depends_on": ["TASK-001"],
                },
                {
                    "id": "TASK-004",
                    "title": "Build primary dashboard experience",
                    "description": "Propose dashboard screens and data integration points for the requested product.",
                    "assigned_agent": "frontend",
                    "priority": "medium",
                    "status": "assigned",
                    "depends_on": ["TASK-002", "TASK-003"],
                },
            ]
        }

    @classmethod
    def _fallback_backend_implementation(
        cls, request: dict[str, Any]
    ) -> dict[str, Any]:
        task = request.get("current_task", {})
        task_id = task.get("id", "")
        task_title = task.get("title", "").lower()
        if task_id != "TASK-001" and "authentication api" not in task_title:
            return cls._fallback_domain_implementation(request)
        return {
            "task_test_paths": ["backend/tests/test_auth.py"],
            "files": [
                {
                    "path": "backend/requirements.txt",
                    "content": "fastapi\npydantic\npytest\nhttpx\n",
                },
                {
                    "path": "backend/__init__.py",
                    "content": "",
                },
                {
                    "path": "backend/app/__init__.py",
                    "content": "",
                },
                {
                    "path": "backend/app/main.py",
                    "content": """from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Generated Backend")


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/auth/login")
def login(payload: LoginRequest) -> dict[str, str]:
    if not payload.username or not payload.password:
        raise HTTPException(status_code=400, detail="Credentials required")
    return {"access_token": f"local-{payload.username}", "token_type": "bearer"}
""",
                },
                {
                    "path": "backend/tests/test_auth.py",
                    "content": """from fastapi.testclient import TestClient

from backend.app.main import app

client = TestClient(app)


def test_login_returns_local_token() -> None:
    response = client.post(
        "/auth/login",
        json={"username": "student", "password": "secret"},
    )
    assert response.status_code == 200
    assert response.json()["token_type"] == "bearer"
""",
                },
            ]
        }

    @staticmethod
    def _fallback_domain_implementation(request: dict[str, Any]) -> dict[str, Any]:
        existing = request.get("relevant_existing_file_contents", {})
        main_path = "backend/app/main.py"
        main_content = existing.get(
            main_path,
            'from fastapi import FastAPI\n\napp = FastAPI(title="Generated Backend")\n',
        )
        router_import = (
            "from backend.app.routes.students import router as students_router"
        )
        router_registration = "app.include_router(students_router)"
        if router_import not in main_content:
            main_content = f"{router_import}\n{main_content}"
        if router_registration not in main_content:
            main_content = f"{main_content.rstrip()}\n\n{router_registration}\n"

        return {
            "task_test_paths": ["backend/tests/test_students.py"],
            "files": [
                {
                    "path": "backend/app/routes/__init__.py",
                    "content": "",
                },
                {
                    "path": "backend/app/routes/students.py",
                    "content": """from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/students", tags=["students"])


class StudentCreate(BaseModel):
    name: str
    grade: str


@router.post("", status_code=201)
def create_student(student: StudentCreate) -> dict[str, str | int]:
    return {"id": 1, "name": student.name, "grade": student.grade}
""",
                },
                {
                    "path": main_path,
                    "content": main_content,
                },
                {
                    "path": "backend/tests/test_students.py",
                    "content": """from fastapi.testclient import TestClient

from backend.app.main import app

client = TestClient(app)


def test_create_student_returns_domain_record() -> None:
    response = client.post(
        "/students",
        json={"name": "Ada", "grade": "10"},
    )
    assert response.status_code == 201
    assert response.json() == {"id": 1, "name": "Ada", "grade": "10"}


def test_create_student_validates_required_fields() -> None:
    response = client.post("/students", json={"name": "Ada"})
    assert response.status_code == 422
""",
                },
            ],
        }


class GroqLLMService(ProviderLLMService):
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        fallback_api_key: str | None = None,
    ) -> None:
        super().__init__(
            provider="groq",
            api_key=api_key or os.getenv("GROQ_API_KEY"),
            model=model or os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
        )
        self.fallback_api_key = fallback_api_key or os.getenv(
            "GROQ_FALLBACK_API_KEY"
        )
        if self.fallback_api_key == self.api_key:
            self.fallback_api_key = None
        self.base_url = "https://api.groq.com/openai/v1/chat/completions"

    @property
    def configuration_detected(self) -> bool:
        return bool(self.api_key or self.fallback_api_key)

    async def generate_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        if not self.api_key:
            if self.fallback_api_key:
                return await self._generate_with_key(
                    self.fallback_api_key, system_prompt, user_prompt
                )
            return self._fallback_response(user_prompt)
        try:
            return await self._generate_with_key(
                self.api_key, system_prompt, user_prompt
            )
        except (LLMRateLimitError, LLMServiceUnavailableError, httpx.HTTPError):
            if not self.fallback_api_key:
                raise
            logger.warning(
                "LLM: groq primary request failed; switching to configured fallback key"
            )
            return await self._generate_with_key(
                self.fallback_api_key, system_prompt, user_prompt
            )

    async def _generate_with_key(
        self, api_key: str, system_prompt: str, user_prompt: str
    ) -> dict[str, Any]:
        response = await self._post_json(
            url=self.base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            payload={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
            retry_bad_request_once=True,
        )
        content = response.json()["choices"][0]["message"]["content"]
        return json.loads(content)


class GeminiLLMService(ProviderLLMService):
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        super().__init__(
            provider="gemini",
            api_key=api_key or os.getenv("GEMINI_API_KEY"),
            model=model or os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        )
        self.base_url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent"
        )

    async def generate_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        if not self.api_key:
            return self._fallback_response(user_prompt)
        response = await self._post_json(
            url=self.base_url,
            headers={
                "x-goog-api-key": self.api_key,
                "Content-Type": "application/json",
            },
            payload={
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": [
                    {"role": "user", "parts": [{"text": user_prompt}]}
                ],
                "generationConfig": {
                    "temperature": 0.2,
                    "responseMimeType": "application/json",
                },
            },
        )
        content = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(content)


def create_llm_service(provider: str | None = None) -> LLMService:
    selected = (provider or os.getenv("LLM_PROVIDER", "gemini")).strip().lower()
    if selected == "gemini":
        service: ProviderLLMService = GeminiLLMService()
    elif selected == "groq":
        service = GroqLLMService()
    else:
        raise ValueError(
            f"Unsupported LLM_PROVIDER '{selected}'. Expected 'gemini' or 'groq'."
        )
    logger.info("LLM: using provider=%s model=%s", service.provider, service.model)
    if isinstance(service, GroqLLMService) and service.fallback_api_key:
        logger.info("LLM: groq fallback credential configured")
    return service
