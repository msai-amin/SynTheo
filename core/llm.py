"""The single LLM client module. [LOCKED — repo layout 1.2]

No other file may import an OpenAI-SDK-style client or talk HTTP to a vLLM endpoint
directly. Everything routes through `LLMClient`, which gives every caller the same
retries, timeouts, token accounting, and per-call trace log for free.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """Raised when a model call exhausts its retries."""


@dataclass
class Sample:
    alias: str
    text: str
    finish_reason: str | None
    tokens_in: int
    tokens_out: int
    latency_ms: float
    temperature: float
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


def load_config(repo_root: Path = REPO_ROOT) -> dict:
    with open(repo_root / "config" / "syntheo.yaml") as f:
        return yaml.safe_load(f)


class LLMClient:
    """Async, batched, logged client for the model roster in config/syntheo.yaml."""

    def __init__(self, config: dict | None = None, repo_root: Path = REPO_ROOT) -> None:
        self.repo_root = repo_root
        self.config = config or load_config(repo_root)
        self._models: dict[str, dict] = {
            alias: m for alias, m in self.config["models"].items() if alias != "super_swap"
        }
        self.trace_dir = repo_root / self.config["logging"]["trace_dir"]
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self._client = httpx.AsyncClient()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    def _base_url(self, alias: str) -> str:
        port = self._models[alias]["port"]
        return f"http://localhost:{port}/v1"

    def _timeout(self, alias: str) -> float:
        return float(self._models[alias]["timeout_s"])

    async def complete(
        self,
        alias: str,
        messages: list[dict[str, str]],
        *,
        n: int = 1,
        temperature: float = 1.0,
        max_tokens: int | None = None,
        max_retries: int = 3,
        extra_body: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> list[Sample]:
        """One HTTP call to `alias`'s /v1/chat/completions, requesting `n` completions.

        Retries on connection errors and retryable HTTP status codes with exponential
        backoff. Every call (success or final failure) is appended to the trace log.
        """
        if alias not in self._models:
            raise LLMError(f"unknown model alias: {alias!r}")

        served_name = self._models[alias]["served_model_name"]
        payload: dict[str, Any] = {
            "model": served_name,
            "messages": messages,
            "n": n,
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if extra_body:
            payload.update(extra_body)

        url = f"{self._base_url(alias)}/chat/completions"
        timeout = self._timeout(alias)
        call_id = str(uuid.uuid4())
        started = time.monotonic()

        last_exc: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                resp = await self._client.post(url, json=payload, timeout=timeout)
                if resp.status_code in _RETRYABLE_STATUS:
                    raise httpx.HTTPStatusError(
                        f"retryable status {resp.status_code}", request=resp.request, response=resp
                    )
                resp.raise_for_status()
                data = resp.json()
                latency_ms = (time.monotonic() - started) * 1000
                samples = self._to_samples(alias, data, temperature, latency_ms)
                self._log_trace(
                    call_id=call_id,
                    run_id=run_id,
                    alias=alias,
                    attempt=attempt,
                    payload=payload,
                    response=data,
                    latency_ms=latency_ms,
                    error=None,
                )
                return samples
            except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if attempt == max_retries:
                    break
                await asyncio.sleep(min(2 ** (attempt - 1) * 0.5, 8.0))

        latency_ms = (time.monotonic() - started) * 1000
        self._log_trace(
            call_id=call_id,
            run_id=run_id,
            alias=alias,
            attempt=max_retries,
            payload=payload,
            response=None,
            latency_ms=latency_ms,
            error=str(last_exc),
        )
        raise LLMError(f"{alias}: exhausted {max_retries} retries: {last_exc}") from last_exc

    async def batch_complete(
        self,
        requests: list[tuple[str, list[dict[str, str]], dict[str, Any]]],
        run_id: str | None = None,
    ) -> list[Sample | LLMError]:
        """Fire many `complete()` calls concurrently.

        `requests` is (alias, messages, kwargs) tuples. Returns results in the same
        order; a failed call is returned as its LLMError rather than raised, so one
        bad sample doesn't cancel the rest of the burst.
        """

        async def _one(alias: str, messages: list[dict[str, str]], kwargs: dict[str, Any]):
            try:
                return await self.complete(alias, messages, run_id=run_id, **kwargs)
            except LLMError as exc:
                return exc

        results = await asyncio.gather(*(_one(a, m, k) for a, m, k in requests))
        # complete() returns list[Sample] (n may be >1); flatten while preserving errors.
        flat: list[Sample | LLMError] = []
        for r in results:
            if isinstance(r, LLMError):
                flat.append(r)
            else:
                flat.extend(r)
        return flat

    @staticmethod
    def _to_samples(
        alias: str, data: dict[str, Any], temperature: float, latency_ms: float
    ) -> list[Sample]:
        usage = data.get("usage", {})
        tokens_in = usage.get("prompt_tokens", 0)
        tokens_out_total = usage.get("completion_tokens", 0)
        choices = data.get("choices", [])
        n_choices = max(len(choices), 1)
        tokens_out_each = tokens_out_total // n_choices if n_choices else tokens_out_total
        samples = []
        for choice in choices:
            text = choice.get("message", {}).get("content", "")
            samples.append(
                Sample(
                    alias=alias,
                    text=text,
                    finish_reason=choice.get("finish_reason"),
                    tokens_in=tokens_in,
                    tokens_out=tokens_out_each,
                    latency_ms=latency_ms,
                    temperature=temperature,
                    raw=data,
                )
            )
        return samples

    def _log_trace(
        self,
        *,
        call_id: str,
        run_id: str | None,
        alias: str,
        attempt: int,
        payload: dict[str, Any],
        response: dict[str, Any] | None,
        latency_ms: float,
        error: str | None,
    ) -> None:
        record = {
            "call_id": call_id,
            "run_id": run_id,
            "alias": alias,
            "attempt": attempt,
            "ts": time.time(),
            "latency_ms": latency_ms,
            "request": {k: v for k, v in payload.items() if k != "messages"},
            "prompt_chars": sum(len(m.get("content", "")) for m in payload["messages"]),
            "usage": (response or {}).get("usage"),
            "error": error,
        }
        trace_file = self.trace_dir / f"{time.strftime('%Y-%m-%d')}.jsonl"
        with open(trace_file, "a") as f:
            f.write(json.dumps(record) + "\n")

    async def healthy(self, alias: str, timeout: float = 5.0) -> bool:
        try:
            r = await self._client.get(f"http://localhost:{self._models[alias]['port']}/health", timeout=timeout)
            return r.status_code == 200
        except httpx.HTTPError:
            return False
