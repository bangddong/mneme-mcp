import httpx

import pytest


def test_is_available_requests_models_at_configured_base_url(monkeypatch):
    from mneme.providers.openai_compatible import OpenAICompatibleProvider

    seen = {}

    def get(url, timeout):
        seen.update(url=url, timeout=timeout)
        return httpx.Response(200)

    monkeypatch.setenv("LLM_BASE_URL", "http://example.test/v1/")
    monkeypatch.setattr(httpx, "get", get)

    assert OpenAICompatibleProvider().is_available() is True
    assert seen == {"url": "http://example.test/v1/models", "timeout": 5.0}


def test_is_available_returns_false_for_non_200_or_network_error(monkeypatch):
    from mneme.providers.openai_compatible import OpenAICompatibleProvider

    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: httpx.Response(503))
    assert OpenAICompatibleProvider().is_available() is False

    def unavailable(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "get", unavailable)
    assert OpenAICompatibleProvider().is_available() is False


def test_complete_posts_openai_payload_and_returns_trimmed_content(monkeypatch):
    from mneme.providers.openai_compatible import OpenAICompatibleProvider

    seen = {}

    def post(url, json, timeout):
        seen.update(url=url, json=json, timeout=timeout)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "  answer  "}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setenv("LLM_BASE_URL", "http://example.test/v1/")
    monkeypatch.setenv("LLM_MODEL", "model-under-test")
    monkeypatch.setattr(httpx, "post", post)

    assert OpenAICompatibleProvider().complete("system prompt", "user prompt") == "answer"
    assert seen == {
        "url": "http://example.test/v1/chat/completions",
        "timeout": 120.0,
        "json": {
            "model": "model-under-test",
            "messages": [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "user prompt"},
            ],
            "temperature": 0,
            "stream": False,
        },
    }


def test_complete_requests_json_object_mode(monkeypatch):
    from mneme.providers.openai_compatible import OpenAICompatibleProvider

    seen = {}

    def post(url, json, timeout):
        seen["json"] = json
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{}"}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", post)

    assert OpenAICompatibleProvider().complete("system", "user", json_mode=True) == "{}"
    assert seen["json"]["response_format"] == {"type": "json_object"}


def test_complete_wraps_transport_and_response_errors(monkeypatch):
    from mneme.providers.openai_compatible import OpenAICompatibleProvider

    def unavailable(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "post", unavailable)

    with pytest.raises(RuntimeError, match="^local LLM call failed: offline$") as exc_info:
        OpenAICompatibleProvider().complete("system", "user")

    assert isinstance(exc_info.value.__cause__, httpx.ConnectError)


def test_legacy_call_uses_the_current_timeout_compatibility_seam(monkeypatch):
    from mneme import llm

    seen = {}

    def post(url, json, timeout):
        seen["timeout"] = timeout
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "answer"}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(llm, "_TIMEOUT", 7.5)

    assert llm._call("system", "user") == "answer"
    assert seen["timeout"] == 7.5
