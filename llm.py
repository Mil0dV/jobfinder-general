"""LLM provider layer: switch between Anthropic (Claude) and OpenRouter (Kimi K3).

Every module gets its client via ``llm.get_client()``, which returns an object
with the same surface the code already uses from the Anthropic SDK:

    client.messages.create(model=..., max_tokens=..., system=..., messages=...,
                           tools=..., output_config=..., thinking=...)
    with client.messages.stream(...) as stream:
        for text in stream.text_stream: ...

Provider selection (``.env``):
    LLM_PROVIDER=openrouter   -> OpenRouter (OpenAI-compatible API)
    LLM_PROVIDER=anthropic    -> real Anthropic SDK, exactly as before
If LLM_PROVIDER is unset, OpenRouter is used when OPENROUTER_API_KEY is set,
otherwise Anthropic. In OpenRouter mode the claude-* names at call sites only
pick the tier: opus-tier requests (cover letters, application answers) run on
OPENROUTER_MODEL (default moonshotai/kimi-k3); everything else runs on
OPENROUTER_FAST_MODEL (default deepseek/deepseek-v4-flash). 429s and 5xx
responses are retried with Retry-After-aware exponential backoff.

Exception classes (``llm.AuthError``, ``llm.NetworkError``, ``llm.APIError``)
alias the Anthropic SDK classes in anthropic mode, so except-clauses work
identically under both providers.
"""

import json
import os
import random
import re
import time
from types import SimpleNamespace

import requests
from dotenv import load_dotenv

load_dotenv()

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Main model: cover letters / application answers (call sites request opus-tier).
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "moonshotai/kimi-k3")
# Fast model: everything else (validation scoring, agent loop, resume parsing/
# tailoring, salary estimates — call sites request haiku/sonnet-tier).
OPENROUTER_FAST_MODEL = os.getenv("OPENROUTER_FAST_MODEL", "deepseek/deepseek-v4-flash")


def _map_model(model: str | None) -> str:
    """Route opus-tier requests to the main model, the rest to the fast model."""
    if model and "opus" in model:
        return OPENROUTER_MODEL
    return OPENROUTER_FAST_MODEL


_env_provider = os.getenv("LLM_PROVIDER", "").strip().lower()
if _env_provider in ("anthropic", "openrouter"):
    PROVIDER = _env_provider
else:
    PROVIDER = "openrouter" if os.getenv("OPENROUTER_API_KEY") else "anthropic"


if PROVIDER == "anthropic":
    import anthropic

    APIError = anthropic.APIError
    AuthError = anthropic.AuthenticationError
    NetworkError = anthropic.APIConnectionError
else:

    class APIError(Exception):
        pass

    class AuthError(APIError):
        pass

    class NetworkError(APIError):
        pass


def api_key_env() -> str:
    return "ANTHROPIC_API_KEY" if PROVIDER == "anthropic" else "OPENROUTER_API_KEY"


def api_key() -> str | None:
    return os.getenv(api_key_env())


def get_client():
    """Return an Anthropic client or the OpenRouter drop-in equivalent."""
    if PROVIDER == "anthropic":
        import anthropic

        return anthropic.Anthropic(api_key=api_key())
    return _OpenRouterClient(api_key())


# ---------------------------------------------------------------------------
# OpenRouter backend (OpenAI-compatible wire format)
# ---------------------------------------------------------------------------


def _flatten_system(system) -> str:
    if isinstance(system, list):  # anthropic block list, possibly with cache_control
        return "".join(b.get("text", "") for b in system)
    return system or ""


def _to_openai_messages(system, messages) -> list:
    out = []
    sys_text = _flatten_system(system)
    if sys_text:
        out.append({"role": "system", "content": sys_text})
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        if m["role"] == "assistant":
            # Echoed response.content from a previous _OpenRouterClient call.
            text = "".join(b.text for b in content if getattr(b, "type", "") == "text")
            tool_calls = [
                {
                    "id": b.id,
                    "type": "function",
                    "function": {"name": b.name, "arguments": json.dumps(b.input)},
                }
                for b in content
                if getattr(b, "type", "") == "tool_use"
            ]
            # Moonshot rejects assistant messages with empty content; use a
            # placeholder if a previous response somehow produced no text.
            msg = {"role": "assistant", "content": text or ("" if tool_calls else "(no output)")}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)
        else:
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": b["tool_use_id"],
                            "content": str(b.get("content", "")),
                        }
                    )
                elif isinstance(b, dict) and b.get("type") == "text":
                    out.append({"role": "user", "content": b["text"]})
    return out


_MAX_RETRIES = 4


