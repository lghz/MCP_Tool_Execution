"""One neutral connectivity request per provider; no experiment cases are read."""

from __future__ import annotations

import argparse
import copy
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .providers import call_provider, strict_json, validate_provider
from .runner import ROOT, now, read_keys, scrub


def check_connections(root: Path, config_path: Path, *, live: bool, transport=None, provider: str | None = None) -> dict:
    if not live:
        raise ValueError("Connection checks require --live")
    config = strict_json(config_path.read_text(encoding="utf-8-sig"))
    specs = config["providers"]
    if len(specs) != 2 or [s["provider"] for s in specs] != ["openai", "anthropic"]:
        raise ValueError("Expected exactly the two selected providers")
    for spec in specs:
        validate_provider(spec)
    if provider is not None:
        if provider not in ("openai", "anthropic"):
            raise ValueError("Unknown provider for connection check")
        specs = [spec for spec in specs if spec["provider"] == provider]
    keys = read_keys(root)
    if any(not keys[s["api_key_env"]] for s in specs):
        raise ValueError("A local API key is missing")
    synthetic = transport is not None
    transport = transport or call_provider
    records = []
    prompt = {"system": "This is an API connectivity check. Reply with the single word OK.", "user": "Reply OK."}
    for configured in specs:
        spec = copy.deepcopy(configured)
        spec["generation"] = {"max_output_tokens": 512}
        tick = time.monotonic()
        reply = transport(spec, prompt, keys[spec["api_key_env"]], 45)
        # This probe intentionally does not ask for an E/C/R decision.
        generated = reply.http_status == 200 and reply.status in ("ok", "invalid_decision") and bool(reply.text.strip())
        row = scrub({"provider": spec["provider"], "requested_model": spec["model_id"],
                     "returned_model": reply.returned_model, "response_id": reply.response_id,
                     "status": "connected" if generated else reply.status,
                     "expected_reply_received": reply.text.strip() == "OK", "http_status": reply.http_status,
                     "finish_reason": reply.finish_reason, "error_type": reply.error_type,
                     "usage": reply.usage, "response_text": reply.text,
                     "latency_seconds": round(time.monotonic() - tick, 3), "checked_at_utc": now()}, keys)
        records.append(row)
        # Persist after each request, so interruption does not erase completed checks.
        if len(records) == 1:
            destination = root / "reports" / ("connection_check_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ") + ".json")
            destination.parent.mkdir(parents=True, exist_ok=True)
        report = {"kind": "synthetic_connection_test" if synthetic else "api_connection_check",
                  "requests_attempted": len(records), "experiment_cases_used": 0, "tool_calls": 0,
                  "main_experiment_started": False, "models": records}
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"report_path": str(destination), **report}


def main():
    parser = argparse.ArgumentParser(description="Two connectivity requests only; no experiment data")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "providers.json")
    parser.add_argument("--provider", choices=("openai", "anthropic"), help="Check only this provider once")
    args = parser.parse_args()
    report = check_connections(ROOT, args.config, live=args.live, provider=args.provider)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
