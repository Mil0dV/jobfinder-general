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

Generation-tier preference: when MARVIN_KEY is set and the local llama rig
answers at import (GET MARVIN_BASE_URL/models), cover letters and resume
tailoring request llm.generation_model(...) instead, which resolves to
the model llama-swap has loaded on the rig (http://marvin:8080/v1): MARVIN_PREFERRED_MODEL
(default qwen3.8-27b) if it is loaded, else MARVIN_MODEL (default gandalf); setting
MARVIN_MODEL explicitly in .env pins it and skips the check. No key or LLM_PROVIDER=anthropic: the normal tiers
are used unchanged. If the rig is down when a generation call asks for it, the user gets
a short press-G-to-retry countdown before the call falls back (re-probed per call, not
once at import). A marvin call that hits a network error mid-flight falls
back to the configured provider once.

Exception classes (``llm.AuthError``, ``llm.NetworkError``, ``llm.APIError``)
alias the Anthropic SDK classes in anthropic mode, so except-clauses work
identically under both providers.
"""

import json
import os
import random
import re
import sys
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

# Marvin: the candidate's V100 llama rig (ik_llama.cpp). Preferred backend for
# generation-tier calls (cover letters, resume tailoring) when reachable.
MARVIN_BASE_URL = os.getenv("MARVIN_BASE_URL", "http://marvin:8080/v1").rstrip("/")
# llama-swap serves several models and loads one on demand (a swap stalls the call), so
# prefer whichever is already loaded: MARVIN_PREFERRED_MODEL if up, else MARVIN_MODEL.
_marvin_pinned = bool(os.getenv("MARVIN_MODEL"))
MARVIN_MODEL = os.getenv("MARVIN_MODEL", "gandalf")
MARVIN_PREFERRED_MODEL = os.getenv("MARVIN_PREFERRED_MODEL", "qwen3.8-27b")
MARVIN_THINKING = (os.getenv("MARVIN_THINKING") or "low").strip().lower()
MARVIN_TELLCHECK_THINKING = (os.getenv("MARVIN_TELLCHECK_THINKING") or "off").strip().lower()
MARVIN_KEY = os.getenv("MARVIN_KEY", "")


def marvin_loaded_models(timeout: float = 3) -> set[str] | None:
    """Ids llama-swap reports as loaded (aliases included), or None if the rig is
    unreachable / not llama-swap-shaped. GET /v1/models carries status.value."""
    if not MARVIN_KEY or PROVIDER == "anthropic":
        return None
    try:
        r = requests.get(
            f"{MARVIN_BASE_URL}/models",
            headers={"Authorization": f"Bearer {MARVIN_KEY}"},
            timeout=timeout,
        )
        if r.status_code != 200:
            return None
        return {
            m["id"]
            for m in r.json().get("data", [])
            if (m.get("status") or {}).get("value") == "loaded"
        }
    except (requests.RequestException, ValueError):
        return None


_DEFAULT_MARVIN_MODEL = MARVIN_MODEL
MARVIN_READY = False
_marvin_down_until = 0.0  # after a declined retry, don't nag again until then
_MARVIN_RETRY_SECS = 5
_MARVIN_QUIET_SECS = 60


def _probe_marvin(timeout: float = 3) -> bool:
    """(Re)check the rig and update MARVIN_READY / MARVIN_MODEL from what is loaded."""
    global MARVIN_READY, MARVIN_MODEL
    loaded = marvin_loaded_models(timeout)
    MARVIN_READY = loaded is not None
    if MARVIN_READY and not _marvin_pinned:
        MARVIN_MODEL = (
            MARVIN_PREFERRED_MODEL if MARVIN_PREFERRED_MODEL in loaded else _DEFAULT_MARVIN_MODEL
        )
    return MARVIN_READY


_probe_marvin()


def describe_model(model: str | None, allow_marvin: bool = True) -> str:
    """Human-readable "model (backend)" for what a request for `model` really runs on."""
    if PROVIDER == "anthropic":
        return f"{model} (anthropic)"
    b = _OpenRouterMessages("")._backend_for(model, allow_marvin=allow_marvin)
    return f"{b['model']} ({b['name']})"


def _countdown_for_retry(message: str) -> bool:
    """Show `message` with a live countdown; True if the user pressed G to retry.
    Non-interactive sessions never wait."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        for left in range(_MARVIN_RETRY_SECS, 0, -1):
            print(f"\r\033[K   {message} (press G to retry, {left}s)", end="", flush=True)
            deadline = time.time() + 1
            while (wait := deadline - time.time()) > 0:
                if select.select([fd], [], [], wait)[0] and os.read(fd, 1).lower() == b"g":
                    print("\r\033[K   retrying...", flush=True)
                    return True
        print("\r\033[K", end="", flush=True)
        return False
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def ensure_marvin(fallback: str | None = None, force: bool = False) -> bool:
    """True if the rig is usable now. If it is down, offer a short press-G-to-retry
    window before the caller falls back, then stay quiet for a minute so one
    letter's several passes don't each ask. `force` skips that quiet period."""
    global _marvin_down_until
    if not MARVIN_KEY or PROVIDER == "anthropic":
        return False
    if MARVIN_READY and not force:
        return True
    if not force and time.time() < _marvin_down_until:
        return False
    target = describe_model(fallback, allow_marvin=False) if fallback else "the fallback model"
    while not _probe_marvin(timeout=5):
        if not _countdown_for_retry(f"{MARVIN_MODEL} (marvin) unreachable; using {target}"):
            _marvin_down_until = time.time() + _MARVIN_QUIET_SECS
            print(f"   {MARVIN_MODEL} (marvin) unreachable; using {target}", flush=True)
            return False
    return True


def _marvin_failed() -> None:
    """A call to the rig died mid-flight: stop trusting it until it is re-probed."""
    global MARVIN_READY, _marvin_down_until
    MARVIN_READY = False
    _marvin_down_until = 0.0


def generation_model(fallback: str) -> str:
    """Model generation-tier call sites should request: marvin when it is up."""
    return MARVIN_MODEL if ensure_marvin(fallback) else fallback


def generation_thinking() -> str | None:
    """Thinking setting for generation-tier calls; None keeps provider defaults.
    On the rig: "off" maps to enable_thinking=false, anything else maps to the
    chat template's reasoning_effort (low|medium|high). Measured on flash-next:
    low and medium consume nearly the same thinking; high costs 3x for no gain.
    Tune with MARVIN_THINKING (default low)."""
    return MARVIN_THINKING if ensure_marvin() else None


def tellcheck_thinking() -> str | None:
    """Thinking for mechanical tell-check / revision passes. Unlike drafting,
    flattening prose gains nothing from reasoning, so the rig defaults to off.
    Tune with MARVIN_TELLCHECK_THINKING (default off)."""
    return MARVIN_TELLCHECK_THINKING if ensure_marvin() else None


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

    def _headers(self, backend: dict) -> dict:
        headers = {
            "Authorization": f"Bearer {backend['key']}",
            "Content-Type": "application/json",
        }
        if backend["name"] == "openrouter":
            headers["X-Title"] = "jobfinder"
        return headers

    def _backend_for(self, model, allow_marvin: bool = True) -> dict:
        if allow_marvin and MARVIN_READY and (model or "") == MARVIN_MODEL:
            return {
                "url": f"{MARVIN_BASE_URL}/chat/completions",
                "key": MARVIN_KEY,
                "model": MARVIN_MODEL,
                "name": "marvin",
                "requested": model,
            }
        # A generation call that asked for MARVIN_MODEL must fall back to the main
        # OpenRouter model; _map_model would send it to the fast tier, since the
        # rig model name carries no tier hint.
        if (model or "") == MARVIN_MODEL:
            fallback_model = OPENROUTER_MODEL
        else:
            fallback_model = _map_model(model)
        return {
            "url": OPENROUTER_URL,
            "key": self._key,
            "model": fallback_model,
            "name": "openrouter",
            "requested": model,
        }

    def _payload(
        self,
        *,
        backend,
        max_tokens,
        messages,
        system=None,
        tools=None,
        output_config=None,
        thinking=None,
    ):
        # Reasoning models (Kimi K3, Flash-Next) spend hidden thinking tokens
        # out of max_tokens; the small per-call budgets tuned for Claude can
        # leave zero room for the actual answer. The rig's Flash-Next needs a
        # lot of headroom: its reasoning easily eats 8k+ tokens on resume-sized
        # prompts, then the answer never starts.
        headroom = 32768 if backend["name"] == "marvin" else 8192
        payload = {
            "model": backend["model"],
            "max_tokens": max(max_tokens, headroom),
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
        if backend["name"] == "marvin" and thinking:
            # ik_llama chat-template knobs (same ones pi's v100 provider config uses).
            if thinking == "off":
                payload["chat_template_kwargs"] = {"enable_thinking": False}
            else:
                payload["chat_template_kwargs"] = {"reasoning_effort": thinking}
        return payload

    def _post(self, payload: dict, backend: dict, stream: bool = False) -> requests.Response:
        last_error = ""
        for attempt in range(_MAX_RETRIES + 1):
            try:
                r = requests.post(
                    backend["url"],
                    headers=self._headers(backend),
                    json=payload,
                    timeout=600,
                    stream=stream,
                )
            except (requests.ConnectionError, requests.Timeout) as e:
                raise NetworkError(f"Could not reach {backend['name']}: {e}") from e
            if r.status_code == 401:
                raise AuthError(f"Invalid {backend['name']} API key")
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
                    return self._post(retry, backend, stream=stream)
                raise APIError(f"{backend['name']} error {r.status_code}: {r.text[:500]}")
            return r
        raise APIError(f"{backend['name']} error after {_MAX_RETRIES + 1} attempts: {last_error}")

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
        backend = self._backend_for(model)

        def _attempt(backend: dict) -> dict:
            payload = self._payload(
                backend=backend,
                max_tokens=max_tokens,
                messages=messages,
                system=system,
                tools=tools,
                output_config=output_config,
                thinking=thinking,
            )
            return self._post(payload, backend).json()

        try:
            data = _attempt(backend)
        except NetworkError:
            if backend["name"] != "marvin":
                raise
            _marvin_failed()
            if ensure_marvin(model, force=True):
                backend = self._backend_for(model)
            else:
                backend = self._backend_for(model, allow_marvin=False)
            data = _attempt(backend)
        if "choices" not in data:
            raise APIError(f"{backend['name']} error: {json.dumps(data)[:500]}")
        msg = data["choices"][0]["message"]
        finish = data["choices"][0].get("finish_reason")
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
        if not blocks:
            # e.g. a reasoning model that burned max_tokens on hidden thinking.
            raise APIError(
                f"{backend['name']} returned no visible output (finish_reason={finish})."
            )
        stop_reason = (
            "tool_use"
            if msg.get("tool_calls")
            else ("max_tokens" if finish == "length" else "end_turn")
        )
        return SimpleNamespace(
            content=blocks, stop_reason=stop_reason, usage=data.get("usage") or {}
        )

    def stream(
        self,
        *,
        model=None,
        max_tokens=1024,
        messages=None,
        system=None,
        thinking=None,
        output_config=None,
        **_ignored,
    ):
        backend = self._backend_for(model)
        payload = self._payload(
            backend=backend,
            max_tokens=max_tokens,
            messages=messages,
            system=system,
            thinking=thinking,
            output_config=output_config,
        )
        payload["stream"] = True
        return _OpenRouterStream(self, payload, backend)


class _OpenRouterStream:
    """Context manager mimicking anthropic's messages.stream()."""

    def __init__(self, messages_api: _OpenRouterMessages, payload: dict, backend: dict):
        self._api = messages_api
        self._payload = payload
        self._backend = backend
        self._response = None
        # Optional callback for reasoning deltas (rig/openrouter thinking text).
        self.on_reasoning = None

    def __enter__(self):
        try:
            self._response = self._api._post(self._payload, self._backend, stream=True)
        except NetworkError:
            if self._backend["name"] != "marvin":
                raise
            requested = self._backend["requested"]
            _marvin_failed()
            retry_marvin = ensure_marvin(requested, force=True)
            self._backend = self._api._backend_for(requested, allow_marvin=retry_marvin)
            self._payload["model"] = self._backend["model"]
            self._response = self._api._post(self._payload, self._backend, stream=True)
        # SSE has no charset header, so requests would decode as ISO-8859-1
        self._response.encoding = "utf-8"
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
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning and self.on_reasoning:
                self.on_reasoning(reasoning)
            chunk = delta.get("content")
            if chunk:
                yield chunk


class _OpenRouterClient:
    def __init__(self, key: str):
        self.messages = _OpenRouterMessages(key)
