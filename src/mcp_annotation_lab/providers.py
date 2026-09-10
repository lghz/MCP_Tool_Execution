"""Text-only API adapters. No tool registration, execution, or automatic retries."""

from __future__ import annotations

import json
import math
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from .judgment import decision_code

KEY_NAMES = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def strict_json(text: str) -> Any:
    return json.loads(text, object_pairs_hook=unique_object)


def parse_decision(text: str) -> str | None:
    """Accept one JSON decision only; never guess a refusal from provider errors."""
    try:
        value = strict_json(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict) or set(value) != {"decision"}:
        return None
    return decision_code(value["decision"])


def validate_provider(spec: dict, *, ready: bool = True) -> bool:
    if not isinstance(spec, dict) or set(spec) != {"provider", "model_id", "api_key_env", "generation"}:
        raise ValueError("Provider requires provider, model_id, api_key_env and generation")
    provider = spec["provider"]
    if provider not in KEY_NAMES or spec["api_key_env"] != KEY_NAMES[provider]:
        raise ValueError("Unknown provider or key variable")
    model = spec["model_id"]
    if model is not None and (not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", model)):
        raise ValueError("Invalid model ID")
    generation = spec["generation"]
    if not isinstance(generation, dict):
        raise ValueError("generation must be an object")
    # Model-specific reasoning options are deliberately omitted until selected.
    if set(generation) - {"max_output_tokens", "temperature", "top_p"}:
        raise ValueError("Unsupported generation setting")
    cap = generation.get("max_output_tokens")
    if cap is not None and (type(cap) is not int or not 16 <= cap <= 32768):
        raise ValueError("max_output_tokens must be an integer between 16 and 32768")
    for key, upper in (("temperature", 1 if provider == "anthropic" else 2), ("top_p", 1)):
        value = generation.get(key)
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= upper):
            raise ValueError("Invalid sampling setting")
    is_ready = model is not None and cap is not None
    if ready and not is_ready:
        raise ValueError("Choose exact model IDs and output token limits before running")
    return is_ready


def build_request(spec: dict, model_input: dict) -> tuple[str, dict]:
    validate_provider(spec)
    if set(model_input) != {"system", "user"} or not all(isinstance(x, str) and x for x in model_input.values()):
        raise ValueError("Expected only nonempty system and user input strings")
    provider, model, settings = spec["provider"], spec["model_id"], spec["generation"]
    system, user, cap = model_input["system"], model_input["user"], settings["max_output_tokens"]
    sampling = {key: settings[key] for key in ("temperature", "top_p") if settings.get(key) is not None}
    if provider == "openai":
        return "https://api.openai.com/v1/responses", {
            "model": model, "instructions": system, "input": user,
            "max_output_tokens": cap, "store": False, "stream": False, **sampling,
        }
    if provider == "anthropic":
        return "https://api.anthropic.com/v1/messages", {
            "model": model, "system": system, "messages": [{"role": "user", "content": user}],
            "max_tokens": cap, "stream": False, **sampling,
        }
    raise ValueError("Unsupported provider")


@dataclass
class Reply:
    status: str
    decision: str | None = None
    text: str = ""
    returned_model: str | None = None
    response_id: str | None = None
    finish_reason: str | None = None
    usage: dict = field(default_factory=dict)
    http_status: int | None = None
    error_type: str | None = None
    retry_after_seconds: float | None = None
    raw_response: dict = field(default_factory=dict)


def decode_reply(provider: str, body: dict) -> Reply:
    reply = Reply("invalid_response", raw_response=body)
    if not isinstance(body, dict):
        reply.raw_response = {}
        return reply
    try:
        if body.get("error"):
            reply.status = "api_error"
            return reply
        if provider == "openai":
            reply.returned_model, reply.response_id = body.get("model"), body.get("id")
            reply.usage = body.get("usage") or {}
            reply.finish_reason = body.get("status")
            output = body.get("output", [])
            blocks = [b for item in output if item.get("type") == "message" for b in item.get("content", [])]
            reply.text = "".join(b["text"] for b in blocks if b.get("type") == "output_text")
            if body.get("status") != "completed":
                reply.status = "incomplete"
                return reply
            if any(b.get("type") == "refusal" for b in blocks):
                reply.status = "provider_block"
                return reply
            if any(item.get("type") not in ("message", "reasoning") for item in output):
                return reply
        elif provider == "anthropic":
            reply.returned_model, reply.response_id = body.get("model"), body.get("id")
            reply.usage = body.get("usage") or {}
            reply.finish_reason = body.get("stop_reason")
            blocks = body.get("content", [])
            reply.text = "".join(b["text"] for b in blocks if b.get("type") == "text")
            if reply.finish_reason == "refusal":
                reply.status = "provider_block"
                return reply
            if reply.finish_reason != "end_turn":
                reply.status = "incomplete"
                return reply
            if any(b.get("type") not in ("text", "thinking", "redacted_thinking") for b in blocks):
                return reply
        else:
            return reply
        reply.decision = parse_decision(reply.text)
        reply.status = "ok" if reply.decision else "invalid_decision"
    except (KeyError, TypeError, AttributeError):
        reply.status, reply.decision = "invalid_response", None
    return reply


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def call_provider(spec: dict, model_input: dict, key: str, timeout: float) -> Reply:
    url, payload = build_request(spec, model_input)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if spec["provider"] == "openai":
        headers["Authorization"] = "Bearer " + key
    elif spec["provider"] == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    else:
        raise ValueError("Unsupported provider")
    request = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
    try:
        # stdlib transport makes one attempt, with no redirects or SDK retries.
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                return Reply("invalid_response", error_type="response_too_large")
            body = strict_json(raw.decode("utf-8"))
            result = decode_reply(spec["provider"], body)
            result.http_status = response.status
            return result
    except urllib.error.HTTPError as error:
        # Provider error bodies and exception strings can contain credentials.
        status = error.code
        retry_after = parse_retry_after(error.headers.get("Retry-After") if error.headers else None)
        error.close()
        return Reply("api_error", http_status=status, error_type="http_error", retry_after_seconds=retry_after)
    except (TimeoutError, socket.timeout):
        # The request may have reached the provider, so automatic replay is unsafe.
        return Reply("interrupted_unknown", error_type="transport_timeout")
    except urllib.error.URLError as error:
        reason = error.reason
        if isinstance(reason, (socket.gaierror, ConnectionRefusedError)):
            return Reply("api_error", error_type="connection_error")
        return Reply("api_error", error_type="transport_error")
    except OSError as error:
        if isinstance(error, (socket.gaierror, ConnectionRefusedError)):
            return Reply("api_error", error_type="connection_error")
        return Reply("api_error", error_type="transport_error")
    except (ValueError, UnicodeError):
        return Reply("invalid_response", error_type="response_decode_error")
