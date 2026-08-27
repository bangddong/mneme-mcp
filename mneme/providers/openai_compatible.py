"""OpenAI-compatible HTTP transport for the optional local generation model."""

import os

import httpx


class OpenAICompatibleProvider:
    """Call an OpenAI-compatible local completion endpoint."""

    _timeout = 120.0

    def _base_url(self) -> str:
        return os.getenv("LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")

    def _model(self) -> str:
        return os.getenv("LLM_MODEL", "qwen2.5:7b")

    def is_available(self) -> bool:
        try:
            response = httpx.get(f"{self._base_url()}/models", timeout=5.0)
            return response.status_code == 200
        except Exception:
            return False

    def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        payload = {
            "model": self._model(),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        try:
            response = httpx.post(
                f"{self._base_url()}/chat/completions",
                json=payload,
                timeout=self._timeout,
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            raise RuntimeError(f"local LLM call failed: {exc}") from exc
