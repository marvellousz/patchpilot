"""Provider-agnostic LLM client. Every model call in PatchPilot goes through here."""

import re
import time

from openai import (
    APIConnectionError,
    APIStatusError,
    BadRequestError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)

from patchpilot.config import Endpoint, llm_chain

MAX_OUTPUT_TOKENS = 2048
MAX_WAIT_SECONDS = 300
# Provider-specific fields on assistant messages: Gemini's thought signatures live in
# tool_calls[].extra_content, Groq's reasoning models add `reasoning`.
PROVIDER_ONLY_FIELDS = {"gemini": {"extra_content"}, "groq": {"reasoning"}}


class ContextTooLarge(Exception):
    """The request exceeds what the endpoint accepts; the caller should send less context."""


def _for_provider(messages: list[dict], provider: str) -> list[dict]:
    foreign = set().union(*(f for p, f in PROVIDER_ONLY_FIELDS.items() if p != provider))
    cleaned = []
    for m in messages:
        m = {k: v for k, v in m.items() if k not in foreign}
        if m.get("tool_calls"):
            m["tool_calls"] = [{k: v for k, v in tc.items() if k not in foreign} for tc in m["tool_calls"]]
        cleaned.append(m)
    return cleaned


def _gemini_compatible(messages: list[dict]) -> bool:
    # Gemini 3 requires its own thought signature on every earlier tool call, so it can only
    # continue a conversation in which it made all the tool calls.
    return all("extra_content" in tc for m in messages for tc in m.get("tool_calls") or [])


def _retry_after(error: RateLimitError) -> float:
    header = error.response.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    match = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", f"{error} {error.response.text}")
    return int(match.group(1) or 0) * 60 + float(match.group(2)) if match else 10.0


def _is_daily_quota(error: RateLimitError) -> bool:
    # The quota details are in the response body; the exception message can omit them.
    text = f"{error} {error.response.text}"
    return "PerDay" in text or "per day" in text.lower()


class LLM:
    def __init__(self, endpoints: list[Endpoint] | None = None):
        self.endpoints = endpoints or llm_chain()
        self.clients = {ep.label: OpenAI(api_key=ep.api_key, base_url=ep.base_url, max_retries=0)
                        for ep in self.endpoints}
        self.exhausted: set[str] = set()  # endpoints whose daily quota ran out this session
        self.last_model: str | None = None
        self.calls = 0

    def chat(self, messages: list[dict], tools: list[dict] | None = None):
        """Return the assistant message from the first endpoint that isn't rate-limited."""
        deadline = time.monotonic() + MAX_WAIT_SECONDS
        last_error: Exception | None = None
        while True:
            waits = []
            for ep in self.endpoints:
                if ep.label in self.exhausted:
                    continue
                if ep.provider == "gemini" and not _gemini_compatible(messages):
                    continue
                try:
                    return self._call(ep, messages, tools)
                except RateLimitError as e:
                    last_error = e
                    if _is_daily_quota(e):
                        self.exhausted.add(ep.label)
                        print(f"[llm] {ep.label} daily quota used up", flush=True)
                    else:
                        waits.append(_retry_after(e))
                except (APIConnectionError, InternalServerError) as e:
                    last_error = e
                    waits.append(5.0)
                except BadRequestError as e:
                    # Groq rejects its own malformed tool-call JSON; a fresh sample usually fixes it.
                    if "tool_use_failed" in e.response.text:
                        last_error = e
                        waits.append(0.0)
                        continue
                    raise RuntimeError(f"{ep.label} rejected the request: {e.response.text[:1500]}") from e
                except APIStatusError as e:
                    if e.status_code == 413:
                        raise ContextTooLarge(str(e)) from e
                    raise

            if not waits:
                raise RuntimeError(f"No LLM endpoint available: {last_error}")
            wait = min(max(min(waits), 1.0), 60.0)
            if time.monotonic() + wait > deadline:
                raise RuntimeError(f"LLM endpoints still rate-limited after {MAX_WAIT_SECONDS}s: {last_error}")
            print(f"[llm] all endpoints busy, waiting {wait:.0f}s", flush=True)
            time.sleep(wait)

    def _call(self, ep: Endpoint, messages: list[dict], tools: list[dict] | None):
        kwargs = {
            "model": ep.model,
            "messages": _for_provider(messages, ep.provider),
            "max_tokens": MAX_OUTPUT_TOKENS,
        }
        if tools:
            kwargs["tools"] = tools
        response = self.clients[ep.label].chat.completions.create(**kwargs)
        self.last_model = ep.label
        self.calls += 1
        return response.choices[0].message
