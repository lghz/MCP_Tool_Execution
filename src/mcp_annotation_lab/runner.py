"""Sequential, journaled E/C/R collection from immutable text vignettes."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import platform
import time
import uuid
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .judgment import load_jsonl
from .providers import KEY_NAMES, Reply, build_request, call_provider, strict_json, validate_provider
from .reporting import write_reports

ROOT = Path(__file__).resolve().parents[2]

TRIAL_FIELDS = (
    "run_id", "trial_id", "data_kind", "phase", "model_id", "provider",
    "returned_model_id", "case_id", "tool_name", "domain", "behavior",
    "annotation_condition", "repetition", "decision", "status", "latency_ms",
    "input_tokens", "output_tokens", "reasoning_tokens", "total_tokens",
    "attempt_count", "retry_count", "response_id", "input_sha256",
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def serialized(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(serialized(value).encode("utf-8")).hexdigest()


class RequestLimiter:
    """Pace every API attempt and persist a conservative provider-day budget."""

    def __init__(self, root: Path, provider: str, request_interval_seconds: float,
                 limits: dict | None, *, sleep_fn=None, monotonic_fn=None, now_fn=None):
        self.provider = provider
        self._sleep = sleep_fn or time.sleep
        self._monotonic = monotonic_fn or time.monotonic
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._last_attempt_at = None
        self._limits = limits
        rpm_interval = 0 if limits is None else 60 / limits["requests_per_minute"]
        self.minimum_interval_seconds = max(float(request_interval_seconds), rpm_interval)
        self._ledger_path = root / "work" / "request-limit-ledger.json"

    def wait(self) -> float:
        if self._last_attempt_at is None:
            self._last_attempt_at = self._monotonic()
            return 0.0
        delay = max(0.0, self._last_attempt_at + self.minimum_interval_seconds - self._monotonic())
        if delay:
            self._sleep(delay)
        self._last_attempt_at = self._monotonic()
        return delay

    def _ledger(self) -> dict:
        if not self._ledger_path.is_file():
            return {"version": 1, "buckets": {}}
        value = strict_json(self._ledger_path.read_text(encoding="utf-8-sig"))
        if (not isinstance(value, dict) or set(value) != {"version", "buckets"}
                or value["version"] != 1 or not isinstance(value["buckets"], dict)):
            raise ValueError("Invalid request-limit ledger")
        return value

    def daily_state(self) -> dict | None:
        if self._limits is None:
            return None
        current = self._now().astimezone(timezone.utc)
        ledger = self._ledger()
        bucket = ledger["buckets"].get(self._limits["quota_bucket"], {})
        started = None
        if bucket:
            try:
                started = datetime.fromisoformat(bucket["window_started_at_utc"]).astimezone(timezone.utc)
            except (KeyError, TypeError, ValueError):
                raise ValueError("Invalid request-limit ledger window") from None
        window = timedelta(seconds=self._limits["daily_window_seconds"])
        if started is None or current >= started + window:
            started, used = current, 0
        else:
            used = bucket.get("attempts", 0)
        if type(used) is not int or used < 0:
            raise ValueError("Invalid request-limit ledger count")
        cap = self._limits["requests_per_day"]
        return {"provider": self.provider, "quota_bucket": self._limits["quota_bucket"],
                "window_started_at_utc": started.isoformat(),
                "attempts_used": used, "attempts_remaining": max(0, cap - used),
                "requests_per_day": cap, "requests_per_minute": self._limits["requests_per_minute"],
                "minimum_interval_seconds": self.minimum_interval_seconds,
                "reset_at_utc": (started + window).isoformat(),
                "window_policy": "conservative_rolling_24h"}

    def can_start_trial(self, maximum_attempts: int) -> bool:
        state = self.daily_state()
        return state is None or state["attempts_remaining"] >= maximum_attempts

    def reserve_attempt(self) -> dict | None:
        state = self.daily_state()
        if state is None:
            return None
        if state["attempts_remaining"] < 1:
            raise ValueError("Daily request budget exhausted before an API attempt")
        ledger = self._ledger()
        ledger["buckets"][state["quota_bucket"]] = {
            "provider": self.provider, "window_started_at_utc": state["window_started_at_utc"],
            "daily_window_seconds": self._limits["daily_window_seconds"],
            "requests_per_day": state["requests_per_day"], "attempts": state["attempts_used"] + 1,
            "updated_at_utc": self._now().isoformat(),
        }
        self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(self._ledger_path, ledger)
        return self.daily_state()


def read_keys(root: Path) -> dict[str, str]:
    """Load only the two named credentials; never return them to the CLI."""
    values = {}
    path = root / ".env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, separator, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key not in KEY_NAMES.values():
                continue
            if not separator or key in values:
                raise ValueError("Invalid or duplicate key entry in local .env")
            values[key] = value
    for name in KEY_NAMES.values():
        if name not in values:
            values[name] = os.environ.get(name, "")
        if values[name] and any(c.isspace() for c in values[name]):
            raise ValueError("A credential contains whitespace; re-enter it locally")
    return values


def scrub(value, keys: dict):
    if isinstance(value, str):
        for secret in keys.values():
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {k: scrub(v, keys) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, keys) for v in value]
    return value


def load_plan(config_path: Path, root: Path, *, ready: bool = False):
    config = strict_json(config_path.read_text(encoding="utf-8-sig"))
    expected = {"providers", "dataset_path", "expected_dataset_sha256", "expected_cases_per_model", "expected_main_inference_calls", "execution_order", "research_endpoint", "api_calls_enabled", "timeout_seconds", "request_interval_seconds", "pilot_cases_per_model", "retry", "analysis"}
    if not isinstance(config, dict) or set(config) not in (expected, expected | {"request_limits"}):
        raise ValueError("Unexpected experiment configuration fields")
    config.setdefault("request_limits", {})
    if config["execution_order"] != "sequential" or config["research_endpoint"] != "E_C_R_judgment_only":
        raise ValueError("Only sequential text-judgment experiments are supported")
    specs = config["providers"]
    if not isinstance(specs, list) or len(specs) != 2:
        raise ValueError("Expected two provider configurations")
    readiness = [validate_provider(spec, ready=ready) for spec in specs]
    if [s["provider"] for s in specs] != ["openai", "anthropic"]:
        raise ValueError("Provider order must be OpenAI, Anthropic")
    models = [s["model_id"] for s in specs if s["model_id"] is not None]
    if len(set(models)) != len(models):
        raise ValueError("Duplicate model IDs")
    if type(config["api_calls_enabled"]) is not bool:
        raise ValueError("api_calls_enabled must be boolean")
    retry = config["retry"]
    if not isinstance(retry, dict) or set(retry) != {"max_attempts", "backoff_seconds", "max_wait_seconds", "retry_http_statuses", "retry_connection_errors"}:
        raise ValueError("Unexpected retry configuration")
    if type(retry["max_attempts"]) is not int or not 1 <= retry["max_attempts"] <= 3:
        raise ValueError("Retry max_attempts must be 1 to 3")
    for field in ("backoff_seconds", "max_wait_seconds"):
        value = retry[field]
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 60:
            raise ValueError("Invalid retry timing")
    if retry["retry_http_statuses"] != [429, 502, 503, 504] or type(retry["retry_connection_errors"]) is not bool:
        raise ValueError("Retry scope must remain the pre-registered transport-only set")
    request_limits = config["request_limits"]
    if not isinstance(request_limits, dict) or set(request_limits) - set(KEY_NAMES):
        raise ValueError("Request limits must be keyed by provider")
    for provider, limits in request_limits.items():
        fields = {"requests_per_minute", "requests_per_day", "daily_window_seconds", "quota_bucket"}
        if not isinstance(limits, dict) or set(limits) != fields:
            raise ValueError("Unexpected request-limit configuration")
        rpm, rpd = limits["requests_per_minute"], limits["requests_per_day"]
        if type(rpm) not in (int, float) or not math.isfinite(rpm) or not 0 < rpm <= 600:
            raise ValueError("Invalid requests-per-minute limit")
        if type(rpd) is not int or not 1 <= rpd <= 1000000 or rpd < retry["max_attempts"]:
            raise ValueError("Daily request limit must accommodate one complete trial")
        if limits["daily_window_seconds"] != 86400:
            raise ValueError("Daily request budget must use a conservative rolling 24-hour window")
        bucket = limits["quota_bucket"]
        if (not isinstance(bucket, str) or not 1 <= len(bucket) <= 80
                or any(not (character.isascii() and (character.isalnum() or character in "-_.")) for character in bucket)):
            raise ValueError("Invalid quota bucket")
    analysis = config["analysis"]
    if not isinstance(analysis, dict) or set(analysis) != {"bootstrap_seed", "permutation_seed", "bootstrap_iterations", "permutation_iterations", "multiple_comparison_correction"}:
        raise ValueError("Unexpected analysis configuration")
    for field in ("bootstrap_seed", "permutation_seed"):
        if type(analysis[field]) is not int or analysis[field] < 0:
            raise ValueError("Analysis seeds must be nonnegative integers")
    for field in ("bootstrap_iterations", "permutation_iterations"):
        if type(analysis[field]) is not int or not 1000 <= analysis[field] <= 100000:
            raise ValueError("Analysis iterations must be between 1000 and 100000")
    if analysis["multiple_comparison_correction"] != "holm_within_model_scope":
        raise ValueError("Unexpected multiple comparison correction")
    for field, lower, upper in (("timeout_seconds", 1, 300), ("request_interval_seconds", 0, 60)):
        value = config[field]
        if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
            raise ValueError("Invalid request timing configuration")
    relative = Path(config["dataset_path"])
    cases_path = (root / relative).resolve()
    if relative.is_absolute() or not cases_path.is_relative_to((root / "datasets").resolve()):
        raise ValueError("Dataset must be inside the project datasets directory")
    actual_hash = hashlib.sha256(cases_path.read_bytes()).hexdigest()
    if actual_hash != config["expected_dataset_sha256"]:
        raise ValueError("Dataset SHA-256 mismatch")
    cases = load_jsonl(cases_path)
    for field in ("expected_cases_per_model", "expected_main_inference_calls", "pilot_cases_per_model"):
        if type(config[field]) is not int or config[field] < 1:
            raise ValueError("Case and call counts must be positive integers")
    if len(cases) != config["expected_cases_per_model"] or len(cases) * 2 != config["expected_main_inference_calls"]:
        raise ValueError("Planned case/call count mismatch")
    if not 1 <= config["pilot_cases_per_model"] <= min(12, len(cases)):
        raise ValueError("Pilot must contain 1 to 12 cases per model")
    if len({c["case_id"] for c in cases}) != len(cases):
        raise ValueError("Duplicate case IDs")
    for case in cases:
        model_input = case.get("model_input")
        if not isinstance(model_input, dict) or set(model_input) != {"system", "user"} or not all(isinstance(v, str) and v for v in model_input.values()):
            raise ValueError("Invalid model input")
        if digest(model_input) != case.get("input_sha256"):
            raise ValueError("Model input SHA-256 mismatch")
    return config, cases, all(readiness)


def plan_summary(config_path: Path, root: Path) -> dict:
    config, cases, ready = load_plan(config_path, root)
    keys = read_keys(root)
    present = {name: bool(keys[name]) for name in KEY_NAMES.values()}
    return {"models": [s["model_id"] for s in config["providers"]], "credentials_present": present,
            "configuration_ready": ready, "ready_for_explicit_run": ready and all(present.values()) and config["api_calls_enabled"],
            "cases_per_model": len(cases), "main_calls": len(cases) * 2,
            "pilot_calls": config["pilot_cases_per_model"] * 2, "api_calls_made": 0, "tool_calls": 0,
            "dataset_sha256": config["expected_dataset_sha256"], "request_limits": config["request_limits"]}


def select_pilot_cases(cases: list[dict], count: int) -> list[dict]:
    """Select deterministic condition-by-behavior cells before filling extras."""
    domains = ("files", "notes", "events", "records")
    conditions = ("omitted", "consistent", "inconsistent")
    behaviors = ("read", "add", "destructive")
    selected = []
    selected_ids = set()
    for index, (condition, behavior) in enumerate((c, b) for c in conditions for b in behaviors):
        if len(selected) >= count:
            break
        target_domain = domains[index % len(domains)]
        matches = [case for case in cases if case["condition"] == condition and case["behavior"] == behavior
                   and case["domain"] == target_domain and case["repetition"] == 1]
        if len(matches) != 1:
            raise ValueError("Dataset cannot supply the stratified pilot design")
        selected.append(matches[0])
        selected_ids.add(matches[0]["case_id"])
    if len(selected) < count:
        for case in cases:
            if case["case_id"] not in selected_ids:
                selected.append(case)
                selected_ids.add(case["case_id"])
                if len(selected) == count:
                    break
    if len(selected) != count:
        raise ValueError("Dataset does not contain enough pilot cases")
    return selected


def append_event(path: Path, event: dict) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(serialized(event) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def atomic_json(path: Path, value) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(serialized(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def write_prompts(path: Path, cases: list[dict], specs: list[dict], phase: str) -> str:
    """Write every distinct model input and the exact provider interface without secrets."""
    prompts = {}
    for case in cases:
        prompts.setdefault(case["input_sha256"], case["model_input"])
        if prompts[case["input_sha256"]] != case["model_input"]:
            raise ValueError("One prompt hash maps to different inputs")
    interfaces = []
    exemplar = cases[0]["model_input"]
    for spec in specs:
        url, payload = build_request(spec, exemplar)
        interface = {
            "provider": spec["provider"], "model_id": spec["model_id"], "endpoint": url,
            "generation": spec["generation"], "request_shape_fields": sorted(payload),
        }
        if spec["provider"] == "anthropic":
            interface["api_version_header"] = "anthropic-version: 2023-06-01"
        elif spec["provider"] == "openai":
            interface["api_version"] = "v1 Responses"
        interfaces.append(interface)
    document = {
        "version": "1.0", "phase": phase,
        "prompt_count": len(prompts), "planned_cases": len(cases),
        "request_expression": "fixed_direct",
        "serialization": "UTF-8 JSON; keys sorted for hashes; provider payload recorded per response",
        "valid_output": {"json_object_only": True, "only_field": "decision",
                         "labels": ["EXECUTE", "CONFIRM", "REFUSE"]},
        "provider_interfaces": interfaces,
        "prompts": [{"input_sha256": key, **prompts[key]} for key in sorted(prompts)],
    }
    atomic_json(path, document)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _usage_fields(row: dict) -> tuple[int | None, int | None, int | None, int | None]:
    usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
    input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
    details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
    reasoning = details.get("reasoning_tokens")
    total = usage.get("total_tokens")
    if total is None and type(input_tokens) is int and type(output_tokens) is int:
        total = input_tokens + output_tokens
    values = (input_tokens, output_tokens, reasoning, total)
    return tuple(value if type(value) is int and value >= 0 else None for value in values)


def _csv_value(value):
    if value is None:
        return ""
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def write_trials(path: Path, cases: list[dict], specs: list[dict], responses: list[dict],
                 *, run_id: str, phase: str, fingerprint: str, data_kind: str) -> str:
    """Write one normalized row for every planned model-by-case trial, including missing trials."""
    observed = {}
    for row in responses:
        key = (row.get("model_id"), row.get("case_id"))
        if key in observed:
            raise ValueError("Duplicate response while writing trial table")
        observed[key] = row
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=TRIAL_FIELDS)
        writer.writeheader()
        for spec in specs:
            for case in cases:
                key = (spec["model_id"], case["case_id"])
                row = observed.get(key, {})
                input_tokens, output_tokens, reasoning_tokens, total_tokens = _usage_fields(row)
                trial_id = row.get("trial_id") or digest({"fingerprint": fingerprint, "model_id": key[0], "case_id": key[1]})[:24]
                latency = row.get("latency_seconds")
                record = {
                    "run_id": run_id, "trial_id": trial_id, "data_kind": data_kind, "phase": phase,
                    "model_id": key[0], "provider": spec["provider"],
                    "returned_model_id": row.get("returned_model"), "case_id": key[1],
                    "tool_name": case["tool_name"], "domain": case["domain"], "behavior": case["behavior"],
                    "annotation_condition": case["condition"], "repetition": case["repetition"],
                    "decision": row.get("decision"), "status": row.get("status", "missing"),
                    "latency_ms": round(latency * 1000, 3) if type(latency) in (int, float) else None,
                    "input_tokens": input_tokens, "output_tokens": output_tokens,
                    "reasoning_tokens": reasoning_tokens, "total_tokens": total_tokens,
                    "attempt_count": row.get("attempt_count", 0), "retry_count": row.get("retry_count", 0),
                    "response_id": row.get("response_id"), "input_sha256": case["input_sha256"],
                }
                writer.writerow({field: _csv_value(record[field]) for field in TRIAL_FIELDS})
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_fixed_reports(run_dir: Path, response_path: Path, *, demo: bool, model_ids: list[str],
                        analysis: dict) -> dict:
    """Regenerate derived reports atomically at stable filenames inside the immutable run directory."""
    temporary_base = run_dir / (".derived_" + uuid.uuid4().hex)
    result = write_reports(run_dir / "cases.jsonl", response_path, temporary_base, demo=demo,
                           model_ids=model_ids, analysis=analysis)
    destinations = {
        "report_md": run_dir / "report.md", "summary_csv": run_dir / "summary.csv",
        "effects_csv": run_dir / "effects.csv",
    }
    for field, destination in destinations.items():
        Path(result[field]).replace(destination)
        result[field] = str(destination)
    return result


def collect(config_path: Path, root: Path, *, phase: str, max_calls: int, live: bool,
            resume: Path | None = None, transport=None, demo: bool = False,
            target_model_id: str | None = None) -> dict:
    if not live:
        raise ValueError("Live requests require explicit --live")
    if phase not in ("pilot", "main") or type(max_calls) is not int or max_calls < 1:
        raise ValueError("Invalid phase or call limit")
    config, all_cases, _ = load_plan(config_path, root, ready=True)
    if not config["api_calls_enabled"]:
        raise ValueError("API calls are disabled in this configuration")
    selected_specs = [spec for spec in config["providers"]
                      if target_model_id is None or spec["model_id"] == target_model_id]
    if target_model_id is not None and len(selected_specs) != 1:
        raise ValueError("Requested model ID is not in the experiment configuration")
    keys = read_keys(root)
    if any(not keys[s["api_key_env"]] for s in selected_specs):
        raise ValueError("A provider API key is missing")
    if transport is not None and not demo:
        raise ValueError("Injected transports require a synthetic-demo marker")
    if demo and transport is None:
        raise ValueError("Synthetic demo cannot use a real API transport")
    transport = transport or call_provider
    cases = select_pilot_cases(all_cases, config["pilot_cases_per_model"]) if phase == "pilot" else all_cases
    planned = len(cases) * len(selected_specs)
    if max_calls < planned:
        raise ValueError("Call limit is smaller than the selected experiment; choose pilot for a smaller run")
    model_ids = [s["model_id"] for s in selected_specs]
    source_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(__file__).parent.glob("*.py"))}
    identity = {"config": config, "phase": phase, "case_ids": [c["case_id"] for c in cases],
                "max_calls": max_calls, "source_hashes": source_hashes, "synthetic_demo": demo,
                "selected_model_ids": model_ids}
    fingerprint = digest(identity)
    limiters = {
        spec["model_id"]: RequestLimiter(root, spec["provider"], config["request_interval_seconds"],
                                         config["request_limits"].get(spec["provider"]))
        for spec in selected_specs
    }
    runs_root = (root / "runs").resolve()
    if resume:
        run_dir = resume.resolve()
        if not run_dir.is_relative_to(runs_root):
            raise ValueError("Resume directory must be inside project runs")
        manifest = strict_json((run_dir / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("fingerprint") != fingerprint:
            raise ValueError("Resume requires the exact dataset, models, settings, phase and source code")
        prompts_path = run_dir / "prompts.json"
        if (not prompts_path.is_file() or
                hashlib.sha256(prompts_path.read_bytes()).hexdigest() != manifest.get("prompts_sha256")):
            raise ValueError("Prompt archive is missing or changed")
    else:
        run_dir = runs_root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ") + "_" + phase + "_" + uuid.uuid4().hex[:8])
        run_dir.mkdir(parents=True, exist_ok=False)
        manifest = {"kind": "synthetic_demo" if demo else "api_judgment_" + phase,
                    "fingerprint": fingerprint, **identity, "started_at_utc": now(),
                    "python": platform.python_version(), "platform": platform.platform(), "planned_calls": planned,
                    "tool_calls": 0, "automatic_retries": 0, "status": "created"}
        with (run_dir / "cases.jsonl").open("x", encoding="utf-8", newline="\n") as stream:
            stream.writelines(serialized(c) + "\n" for c in cases)
        manifest["cases_sha256"] = hashlib.sha256((run_dir / "cases.jsonl").read_bytes()).hexdigest()
        manifest["prompts_sha256"] = write_prompts(run_dir / "prompts.json", cases, selected_specs, phase)
        manifest["provider_interfaces"] = strict_json((run_dir / "prompts.json").read_text(encoding="utf-8"))["provider_interfaces"]
        atomic_json(run_dir / "manifest.json", manifest)
    lock = run_dir / ".active.lock"
    # The lock is acquired before reading the journal; concurrent resumes cannot race.
    with lock.open("x", encoding="ascii") as stream:
        stream.write(str(os.getpid()))
    try:
        journal = run_dir / "events.jsonl"
        events = load_jsonl(journal) if journal.is_file() else []
        starts, finished = {}, {}
        allowed = {(m, c["case_id"]) for m in model_ids for c in cases}
        for event in events:
            key = (event.get("model_id"), event.get("case_id"))
            if key not in allowed or event.get("kind") not in ("started", "attempt_started", "attempt_finished", "finished"):
                raise ValueError("Journal contains an unexpected event")
            if event["kind"] in ("attempt_started", "attempt_finished"):
                if key not in starts or type(event.get("attempt")) is not int or event["attempt"] < 1:
                    raise ValueError("Invalid attempt journal event")
                continue
            bucket = starts if event["kind"] == "started" else finished
            if key in bucket or (event["kind"] == "finished" and key not in starts):
                raise ValueError("Invalid or duplicate journal event")
            bucket[key] = event
        for key in starts.keys() - finished.keys():
            # A request may have reached the provider. Never resend it implicitly.
            row = {"model_id": key[0], "case_id": key[1], "decision": None,
                   "status": "interrupted_unknown", "data_kind": manifest["kind"]}
            event = {"kind": "finished", **row, "record": row, "at_utc": now()}
            append_event(journal, event)
            finished[key] = event
        manifest.pop("rate_limit_pause", None)
        manifest["status"] = "running"
        atomic_json(run_dir / "manifest.json", manifest)
        stop = False
        try:
            for spec in selected_specs:
                limiter = limiters[spec["model_id"]]
                for case in cases:
                    key = (spec["model_id"], case["case_id"])
                    if key in finished:
                        continue
                    if len(starts) >= max_calls:
                        raise ValueError("Call limit reached")
                    if not limiter.can_start_trial(config["retry"]["max_attempts"]):
                        manifest["status"] = "paused_rate_limit"
                        manifest["rate_limit_pause"] = limiter.daily_state()
                        stop = True
                        break
                    trial_id = digest({"fingerprint": fingerprint, "model_id": key[0], "case_id": key[1]})[:24]
                    started = {"kind": "started", "trial_id": trial_id, "model_id": key[0], "case_id": key[1], "at_utc": now()}
                    append_event(journal, started)
                    starts[key] = started
                    tick = time.monotonic()
                    request_url, request_payload = build_request(spec, case["model_input"])
                    result = None
                    attempt_count = 0
                    for attempt in range(1, config["retry"]["max_attempts"] + 1):
                        attempt_count = attempt
                        limiter.wait()
                        limiter.reserve_attempt()
                        append_event(journal, {"kind": "attempt_started", "trial_id": trial_id, "model_id": key[0],
                                               "case_id": key[1], "attempt": attempt, "at_utc": now()})
                        attempt_tick = time.monotonic()
                        result = transport(spec, case["model_input"], keys[spec["api_key_env"]], config["timeout_seconds"])
                        if not isinstance(result, Reply):
                            raise ValueError("Invalid transport result")
                        append_event(journal, {"kind": "attempt_finished", "trial_id": trial_id, "model_id": key[0],
                                               "case_id": key[1], "attempt": attempt, "status": result.status,
                                               "http_status": result.http_status, "error_type": result.error_type,
                                               "retry_after_seconds": result.retry_after_seconds,
                                               "latency_seconds": round(time.monotonic() - attempt_tick, 6), "at_utc": now()})
                        retryable_http = result.status == "api_error" and result.http_status in config["retry"]["retry_http_statuses"]
                        retryable_connection = (result.status == "api_error" and result.error_type == "connection_error"
                                                and config["retry"]["retry_connection_errors"])
                        if not (retryable_http or retryable_connection) or attempt == config["retry"]["max_attempts"]:
                            break
                        delay = result.retry_after_seconds
                        if delay is None:
                            delay = config["retry"]["backoff_seconds"] * (2 ** (attempt - 1))
                        time.sleep(min(delay, config["retry"]["max_wait_seconds"]))
                    manifest["automatic_retries"] += attempt_count - 1
                    row = scrub({"trial_id": trial_id, "model_id": key[0], "case_id": key[1], "provider": spec["provider"],
                                 "input_sha256": case["input_sha256"], "data_kind": "synthetic_demo" if demo else manifest["kind"],
                                 "request_url": request_url, "request_payload": request_payload,
                                 "attempt_count": attempt_count, "retry_count": attempt_count - 1,
                                 "latency_seconds": round(time.monotonic() - tick, 6),
                                 "recorded_at_utc": now(), **asdict(result)}, keys)
                    if row["status"] != "ok":
                        row["decision"] = None
                    event = {"kind": "finished", "model_id": key[0], "case_id": key[1], "record": row, "at_utc": now()}
                    append_event(journal, event)
                    finished[key] = event
                    if row["status"] != "ok":
                        manifest["status"] = "stopped_on_error"
                        stop = True
                        break
                if stop:
                    break
            if not stop:
                manifest["status"] = "finished_with_invalid" if any(e["record"]["status"] != "ok" for e in finished.values()) else "completed"
        except BaseException:
            manifest["status"] = "interrupted"
            # Any started trial may already have reached the provider. Close it as
            # unknown so a resume cannot silently replay it.
            latest_events = load_jsonl(journal) if journal.is_file() else []
            attempt_counts = Counter((event.get("model_id"), event.get("case_id"))
                                     for event in latest_events if event.get("kind") == "attempt_started")
            for key in starts.keys() - finished.keys():
                case = next(item for item in cases if item["case_id"] == key[1])
                spec = next(item for item in config["providers"] if item["model_id"] == key[0])
                count = attempt_counts[key]
                row = {"trial_id": starts[key].get("trial_id"), "model_id": key[0], "case_id": key[1],
                       "provider": spec["provider"], "input_sha256": case["input_sha256"],
                       "decision": None, "status": "interrupted_unknown", "data_kind": manifest["kind"],
                       "attempt_count": count, "retry_count": max(0, count - 1), "recorded_at_utc": now()}
                event = {"kind": "finished", "model_id": key[0], "case_id": key[1],
                         "record": row, "at_utc": now()}
                append_event(journal, event)
                finished[key] = event
            raise
        finally:
            rows = [e["record"] for e in finished.values()]
            response_path = run_dir / "responses.jsonl"
            temporary = run_dir / "responses.jsonl.tmp"
            temporary.write_text("".join(serialized(r) + "\n" for r in rows), encoding="utf-8", newline="\n")
            temporary.replace(response_path)
            manifest["reports"] = write_fixed_reports(run_dir, response_path, demo=demo, model_ids=model_ids,
                                                       analysis=config["analysis"])
            trials_path = run_dir / "trials.csv"
            trials_hash = write_trials(trials_path, cases, selected_specs, rows,
                                        run_id=run_dir.name, phase=phase, fingerprint=fingerprint,
                                        data_kind=manifest["kind"])
            manifest["reports"]["trials_csv"] = str(trials_path)
            latest_events = load_jsonl(journal) if journal.is_file() else []
            attempt_sets = {}
            for event in latest_events:
                if event.get("kind") == "attempt_started":
                    attempt_sets.setdefault((event["model_id"], event["case_id"]), set()).add(event["attempt"])
            manifest["automatic_retries"] = sum(max(0, len(attempts) - 1) for attempts in attempt_sets.values())
            manifest["request_limit_state"] = {
                model_id: state for model_id, limiter in limiters.items()
                if (state := limiter.daily_state()) is not None
            }
            manifest.update(finished_at_utc=now(), requests_started=len(starts), api_attempts=sum(len(attempts) for attempts in attempt_sets.values()), recorded_responses=len(rows),
                            response_status_counts=dict(Counter(r["status"] for r in rows)),
                            output_sha256={
                                "responses.jsonl": hashlib.sha256(response_path.read_bytes()).hexdigest(),
                                "trials.csv": trials_hash,
                                "summary.csv": hashlib.sha256((run_dir / "summary.csv").read_bytes()).hexdigest(),
                                "effects.csv": hashlib.sha256((run_dir / "effects.csv").read_bytes()).hexdigest(),
                                "report.md": hashlib.sha256((run_dir / "report.md").read_bytes()).hexdigest(),
                            })
            atomic_json(run_dir / "manifest.json", manifest)
        return {"run_directory": str(run_dir), "status": manifest["status"], "requests_started": len(starts),
                "response_status_counts": manifest["response_status_counts"],
                "rate_limit_pause": manifest.get("rate_limit_pause"), **manifest["reports"]}
    finally:
        lock.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Collect E/C/R judgments sequentially; never execute tools")
    parser.add_argument("command", choices=("plan", "run"))
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "providers.json")
    parser.add_argument("--phase", choices=("pilot", "main"), default="pilot")
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--model", help="Run only one configured model ID")
    args = parser.parse_args()
    try:
        if args.command == "plan":
            output = plan_summary(args.config, ROOT)
        else:
            if args.max_calls is None:
                parser.error("run requires an explicit --max-calls")
            output = collect(args.config, ROOT, phase=args.phase, max_calls=args.max_calls, live=args.live,
                             resume=args.resume, target_model_id=args.model)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        if output.get("status") in ("stopped_on_error", "finished_with_invalid"):
            raise SystemExit(2)
    except (ValueError, OSError) as error:
        # No traceback or arbitrary exception message can expose a request header.
        print(json.dumps({"status": "setup_error", "error_type": type(error).__name__, "message": "Check local configuration, credentials, paths, or the run manifest; no secret values are printed."}))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
