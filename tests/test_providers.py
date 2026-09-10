import json
from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest

from mcp_annotation_lab.providers import Reply, build_request, call_provider, decode_reply, parse_decision
from mcp_annotation_lab.check_connections import check_connections


def spec(provider):
    return {"provider": provider, "model_id": {"openai": "gpt-5.6-sol", "anthropic": "claude-sonnet-5"}[provider],
            "api_key_env": {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}[provider],
            "generation": {"max_output_tokens": 512}}


def body(provider, text='{"decision":"CONFIRM"}'):
    if provider == "openai":
        return {"status": "completed", "model": "returned-snapshot", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}], "usage": {"input_tokens": 20, "output_tokens": 5}}
    if provider == "anthropic":
        return {"stop_reason": "end_turn", "model": "returned-snapshot", "content": [{"type": "thinking", "thinking": "ignored"}, {"type": "text", "text": text}], "usage": {"input_tokens": 20, "output_tokens": 5}}
    raise ValueError("unsupported test provider")


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_requests_preserve_inputs_without_registering_tools(provider):
    url, payload = build_request(spec(provider), {"system": "fixed system", "user": "fixed user"})
    dumped = json.dumps(payload)
    assert "fixed system" in dumped and "fixed user" in dumped
    assert "tools" not in payload and "case_id" not in dumped and "condition" not in dumped
    assert "temperature" not in dumped and "top_p" not in dumped
    assert "?" not in url and "candidateCount" not in dumped


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_reply_extracts_final_text_and_usage(provider):
    result = decode_reply(provider, body(provider))
    assert (result.status, result.decision) == ("ok", "C")
    assert result.returned_model == "returned-snapshot" and result.usage


@pytest.mark.parametrize("text", ['{"decision":"E","decision":"R"}', 'Please REFUSE', '```json\n{"decision":"E"}\n```', '{"decision":"EXECUTE","explanation":"ok"}', 'E', '{}'])
def test_does_not_guess_or_repair_decisions(text):
    assert parse_decision(text) is None


def test_truncation_and_provider_blocks_are_not_research_refusals():
    data = body("openai", '{"decision":"REFUSE"}')
    data["status"] = "incomplete"
    assert decode_reply("openai", data).decision is None
    data = body("anthropic", '{"decision":"REFUSE"}')
    data["stop_reason"] = "refusal"
    assert decode_reply("anthropic", data).status == "provider_block"
    assert decode_reply("anthropic", data).decision is None


@pytest.mark.parametrize("error", [HTTPError("https://example.invalid", 401, "fake-secret", {}, BytesIO(b"fake-secret")), URLError("fake-secret")])
def test_transport_errors_do_not_leak_credentials_or_retry(monkeypatch, error):
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            raise error
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    result = call_provider(spec("openai"), {"system": "s", "user": "u"}, "fake-secret", 1)
    assert len(calls) == 1 and result.decision is None and result.status == "api_error"
    assert "fake-secret" not in str(result)


def test_retry_after_is_parsed_without_retaining_error_body(monkeypatch):
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            raise HTTPError(request.full_url, 503, "fake-secret", {"Retry-After": "2"}, BytesIO(b"fake-secret"))
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    result = call_provider(spec("openai"), {"system": "s", "user": "u"}, "fake-secret", 1)
    assert len(calls) == 1 and result.http_status == 503 and result.retry_after_seconds == 2
    assert "fake-secret" not in str(result)


def test_connection_check_is_two_neutral_calls_and_never_loads_dataset(tmp_path):
    # There is deliberately no datasets directory or executable tool server.
    providers = [spec(p) for p in ("openai", "anthropic")]
    config = tmp_path / "providers.json"
    config.write_text(json.dumps({"providers": providers, "api_calls_enabled": False}), encoding="utf-8")
    (tmp_path / ".env").write_text("\n".join(s["api_key_env"] + "=fake-secret" for s in providers), encoding="utf-8")
    calls = []
    def fake(specification, inputs, key, timeout):
        assert set(inputs) == {"system", "user"} and "OK" in inputs["user"]
        assert "메타데이터" not in inputs["user"] and "decision" not in inputs["user"]
        calls.append(specification["provider"])
        return Reply("invalid_decision", text="OK", http_status=200, returned_model=specification["model_id"])
    result = check_connections(tmp_path, config, live=True, transport=fake)
    assert calls == ["openai", "anthropic"]
    assert result["requests_attempted"] == 2 and result["experiment_cases_used"] == 0
    assert all(r["status"] == "connected" for r in result["models"])
    assert "fake-secret" not in json.dumps(result)
    assert not (tmp_path / "runs").exists()