def _retry_delay(r: requests.Response, attempt: int) -> float:
    """Delay before a retry: honor Retry-After when given, else exponential backoff."""
    retry_after = r.headers.get("Retry-After")
    if retry_after is None:
        try:  # OpenRouter tucks the upstream hint into error metadata
            retry_after = r.json()["error"]["metadata"].get("retry_after_seconds")
        except Exception:
            retry_after = None
    try:
        if retry_after is not None:
            return min(float(retry_after), 30.0) + random.uniform(0, 1)
    except (TypeError, ValueError):
        pass
    return min(2.0**attempt, 30.0) + random.uniform(0, 1)


def _strip_json_fences(text: str) -> str:
    m = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    return m.group(1).strip() if m else text.strip()


class _OpenRouterMessages:
    def __init__(self, key: str):
        self._key = key

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
            "X-Title": "jobfinder",
        }

    def _payload(self, *, model, max_tokens, messages, system=None, tools=None, output_config=None):
        # Reasoning models (like Kimi K3) spend hidden thinking tokens out of
        # max_tokens; the small per-call budgets tuned for Claude can leave zero
        # room for the actual answer. Give every call generous headroom.
        payload = {
            "model": _map_model(model),
            "max_tokens": max(max_tokens, 8192),
            "messages": _to_openai_messages(system, messages),
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {"type": "object"}),
                    },
                }
                for t in tools
            ]
        fmt = (output_config or {}).get("format")
        if fmt and fmt.get("type") == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "strict": True, "schema": fmt["schema"]},
            }
        return payload

    def _post(self, payload: dict, stream: bool = False) -> requests.Response:
        last_error = ""
        for attempt in range(_MAX_RETRIES + 1):
            try:
                r = requests.post(
                    OPENROUTER_URL,
                    headers=self._headers(),
                    json=payload,
                    timeout=300,
                    stream=stream,
                )
            except (requests.ConnectionError, requests.Timeout) as e:
                raise NetworkError(f"Could not reach OpenRouter: {e}") from e
            if r.status_code == 401:
                raise AuthError("Invalid OPENROUTER_API_KEY")
            if r.status_code == 429 or r.status_code >= 500:
                last_error = r.text[:500]
                if attempt < _MAX_RETRIES:
                    delay = _retry_delay(r, attempt)
                    print(
                        f"   ({payload['model']} busy [{r.status_code}], "
                        f"retrying in {delay:.0f}s...)",
                        flush=True,
                    )
                    time.sleep(delay)
                    continue
                break
            if r.status_code >= 400:
                # If structured output isn't supported for this model, retry without it.
                if "response_format" in payload:
                    retry = {k: v for k, v in payload.items() if k != "response_format"}
                    return self._post(retry, stream=stream)
                raise APIError(f"OpenRouter error {r.status_code}: {r.text[:500]}")
            return r
        raise APIError(f"OpenRouter error after {_MAX_RETRIES + 1} attempts: {last_error}")

    def create(
        self,
        *,
        model=None,
        max_tokens=1024,
        messages=None,
        system=None,
        tools=None,
        output_config=None,
        thinking=None,
        **_ignored,
    ):
        payload = self._payload(
            model=model,
            max_tokens=max_tokens,
            messages=messages,
            system=system,
            tools=tools,
            output_config=output_config,
        )
        data = self._post(payload).json()
        if "choices" not in data:
            raise APIError(f"OpenRouter error: {json.dumps(data)[:500]}")
        msg = data["choices"][0]["message"]
        blocks = []
        text = msg.get("content") or ""
        if output_config:
            text = _strip_json_fences(text)
        if text:
            blocks.append(SimpleNamespace(type="text", text=text))
        for tc in msg.get("tool_calls") or []:
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            blocks.append(
                SimpleNamespace(
                    type="tool_use", id=tc["id"], name=tc["function"]["name"], input=args
                )
            )
        stop_reason = "tool_use" if msg.get("tool_calls") else "end_turn"
        return SimpleNamespace(content=blocks, stop_reason=stop_reason)

    def stream(
        self, *, model=None, max_tokens=1024, messages=None, system=None, thinking=None, **_ignored
    ):
        payload = self._payload(
            model=model, max_tokens=max_tokens, messages=messages, system=system
        )
        payload["stream"] = True
        return _OpenRouterStream(self, payload)


class _OpenRouterStream:
    """Context manager mimicking anthropic's messages.stream()."""

    def __init__(self, messages_api: _OpenRouterMessages, payload: dict):
        self._api = messages_api
        self._payload = payload
        self._response = None

    def __enter__(self):
        self._response = self._api._post(self._payload, stream=True)
        return self

    def __exit__(self, *exc):
        if self._response is not None:
            self._response.close()
        return False

    @property
    def text_stream(self):
        for line in self._response.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                break
            try:
                delta = json.loads(data)["choices"][0]["delta"]
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
            chunk = delta.get("content")
            if chunk:
                yield chunk


class _OpenRouterClient:
    def __init__(self, key: str):
        self.messages = _OpenRouterMessages(key)
