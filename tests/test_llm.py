import asyncio

import httpx
import pytest

from app.services.llm import (
    GeminiLLMService,
    GroqLLMService,
    LLMRouter,
    LLMGenerationValidationError,
    LLMImplementationValidationError,
    LLMRouterUnavailableError,
    LLMRateLimitError,
    LLMServiceUnavailableError,
    LLMStructuredOutputError,
    OllamaLLMService,
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
        item = type(self).responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class MockProvider:
    def __init__(self, provider: str, outcomes: list, model: str = "test-model"):
        self.provider = provider
        self.model = model
        self.outcomes = list(outcomes)
        self.calls = 0
        self.last_retry_count = 0

    async def generate_json(self, system_prompt: str, user_prompt: str) -> dict:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


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
    monkeypatch.delenv("LLM_PRIMARY_PROVIDER", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test-model")

    service = create_llm_service()

    assert isinstance(service, LLMRouter)
    assert [provider.provider for provider in service.providers] == ["gemini"]
    assert service.providers[0].model == "gemini-test-model"


def test_provider_selection_supports_groq(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "groq-test-key")
    monkeypatch.setenv("GROQ_MODEL", "groq-test-model")

    service = create_llm_service(provider="groq")

    assert isinstance(service, LLMRouter)
    assert [provider.provider for provider in service.providers] == ["groq"]
    assert service.providers[0].model == "groq-test-model"


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


def test_router_gemini_success_does_not_call_fallbacks() -> None:
    gemini = MockProvider("gemini", [{"status": "ok"}])
    groq = MockProvider("groq", [{"status": "unused"}])
    ollama = MockProvider("ollama", [{"status": "unused"}])
    router = LLMRouter([gemini, groq, ollama])

    result = asyncio.run(router.generate_json("manager", "requirement"))

    assert result == {"status": "ok"}
    assert [gemini.calls, groq.calls, ollama.calls] == [1, 0, 0]
    assert router.last_metadata
    assert router.last_metadata.provider_used == "gemini"
    assert router.last_metadata.fallback_count == 0


@pytest.mark.parametrize(
    "failure",
    [
        LLMRateLimitError("gemini rate limited"),
        LLMServiceUnavailableError("gemini timed out"),
        LLMServiceUnavailableError("gemini returned 500"),
    ],
)
def test_router_falls_back_from_gemini_to_groq_on_retryable_failure(
    failure: Exception,
) -> None:
    gemini = MockProvider("gemini", [failure])
    gemini.last_retry_count = 3
    groq = MockProvider("groq", [{"provider": "groq"}])
    ollama = MockProvider("ollama", [{"provider": "ollama"}])
    router = LLMRouter([gemini, groq, ollama])

    result = asyncio.run(router.generate_json("system", "user"))

    assert result == {"provider": "groq"}
    assert [gemini.calls, groq.calls, ollama.calls] == [1, 1, 0]
    assert router.last_metadata
    assert router.last_metadata.provider_used == "groq"
    assert router.last_metadata.fallback_count == 1
    assert router.last_metadata.retry_count == 3


def test_router_falls_back_to_ollama_after_cloud_failures() -> None:
    gemini = MockProvider(
        "gemini", [LLMServiceUnavailableError("gemini unavailable")]
    )
    groq = MockProvider("groq", [LLMRateLimitError("groq rate limited")])
    ollama = MockProvider("ollama", [{"provider": "ollama"}], model="qwen")
    router = LLMRouter([gemini, groq, ollama])

    result = asyncio.run(router.generate_json("system", "user"))

    assert result == {"provider": "ollama"}
    assert [gemini.calls, groq.calls, ollama.calls] == [1, 1, 1]
    assert router.last_metadata
    assert router.last_metadata.provider_used == "ollama"
    assert router.last_metadata.fallback_count == 2


def test_router_returns_controlled_error_when_all_providers_unavailable() -> None:
    providers = [
        MockProvider("gemini", [LLMServiceUnavailableError("offline")]),
        MockProvider("groq", [LLMRateLimitError("limited")]),
        MockProvider("ollama", [LLMServiceUnavailableError("not running")]),
    ]

    with pytest.raises(LLMRouterUnavailableError, match="All configured"):
        asyncio.run(LLMRouter(providers).generate_json("system", "user"))

    assert [provider.calls for provider in providers] == [1, 1, 1]


def test_malformed_structured_output_does_not_fallback(monkeypatch) -> None:
    install_http_fakes(
        monkeypatch,
        [
            response(
                200,
                body={
                    "candidates": [
                        {"content": {"parts": [{"text": "not-json"}]}}
                    ]
                },
            )
        ],
    )
    gemini = GeminiLLMService(api_key="gemini-secret")
    groq = MockProvider("groq", [{"status": "must-not-run"}])

    with pytest.raises(LLMStructuredOutputError):
        asyncio.run(LLMRouter([gemini, groq]).generate_json("system", "user"))

    assert groq.calls == 0


def test_invalid_http_400_does_not_fallback(monkeypatch) -> None:
    install_http_fakes(monkeypatch, [response(400)])
    gemini = GeminiLLMService(api_key="gemini-secret")
    groq = MockProvider("groq", [{"status": "must-not-run"}])

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(LLMRouter([gemini, groq]).generate_json("system", "user"))

    assert FakeAsyncClient.calls == 1
    assert groq.calls == 0


def test_groq_http_400_does_not_switch_keys_or_providers(monkeypatch) -> None:
    install_http_fakes(monkeypatch, [response(400), response(400)])
    groq = GroqLLMService(
        api_key="primary-secret", fallback_api_key="secondary-secret"
    )
    ollama = MockProvider("ollama", [{"status": "must-not-run"}])

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(LLMRouter([groq, ollama]).generate_json("system", "user"))

    assert FakeAsyncClient.calls == 2
    assert set(FakeAsyncClient.authorization_headers) == {"Bearer primary-secret"}
    assert ollama.calls == 0


def test_ollama_normalizes_structured_json_response(monkeypatch) -> None:
    install_http_fakes(
        monkeypatch,
        [
            response(
                200,
                body={"message": {"content": '{"provider": "ollama"}'}},
            )
        ],
    )
    ollama = OllamaLLMService(
        base_url="http://localhost:11434", model="local-test-model"
    )

    result = asyncio.run(ollama.generate_json("system", "user"))

    assert result == {"provider": "ollama"}


def test_ollama_not_running_is_a_controlled_router_failure(monkeypatch) -> None:
    request = httpx.Request("POST", "http://localhost:11434/api/chat")
    install_http_fakes(
        monkeypatch,
        [httpx.ConnectError("connection refused", request=request) for _ in range(4)],
    )
    ollama = OllamaLLMService(
        base_url="http://localhost:11434", model="local-test-model"
    )

    with pytest.raises(LLMRouterUnavailableError, match="ollama"):
        asyncio.run(LLMRouter([ollama]).generate_json("system", "user"))

    assert FakeAsyncClient.calls == 4


def test_api_keys_never_appear_in_router_logs(monkeypatch, caplog) -> None:
    secret = "super-secret-groq-key"
    install_http_fakes(
        monkeypatch,
        [
            response(
                200,
                body={"choices": [{"message": {"content": '{"ok": true}'}}]},
            )
        ],
    )
    router = LLMRouter([GroqLLMService(api_key=secret)])

    with caplog.at_level("INFO"):
        result = asyncio.run(router.generate_json("system", "user"))

    assert result == {"ok": True}
    assert secret not in caplog.text


def test_router_serializes_concurrent_requests() -> None:
    class GateProvider:
        provider = "gemini"
        model = "gate-test"
        last_retry_count = 0

        def __init__(self) -> None:
            self.active = 0
            self.maximum_active = 0

        async def generate_json(self, system_prompt: str, user_prompt: str) -> dict:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            await asyncio.sleep(0)
            self.active -= 1
            return {"user": user_prompt}

    provider = GateProvider()
    router = LLMRouter([provider])

    async def run_requests():
        return await asyncio.gather(
            router.generate_json("system", "one"),
            router.generate_json("system", "two"),
        )

    results = asyncio.run(run_requests())

    assert results == [{"user": "one"}, {"user": "two"}]
    assert provider.maximum_active == 1


def _require_valid_generation(response: dict) -> None:
    if not response.get("valid"):
        raise LLMImplementationValidationError("no implementation files")


def _generation_correction_prompt(error, response):
    return "correct generation", f"validation error: {error}"


def test_invalid_gemini_generation_escalates_to_ollama() -> None:
    gemini = MockProvider("gemini", [{"valid": False}, {"valid": False}])
    ollama = MockProvider("ollama", [{"valid": True}], model="qwen2.5-coder:3b")
    groq = MockProvider("groq", [{"valid": True}])
    router = LLMRouter([gemini, ollama, groq])

    result = asyncio.run(
        router.generate_json_with_validation(
            "system",
            "user",
            _require_valid_generation,
            _generation_correction_prompt,
        )
    )

    assert result == {"valid": True}
    assert [gemini.calls, ollama.calls, groq.calls] == [2, 1, 0]
    assert router.last_generation_metadata
    assert router.last_generation_metadata.provider_used == "ollama"
    assert router.last_generation_metadata.generation_attempts == 2
    assert router.last_generation_metadata.generation_correction_attempts == 1
    assert router.last_generation_metadata.provider_fallback_count == 1
    assert router.last_generation_metadata.escalation_reasons == (
        "invalid_generation_after_correction",
    )


def test_unavailable_ollama_generation_moves_to_groq() -> None:
    gemini = MockProvider("gemini", [{"valid": False}, {"valid": False}])
    ollama = MockProvider(
        "ollama", [LLMServiceUnavailableError("not running")], model="qwen"
    )
    groq = MockProvider("groq", [{"valid": True}])
    router = LLMRouter([gemini, ollama, groq])

    result = asyncio.run(
        router.generate_json_with_validation(
            "system",
            "user",
            _require_valid_generation,
            _generation_correction_prompt,
        )
    )

    assert result == {"valid": True}
    assert [gemini.calls, ollama.calls, groq.calls] == [2, 1, 1]
    assert router.last_generation_metadata
    assert router.last_generation_metadata.provider_used == "groq"
    assert router.last_generation_metadata.provider_fallback_count == 2


def test_valid_gemini_generation_does_not_invoke_ollama() -> None:
    gemini = MockProvider("gemini", [{"valid": True}])
    ollama = MockProvider("ollama", [{"valid": True}])
    router = LLMRouter([gemini, ollama])

    result = asyncio.run(
        router.generate_json_with_validation(
            "system",
            "user",
            _require_valid_generation,
            _generation_correction_prompt,
        )
    )

    assert result == {"valid": True}
    assert [gemini.calls, ollama.calls] == [1, 0]
    assert router.last_generation_metadata
    assert router.last_generation_metadata.generation_correction_attempts == 0
    assert router.last_generation_metadata.provider_fallback_count == 0


def test_malformed_structured_output_uses_bounded_correction() -> None:
    gemini = MockProvider(
        "gemini",
        [LLMStructuredOutputError("malformed"), {"valid": True}],
    )
    router = LLMRouter([gemini])

    result = asyncio.run(
        router.generate_json_with_validation(
            "system",
            "user",
            _require_valid_generation,
            _generation_correction_prompt,
        )
    )

    assert result == {"valid": True}
    assert gemini.calls == 2
    assert router.last_generation_metadata
    assert router.last_generation_metadata.generation_correction_attempts == 1


def test_invalid_generation_provider_fallback_is_bounded() -> None:
    providers = [
        MockProvider("gemini", [{"valid": False}, {"valid": False}]),
        MockProvider("ollama", [{"valid": False}, {"valid": False}]),
        MockProvider("groq", [{"valid": False}, {"valid": False}]),
    ]
    router = LLMRouter(providers)

    with pytest.raises(
        LLMGenerationValidationError, match="bounded generation providers"
    ):
        asyncio.run(
            router.generate_json_with_validation(
                "system",
                "user",
                _require_valid_generation,
                _generation_correction_prompt,
            )
        )

    assert [provider.calls for provider in providers] == [2, 2, 2]
    assert router.last_generation_metadata
    assert router.last_generation_metadata.generation_attempts == 3
    assert router.last_generation_metadata.generation_correction_attempts == 3
    assert router.last_generation_metadata.provider_fallback_count == 2


def test_provider_selection_supports_configurable_tertiary(monkeypatch) -> None:
    monkeypatch.setenv("LLM_PRIMARY_PROVIDER", "gemini")
    monkeypatch.setenv("LLM_SECONDARY_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_TERTIARY_PROVIDER", "groq")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5-coder:3b")
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")

    service = create_llm_service()

    assert [provider.provider for provider in service.providers] == [
        "gemini",
        "ollama",
        "groq",
    ]
