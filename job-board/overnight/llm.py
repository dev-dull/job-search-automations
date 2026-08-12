"""llama-swap client for grammar-constrained JSON.

Everything here exists to make the local model emit JSON that always parses and
always matches the schema, so the prescreen never has to guess at malformed
output at 3am.

Two hard-won details, both verified against the box on 2026-08-02:

1. `chat_template_kwargs={"enable_thinking": false}` is REQUIRED. Without it the
   Qwen models emit a `reasoning_content` block, burn the whole token budget on
   it, and return `content: ""` with `finish_reason: "length"`. It fails as an
   empty string, not as an error, so it is easy to mistake for a bad prompt.

2. Schema property ORDER matters. Grammar-constrained decoding emits fields in
   schema order, so putting `evidence`/`gaps` before `score` forces the model to
   commit to its reasoning before the number. Same design as the cloud scorer.
   Keep score fields last when you edit SCHEMAS.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

DEFAULT_ENDPOINT = "http://localhost:8080"

# llama-swap starts a model on first request; a cold MoE off spinning rust can
# take minutes (the server's own healthCheckTimeout is 900s).
COLD_START_TIMEOUT = 900
WARM_TIMEOUT = 180


class LLMError(RuntimeError):
    pass


class LlamaSwap:
    def __init__(self, endpoint: str = DEFAULT_ENDPOINT, *, timeout: int | None = None,
                 dead_after: int = 2):
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout or COLD_START_TIMEOUT
        self._warm: set[str] = set()
        # Per-model circuit breaker on TIMEOUTS. --max-submit bounds the money,
        # but nothing bounded the wall clock: a hung llama-swap costs one full
        # COLD_START_TIMEOUT per posting (~45 min), so 400 postings could run
        # 37+ hours into the next night's cron. After `dead_after` timeouts a
        # model is declared dead and every later call fails fast — the run ends
        # in minutes with a report full of screen-failures (fail-open =>
        # stage=error => never a paid call), instead of grinding for a day.
        self.dead_after = dead_after
        self._timeouts: dict[str, int] = {}
        self._dead: set[str] = set()

    def models(self) -> list[str]:
        req = urllib.request.Request(f"{self.endpoint}/v1/models")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return [m["id"] for m in json.load(resp).get("data", [])]

    def json_call(
        self,
        model: str,
        system: str,
        user: str,
        schema: dict,
        *,
        max_tokens: int = 1200,
        temperature: float = 0.0,
        seed: int | None = 42,
        retries: int = 2,
    ) -> dict:
        """One grammar-constrained call. Returns the parsed object.

        Raises LLMError rather than returning junk: a prescreen that silently
        degrades is worse than one that stops and says so in the morning report.
        """
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            # See module docstring — without this, content comes back empty.
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "prescreen", "strict": True, "schema": schema},
            },
        }
        if seed is not None:
            body["seed"] = seed

        if model in self._dead:
            raise LLMError(f"{model}: declared dead after {self.dead_after} "
                           f"timeouts tonight; not retrying")
        timeout = WARM_TIMEOUT if model in self._warm else self.timeout
        last_err: Exception | None = None

        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(
                    f"{self.endpoint}/v1/chat/completions",
                    data=json.dumps(body).encode(),
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    payload = json.load(resp)

                self._warm.add(model)
                timeout = WARM_TIMEOUT

                choice = payload["choices"][0]
                content = choice["message"].get("content") or ""
                finish = choice.get("finish_reason")

                if finish == "length" and not content.strip():
                    raise LLMError(
                        f"{model}: empty content with finish_reason=length. The "
                        "thinking block ate the budget — check that "
                        "chat_template_kwargs.enable_thinking is still being honoured."
                    )
                if not content.strip():
                    raise LLMError(f"{model}: empty content (finish_reason={finish})")

                return json.loads(content)

            # OSError covers ConnectionResetError and URLError (both subclass it).
            # Catching only URLError let a single reset-by-peer kill a 20-minute
            # run and lose every screened posting — do not narrow this.
            except (OSError, TimeoutError, json.JSONDecodeError, KeyError, LLMError) as err:
                last_err = err
                # A timeout (socket.timeout subclasses TimeoutError AND OSError)
                # is the expensive failure — count it toward the model breaker.
                if isinstance(err, TimeoutError):
                    self._timeouts[model] = self._timeouts.get(model, 0) + 1
                    if self._timeouts[model] >= self.dead_after:
                        self._dead.add(model)
                        break
                if attempt < retries:
                    time.sleep(2 * (attempt + 1))

        raise LLMError(f"{model}: failed after {retries + 1} attempts: {last_err}")


# --- Schemas ---------------------------------------------------------------
# Field order is load-bearing. Reasoning fields first, score last.

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "title_evidence": {
            "type": "string",
            "description": "The exact title text you are judging.",
        },
        "family": {
            "type": "string",
            "enum": [
                "platform", "devops", "build-release", "developer-experience",
                "infrastructure", "sre", "management", "frontend", "other",
            ],
        },
        "level": {
            "type": "string",
            "enum": ["below-senior", "senior", "staff-plus", "management", "unclear"],
        },
        "keep": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["title_evidence", "family", "level", "keep", "reason"],
    "additionalProperties": False,
}

GATE_SCHEMA = {
    "type": "object",
    "properties": {
        "gate_failures": {
            "type": "array",
            "description": (
                "HARD deal-breakers this posting CLEARLY fails. Quote the posting. "
                "Ambiguity is NOT a failure — omit anything you cannot quote."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "gate": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["gate", "evidence"],
                "additionalProperties": False,
            },
        },
        "strong_matches": {"type": "array", "items": {"type": "string"}},
        "required_misses": {"type": "array", "items": {"type": "string"}},
        "pace_signals": {
            "type": "array",
            "description": "Each prefixed 'RED: ' or 'GREEN: '.",
            "items": {"type": "string"},
        },
        "fit_sketch": {"type": "integer", "minimum": 0, "maximum": 100},
        "worth_paid_scoring": {"type": "boolean"},
    },
    "required": [
        "gate_failures", "strong_matches", "required_misses",
        "pace_signals", "fit_sketch", "worth_paid_scoring",
    ],
    "additionalProperties": False,
}
