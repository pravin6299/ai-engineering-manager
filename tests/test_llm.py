import asyncio

import httpx
import pytest

from app.services.llm import (
    GeminiLLMService,
    GroqLLMService,
    LLMRateLimitError,
    LLMServiceUnavailableError,
    create_llm_service,
)


class FakeAsyncClient:
    responses: list[httpx.Response] = []
    calls = 0
    authorization_headers: list[str] = []

    def __init__(self, timeout: int) -> None:
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def post(self, url: str, headers: dict, json: dict) -> httpx.Response:
        type(self).calls += 1
        type(self).authorization_headers.append(headers.get("Authorization", ""))
        return type(self).responses.pop(0)


def response(status: int, *, headers: dict | None = None, body: dict | None = None):
    request = httpx.Request("POST", "https://provider.example/generate")
    return httpx.Response(
        status,
        request=request,
        headers=headers,
        json=body,
    )


def install_http_fakes(monkeypatch, responses: list[httpx.Response]):
    sleep_calls: list[float] = []
    FakeAsyncClient.responses = responses
    FakeAsyncClient.calls = 0
    FakeAsyncClient.authorization_headers = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr("app.services.llm.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("app.services.llm.asyncio.sleep", fake_sleep)
    return sleep_calls


def test_provider_selection_defaults_to_gemini(monkeypatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test-model")

    service = create_llm_service()

    assert isinstance(service, GeminiLLMService)
    assert service.provider == "gemini"
    assert service.model == "gemini-test-model"


def test_provider_selection_supports_groq(monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "groq-test-key")
    monkeypatch.setenv("GROQ_MODEL", "groq-test-model")

    service = create_llm_service()

    assert isinstance(service, GroqLLMService)
    assert service.provider == "groq"
    assert service.model == "groq-test-model"


def test_groq_detects_fallback_only_configuration(monkeypatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_FALLBACK_API_KEY", "fallback-test-key")

    service = GroqLLMService()

    assert service.configuration_detected


def test_gemini_respects_retry_after_on_429(monkeypatch) -> None:
    sleep_calls = install_http_fakes(
        monkeypatch,
        [
            response(429, headers={"Retry-After": "2"}),
            response(
                200,
                body={
                    "candidates": [
                        {
                            "content": {
                                "parts": [{"text": '{"status": "ok"}'}]
                            }
                        }
                    ]
                },
            ),
        ],
    )

    result = asyncio.run(
        GeminiLLMService(api_key="test-key").generate_json(
            "Return JSON.", "Return an object."
        )
    )

    assert result == {"status": "ok"}
    assert sleep_calls == [2.0]
    assert FakeAsyncClient.calls == 2


def test_rate_limit_retries_are_bounded(monkeypatch) -> None:
    sleep_calls = install_http_fakes(
        monkeypatch,
        [response(429) for _ in range(4)],
    )

    with pytest.raises(LLMRateLimitError, match="after 3 retries"):
        asyncio.run(
            GeminiLLMService(api_key="test-key").generate_json(
                "Return JSON.", "Return an object."
            )
        )

    assert sleep_calls == [1.0, 2.0, 4.0]
    assert FakeAsyncClient.calls == 4


def test_service_unavailable_retries_then_recovers(monkeypatch) -> None:
    sleep_calls = install_http_fakes(
        monkeypatch,
        [
            response(503, headers={"Retry-After": "1.5"}),
            response(
                200,
                body={
                    "candidates": [
                        {
                            "content": {
                                "parts": [{"text": '{"status": "ok"}'}]
                            }
                        }
                    ]
                },
            ),
        ],
    )

    result = asyncio.run(
        GeminiLLMService(api_key="test-key").generate_json(
            "Return JSON.", "Return an object."
        )
    )

    assert result == {"status": "ok"}
    assert sleep_calls == [1.5]
    assert FakeAsyncClient.calls == 2


def test_service_unavailable_retries_are_bounded(monkeypatch) -> None:
    sleep_calls = install_http_fakes(
        monkeypatch,
        [response(503) for _ in range(4)],
    )

    with pytest.raises(LLMServiceUnavailableError, match="after 3 retries"):
        asyncio.run(
            GeminiLLMService(api_key="test-key").generate_json(
                "Return JSON.", "Return an object."
            )
        )

    assert sleep_calls == [1.0, 2.0, 4.0]
    assert FakeAsyncClient.calls == 4


def test_groq_retries_once_after_400(monkeypatch) -> None:
    sleep_calls = install_http_fakes(
        monkeypatch,
        [
            response(400),
            response(
                200,
                body={
                    "choices": [
                        {"message": {"content": '{"status": "ok"}'}}
                    ]
                },
            ),
        ],
    )

    result = asyncio.run(
        GroqLLMService(api_key="test-key").generate_json(
            "Return JSON.", "Return an object."
        )
    )

    assert result == {"status": "ok"}
    assert sleep_calls == [1]
    assert FakeAsyncClient.calls == 2


def test_groq_uses_fallback_key_after_primary_retries_are_exhausted(
    monkeypatch,
) -> None:
    sleep_calls = install_http_fakes(
        monkeypatch,
        [
            response(429),
            response(429),
            response(429),
            response(429),
            response(
                200,
                body={
                    "choices": [
                        {"message": {"content": '{"status": "fallback"}'}}
                    ]
                },
            ),
        ],
    )

    result = asyncio.run(
        GroqLLMService(
            api_key="primary-key",
            fallback_api_key="secondary-key",
        ).generate_json("Return JSON.", "Return an object.")
    )

    assert result == {"status": "fallback"}
    assert sleep_calls == [1.0, 2.0, 4.0]
    assert FakeAsyncClient.authorization_headers == [
        "Bearer primary-key",
        "Bearer primary-key",
        "Bearer primary-key",
        "Bearer primary-key",
        "Bearer secondary-key",
    ]


def test_groq_does_not_retry_same_key_as_fallback(monkeypatch) -> None:
    install_http_fakes(monkeypatch, [response(401)])

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(
            GroqLLMService(
                api_key="same-key",
                fallback_api_key="same-key",
            ).generate_json("Return JSON.", "Return an object.")
        )

    assert FakeAsyncClient.calls == 1
