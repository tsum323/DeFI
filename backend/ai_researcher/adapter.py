"""Language-model adapters. The research loop depends only on `LLMAdapter`.

AnthropicAdapter uses Claude via the official SDK with structured outputs
(pydantic schemas) and the server-side refusal fallback. The model and the API
key environment variable come from config/lab.yaml (`ai_researcher`).
"""

from __future__ import annotations

import os
from typing import Protocol

from backend.ai_researcher.schema import Commentary, HypothesisBatch


class LLMError(RuntimeError):
    pass


class LLMAdapter(Protocol):
    model: str

    def propose(self, system: str, context: str, n: int) -> HypothesisBatch: ...

    def comment(self, system: str, results: str) -> Commentary: ...


class AnthropicAdapter:
    def __init__(self, model: str, api_key_env: str = "ANTHROPIC_API_KEY", effort: str = "high"):
        import anthropic
        key = os.environ.get(api_key_env)
        if not key:
            raise LLMError(f"environment variable {api_key_env} is not set")
        self.model, self.effort = model, effort
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=key)

    def _parse(self, system: str, user: str, schema):
        a = self._anthropic
        try:
            # thinking is always on for claude-opus-5-5 (adaptive); effort controls depth.
            # Server-side fallback re-runs a safety-declined request on another model.
            resp = self.client.beta.messages.parse(
                model=self.model, max_tokens=16000, system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": self.effort}, output_format=schema,
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            )
        except a.BadRequestError as exc:
            raise LLMError(f"bad request: {exc.message}") from exc
        except a.AuthenticationError as exc:
            raise LLMError("invalid API key") from exc
        except a.RateLimitError as exc:
            raise LLMError("rate limited, try again later") from exc
        except a.APIStatusError as exc:
            raise LLMError(f"API error {exc.status_code}: {exc.message}") from exc
        except a.APIConnectionError as exc:
            raise LLMError("network error reaching the API") from exc
        if resp.stop_reason == "refusal":
            raise LLMError("the model declined the request")
        if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
            raise LLMError(f"no valid structured output (stop_reason={resp.stop_reason})")
        return resp.parsed_output

    def propose(self, system: str, context: str, n: int) -> HypothesisBatch:
        return self._parse(system, f"{context}\n\nPropose up to {n} hypotheses.", HypothesisBatch)

    def comment(self, system: str, results: str) -> Commentary:
        return self._parse(system, results, Commentary)
