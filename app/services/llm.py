from __future__ import annotations

import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable

import httpx

logger = logging.getLogger(__name__)


class LLMService(ABC):
    last_generation_metadata: GenerationRecoveryMetadata | None = None

    @abstractmethod
    async def generate_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """Return a JSON-compatible response from an LLM provider."""

    async def generate_json_with_validation(
        self,
        system_prompt: str,
        user_prompt: str,
        validator: Callable[[dict[str, Any]], None],
        correction_prompt: Callable[
            [LLMImplementationValidationError, dict[str, Any]], tuple[str, str]
        ],
    ) -> dict[str, Any]:
        response = await self.generate_json(system_prompt, user_prompt)
        try:
            validator(response)
        except LLMImplementationValidationError as exc:
            correction_system, correction_user = correction_prompt(exc, response)
            response = await self.generate_json(correction_system, correction_user)
            try:
                validator(response)
            except LLMImplementationValidationError as final_exc:
                self.last_generation_metadata = GenerationRecoveryMetadata(
                    provider_used="service",
                    model_used="unknown",
                    generation_attempts=1,
                    generation_correction_attempts=1,
                    provider_fallback_count=0,
                )
                raise LLMGenerationValidationError(
                    "Generation remained invalid after one bounded correction"
                ) from final_exc
            self.last_generation_metadata = GenerationRecoveryMetadata(
                provider_used="service",
                model_used="unknown",
                generation_attempts=1,
                generation_correction_attempts=1,
                provider_fallback_count=0,
            )
            return response
        except ValueError:
            self.last_generation_metadata = GenerationRecoveryMetadata(
                provider_used="service",
                model_used="unknown",
                generation_attempts=1,
                generation_correction_attempts=0,
                provider_fallback_count=0,
            )
            return response
        self.last_generation_metadata = GenerationRecoveryMetadata(
            provider_used="service",
            model_used="unknown",
            generation_attempts=1,
            generation_correction_attempts=0,
            provider_fallback_count=0,
        )
        return response


class LLMRateLimitError(RuntimeError):
    """Raised when a provider remains rate limited after bounded retries."""


class LLMServiceUnavailableError(RuntimeError):
    """Raised when a provider remains unavailable after bounded retries."""


class LLMRouterUnavailableError(RuntimeError):
    """Raised when every configured provider is temporarily unavailable."""


class LLMStructuredOutputError(ValueError):
    """Raised when a provider returns malformed or non-object JSON."""


class LLMImplementationValidationError(ValueError):
    """Raised when structured generation is valid JSON but not implementable."""


class LLMGenerationValidationError(ValueError):
    """Raised when all bounded provider generation attempts remain invalid."""


@dataclass(frozen=True)
class LLMResponseMetadata:
    provider_used: str
    model_used: str
    fallback_count: int
    retry_count: int


@dataclass(frozen=True)
class GenerationRecoveryMetadata:
    provider_used: str
    model_used: str
    generation_attempts: int
    generation_correction_attempts: int
    provider_fallback_count: int
    escalation_reasons: tuple[str, ...] = ()


class ProviderLLMService(LLMService):
    max_transient_retries = 3

    def __init__(self, provider: str, api_key: str | None, model: str) -> None:
        self.provider = provider
        self.api_key = api_key
        self.model = model
        self.last_retry_count = 0

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
        self.last_retry_count = 0
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                try:
                    response = await client.post(
                        url, headers=headers, json=payload
                    )
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    if transient_retries >= self.max_transient_retries:
                        raise LLMServiceUnavailableError(
                            f"{self.provider} connection failure persisted after "
                            f"{self.max_transient_retries} retries for model {self.model}"
                        ) from exc
                    delay = self._retry_delay(None, transient_retries)
                    logger.warning(
                        "LLM ROUTER: %s connection failure; retrying %s (%d/%d) "
                        "in %.1f seconds",
                        self.provider,
                        self.provider,
                        transient_retries + 1,
                        self.max_transient_retries,
                        delay,
                    )
                    transient_retries += 1
                    self.last_retry_count = transient_retries
                    await asyncio.sleep(delay)
                    continue
                if response.status_code in {429, 500, 502, 503, 504}:
                    logger.warning(
                        "LLM ROUTER: %s returned %d",
                        self.provider,
                        response.status_code,
                    )
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
                    transient_retries += 1
                    self.last_retry_count = transient_retries
                    logger.warning(
                        "LLM ROUTER: retrying %s (%d/%d) in %.1f seconds",
                        self.provider,
                        transient_retries,
                        self.max_transient_retries,
                        delay,
                    )
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
    def _decode_json_object(content: str) -> dict[str, Any]:
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMStructuredOutputError(
                "LLM provider returned malformed structured JSON"
            ) from exc
        if not isinstance(decoded, dict):
            raise LLMStructuredOutputError(
                "LLM provider returned JSON that is not an object"
            )
        return decoded

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
            api_key=os.getenv("GROQ_API_KEY") if api_key is None else api_key,
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
        except (LLMRateLimitError, LLMServiceUnavailableError):
            if not self.fallback_api_key:
                raise
            primary_retry_count = self.last_retry_count
            logger.warning(
                "LLM: groq primary request failed; switching to configured fallback key"
            )
            try:
                return await self._generate_with_key(
                    self.fallback_api_key, system_prompt, user_prompt
                )
            finally:
                self.last_retry_count += primary_retry_count
        except httpx.HTTPStatusError as exc:
            if not self.fallback_api_key or exc.response.status_code not in {401, 403}:
                raise
            logger.warning(
                "LLM: groq primary credential was rejected; switching to configured "
                "fallback key"
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
        return self._decode_json_object(content)


class GeminiLLMService(ProviderLLMService):
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        super().__init__(
            provider="gemini",
            api_key=os.getenv("GEMINI_API_KEY") if api_key is None else api_key,
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
        return self._decode_json_object(content)


class OllamaLLMService(ProviderLLMService):
    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__(
            provider="ollama",
            api_key=None,
            model=model if model is not None else os.getenv("OLLAMA_MODEL", ""),
        )
        self.base_url = (
            base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        ).rstrip("/")

    @property
    def configuration_detected(self) -> bool:
        return bool(self.base_url and self.model)

    async def generate_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        if not self.configuration_detected:
            raise LLMServiceUnavailableError(
                "Ollama is not configured; set OLLAMA_MODEL to enable local fallback"
            )
        response = await self._post_json(
            url=f"{self.base_url}/api/chat",
            headers={"Content-Type": "application/json"},
            payload={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "format": "json",
                "stream": False,
                "options": {"temperature": 0.2},
            },
        )
        content = response.json()["message"]["content"]
        return self._decode_json_object(content)


class LLMRouter(LLMService):
    def __init__(
        self,
        providers: list[ProviderLLMService],
        deterministic_fallback: ProviderLLMService | None = None,
        concurrency_limit: int = 1,
    ) -> None:
        self.providers = providers
        self.deterministic_fallback = deterministic_fallback
        self.last_metadata: LLMResponseMetadata | None = None
        self.last_generation_metadata: GenerationRecoveryMetadata | None = None
        self._request_gate = asyncio.Semaphore(max(1, concurrency_limit))

    @property
    def configuration_detected(self) -> bool:
        return bool(self.providers)

    async def generate_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        task = self._task_name(system_prompt, user_prompt)
        logger.info("LLM ROUTER: task=%s", task)
        async with self._request_gate:
            failures: list[str] = []
            total_retries = 0
            for fallback_count, provider in enumerate(self.providers):
                if fallback_count:
                    logger.warning(
                        "LLM ROUTER: falling back to %s", provider.provider
                    )
                logger.info(
                    "LLM ROUTER: trying provider=%s model=%s",
                    provider.provider,
                    provider.model,
                )
                if provider.provider == "groq" and len((system_prompt + user_prompt).encode("utf-8")) > 12000:
                    failures.append("groq: CONTEXT_TOO_LARGE (request exceeds 12000-byte budget)")
                    logger.warning("LLM ROUTER: groq request exceeds context budget; skipping identical payload")
                    continue
                try:
                    result = await provider.generate_json(system_prompt, user_prompt)
                except httpx.HTTPStatusError as exc:
                    if exc.response is not None and exc.response.status_code == 413:
                        failures.append(f"{provider.provider}: CONTEXT_TOO_LARGE (HTTP 413)")
                        logger.warning("LLM ROUTER: %s returned HTTP 413; not retrying identical payload", provider.provider)
                        continue
                    raise
                except (LLMRateLimitError, LLMServiceUnavailableError) as exc:
                    total_retries += provider.last_retry_count
                    failures.append(f"{provider.provider}: {exc}")
                    logger.warning(
                        "LLM ROUTER: %s unavailable after retries", provider.provider
                    )
                    continue
                total_retries += provider.last_retry_count
                self.last_metadata = LLMResponseMetadata(
                    provider_used=provider.provider,
                    model_used=provider.model,
                    fallback_count=fallback_count,
                    retry_count=total_retries,
                )
                logger.info(
                    "LLM ROUTER: %s request successful", provider.provider
                )
                return result

            if self.deterministic_fallback is not None and not self.providers:
                result = await self.deterministic_fallback.generate_json(
                    system_prompt, user_prompt
                )
                self.last_metadata = LLMResponseMetadata(
                    provider_used="deterministic-local",
                    model_used="built-in",
                    fallback_count=0,
                    retry_count=0,
                )
                return result
            detail = "; ".join(failures) or "no providers are configured"
            raise LLMRouterUnavailableError(
                f"All configured LLM providers are unavailable: {detail}"
            )

    async def generate_json_with_validation(
        self,
        system_prompt: str,
        user_prompt: str,
        validator: Callable[[dict[str, Any]], None],
        correction_prompt: Callable[
            [LLMImplementationValidationError, dict[str, Any]], tuple[str, str]
        ],
    ) -> dict[str, Any]:
        async with self._request_gate:
            failures: list[str] = []
            escalation_reasons: list[str] = []
            generation_attempts = 0
            correction_attempts = 0
            configured = self.providers
            if not configured and self.deterministic_fallback is not None:
                configured = [self.deterministic_fallback]

            for provider_index, provider in enumerate(configured):
                logger.info(
                    "LLM ROUTER: generation provider=%s model=%s",
                    provider.provider,
                    provider.model,
                )
                generation_attempts += 1
                if provider.provider == "groq" and len((system_prompt + user_prompt).encode("utf-8")) > 12000:
                    failures.append("groq: CONTEXT_TOO_LARGE (request exceeds 12000-byte budget)")
                    escalation_reasons.append("CONTEXT_TOO_LARGE")
                    logger.warning("LLM ROUTER: groq request exceeds context budget; skipping identical payload")
                    continue
                try:
                    response = await provider.generate_json(
                        system_prompt, user_prompt
                    )
                except (LLMRateLimitError, LLMServiceUnavailableError) as exc:
                    failures.append(f"{provider.provider}: {exc}")
                    if provider_index + 1 < len(configured):
                        next_provider = configured[provider_index + 1].provider
                        logger.warning(
                            "LLM ROUTER: escalating generation provider %s -> %s "
                            "reason=provider_unavailable",
                            provider.provider,
                            next_provider,
                        )
                    continue
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code if exc.response is not None else "unknown"
                    if status == 413:
                        failures.append(f"{provider.provider}: CONTEXT_TOO_LARGE (HTTP 413)")
                        escalation_reasons.append("CONTEXT_TOO_LARGE")
                        logger.warning("LLM ROUTER: %s returned HTTP 413; not retrying identical payload", provider.provider)
                        continue
                    raise LLMGenerationValidationError(
                        f"{provider.provider} rejected generation with HTTP {status}; check model and request configuration"
                    ) from exc
                except LLMStructuredOutputError as exc:
                    correction_attempts += 1
                    logger.warning(
                        "LLM ROUTER: malformed structured output from %s; requesting bounded correction",
                        provider.provider,
                    )
                    correction_system, correction_user = correction_prompt(
                        LLMImplementationValidationError(str(exc)),
                        {"malformed_response_error": str(exc)},
                    )
                    if provider.provider == "groq" and len((correction_system + correction_user).encode("utf-8")) > 12000:
                        failures.append("groq: CONTEXT_TOO_LARGE (correction exceeds 12000-byte budget)")
                        escalation_reasons.append("CONTEXT_TOO_LARGE")
                        continue
                    try:
                        response = await provider.generate_json(
                            correction_system, correction_user
                        )
                    except httpx.HTTPStatusError as retry_exc:
                        status = retry_exc.response.status_code if retry_exc.response is not None else "unknown"
                        if status == 413:
                            failures.append(f"{provider.provider}: CONTEXT_TOO_LARGE (HTTP 413)")
                            escalation_reasons.append("CONTEXT_TOO_LARGE")
                            logger.warning("LLM ROUTER: %s correction returned HTTP 413; not retrying identical payload", provider.provider)
                        else:
                            raise LLMGenerationValidationError(
                                f"{provider.provider} rejected generation correction with HTTP {status}; check model and request configuration"
                            ) from retry_exc
                    except (LLMRateLimitError, LLMServiceUnavailableError, LLMStructuredOutputError) as retry_exc:
                        failures.append(f"{provider.provider}: {retry_exc}")
                    else:
                        try:
                            validator(response)
                        except ValueError as final_exc:
                            failures.append(f"{provider.provider}: {final_exc}")
                        else:
                            self.last_generation_metadata = GenerationRecoveryMetadata(
                                provider_used=provider.provider,
                                model_used=provider.model,
                                generation_attempts=generation_attempts,
                                generation_correction_attempts=correction_attempts,
                                provider_fallback_count=provider_index,
                                escalation_reasons=tuple(escalation_reasons),
                            )
                            return response
                    if provider_index + 1 < len(configured):
                        escalation_reasons.append("malformed_generation_after_correction")
                    continue
                try:
                    validator(response)
                except LLMImplementationValidationError as exc:
                    logger.warning(
                        "LLM ROUTER: implementation validation failed: %s", exc
                    )
                    correction_attempts += 1
                    logger.info(
                        "LLM ROUTER: generation correction 1 using %s",
                        provider.provider,
                    )
                    correction_system, correction_user = correction_prompt(
                        exc, response
                    )
                    if provider.provider == "groq" and len((correction_system + correction_user).encode("utf-8")) > 12000:
                        failures.append("groq: CONTEXT_TOO_LARGE (correction exceeds 12000-byte budget)")
                        escalation_reasons.append("CONTEXT_TOO_LARGE")
                        continue
                    try:
                        response = await provider.generate_json(
                            correction_system, correction_user
                        )
                    except httpx.HTTPStatusError as retry_exc:
                        status = retry_exc.response.status_code if retry_exc.response is not None else "unknown"
                        if status == 413:
                            failures.append(f"{provider.provider}: CONTEXT_TOO_LARGE (HTTP 413)")
                            escalation_reasons.append("CONTEXT_TOO_LARGE")
                            logger.warning("LLM ROUTER: %s correction returned HTTP 413; not retrying identical payload", provider.provider)
                        else:
                            raise LLMGenerationValidationError(
                                f"{provider.provider} rejected generation correction with HTTP {status}; check model and request configuration"
                            ) from retry_exc
                    except (LLMRateLimitError, LLMServiceUnavailableError) as retry_exc:
                        failures.append(f"{provider.provider}: {retry_exc}")
                    else:
                        try:
                            validator(response)
                        except ValueError as final_exc:
                            failures.append(f"{provider.provider}: {final_exc}")
                        else:
                            self.last_generation_metadata = GenerationRecoveryMetadata(
                                provider_used=provider.provider,
                                model_used=provider.model,
                                generation_attempts=generation_attempts,
                                generation_correction_attempts=correction_attempts,
                                provider_fallback_count=provider_index,
                                escalation_reasons=tuple(escalation_reasons),
                            )
                            return response
                    if provider_index + 1 < len(configured):
                        next_provider = configured[provider_index + 1].provider
                        reason = "invalid_generation_after_correction"
                        escalation_reasons.append(reason)
                        logger.warning(
                            "LLM ROUTER: escalating generation provider %s -> %s "
                            "reason=%s",
                            provider.provider,
                            next_provider,
                            reason,
                        )
                    continue
                except ValueError:
                    self.last_generation_metadata = GenerationRecoveryMetadata(
                        provider_used=provider.provider,
                        model_used=provider.model,
                        generation_attempts=generation_attempts,
                        generation_correction_attempts=correction_attempts,
                        provider_fallback_count=provider_index,
                        escalation_reasons=tuple(escalation_reasons),
                    )
                    return response
                self.last_generation_metadata = GenerationRecoveryMetadata(
                    provider_used=provider.provider,
                    model_used=provider.model,
                    generation_attempts=generation_attempts,
                    generation_correction_attempts=correction_attempts,
                    provider_fallback_count=provider_index,
                    escalation_reasons=tuple(escalation_reasons),
                )
                return response

            detail = "; ".join(failures) or "no providers are configured"
            self.last_generation_metadata = GenerationRecoveryMetadata(
                provider_used="",
                model_used="",
                generation_attempts=generation_attempts,
                generation_correction_attempts=correction_attempts,
                provider_fallback_count=max(0, len(configured) - 1),
                escalation_reasons=tuple(escalation_reasons),
            )
            raise LLMGenerationValidationError(
                f"All bounded generation providers failed validation: {detail}"
            )

    @staticmethod
    def _task_name(system_prompt: str, user_prompt: str) -> str:
        try:
            operation = json.loads(user_prompt).get("operation")
        except (json.JSONDecodeError, AttributeError):
            operation = None
        if operation == "backend_implementation":
            return "backend_implementation"
        if "Engineering Manager" in system_prompt:
            return "manager_planning"
        return operation or "structured_generation"


def _provider_from_name(name: str) -> ProviderLLMService:
    if name == "gemini":
        return GeminiLLMService()
    if name == "groq":
        return GroqLLMService()
    if name == "ollama":
        return OllamaLLMService()
    raise ValueError(
        f"Unsupported LLM provider '{name}'. Expected gemini, groq, or ollama."
    )


def create_llm_service(provider: str | None = None) -> LLMRouter:
    configured_order = (
        [provider]
        if provider
        else [
            os.getenv("LLM_PRIMARY_PROVIDER", "gemini"),
            os.getenv("LLM_SECONDARY_PROVIDER", "groq"),
            os.getenv(
                "LLM_TERTIARY_PROVIDER",
                os.getenv("LLM_LOCAL_PROVIDER", "ollama"),
            ),
        ]
    )
    providers: list[ProviderLLMService] = []
    seen: set[str] = set()
    for configured_name in configured_order:
        name = configured_name.strip().lower()
        if not name or name in seen:
            continue
        seen.add(name)
        candidate = _provider_from_name(name)
        if candidate.configuration_detected:
            providers.append(candidate)
            logger.info(
                "LLM ROUTER: configured provider=%s model=%s",
                candidate.provider,
                candidate.model,
            )
        elif name == "ollama":
            logger.info("LLM: Ollama unavailable; local fallback disabled")
        else:
            logger.info("LLM ROUTER: provider=%s is not configured; skipping", name)

    return LLMRouter(
        providers=providers,
        deterministic_fallback=GeminiLLMService(api_key=""),
    )
