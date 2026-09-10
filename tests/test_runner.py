import csv
import json
from pathlib import Path

import pytest

import mcp_annotation_lab.runner as runner_module
from mcp_annotation_lab.providers import Reply
from mcp_annotation_lab.runner import RequestLimiter, collect, digest, plan_summary, select_pilot_cases
from test_providers import spec


def setup_plan(tmp_path, *, enabled=True, request_limits=None, retry_attempts=3):
    cases = []
    for i in range(2):
        inputs = {"system": "fixture system", "user": f"fixture {i}"}
        cases.append({"case_id": f"case{i}", "condition": "consistent", "tool_name": "read_file", "domain": "files", "behavior": "read", "repetition": i + 1, "input_sha256": digest(inputs), "model_input": inputs})
    (tmp_path / "datasets").mkdir()
    cases_path = tmp_path / "datasets" / "cases.jsonl"
    cases_path.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases), encoding="utf-8")
    import hashlib
    config = {"providers": [spec(p) for p in ("openai", "anthropic")],
              "dataset_path": "datasets/cases.jsonl", "expected_dataset_sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest(),
              "expected_cases_per_model": 2, "expected_main_inference_calls": 4,
              "execution_order": "sequential", "research_endpoint": "E_C_R_judgment_only", "api_calls_enabled": enabled,
              "timeout_seconds": 1, "request_interval_seconds": 0, "pilot_cases_per_model": 1,
              "retry": {"max_attempts": retry_attempts, "backoff_seconds": 0, "max_wait_seconds": 0,
                        "retry_http_statuses": [429, 502, 503, 504], "retry_connection_errors": True},
              "analysis": {"bootstrap_seed": 20260908, "permutation_seed": 20260909,
                           "bootstrap_iterations": 1000, "permutation_iterations": 1000,
                           "multiple_comparison_correction": "holm_within_model_scope"}}
    if request_limits is not None:
        config["request_limits"] = request_limits
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / ".env").write_text("\n".join(s["api_key_env"] + "=fixture-secret" for s in config["providers"]), encoding="utf-8")
    return config_path


def test_experiment_disabled_and_no_live_flag_prevent_all_requests(tmp_path):
    path = setup_plan(tmp_path, enabled=False)
    assert plan_summary(path, tmp_path)["ready_for_explicit_run"] is False
    def never(*args):
        pytest.fail("A network request must not be attempted")
    for live in (False, True):
        with pytest.raises(ValueError):
            collect(path, tmp_path, phase="main", max_calls=4, live=live, transport=never, demo=True)
    assert not (tmp_path / "runs").exists()


def test_sequential_collection_and_resume_do_not_duplicate_results(tmp_path):
    path = setup_plan(tmp_path)
    calls = []
    def fake(s, inputs, key, timeout):
        calls.append((s["provider"], inputs["user"]))
        return Reply("ok", decision="C", text='{"decision":"CONFIRM"}', http_status=200)
    result = collect(path, tmp_path, phase="main", max_calls=4, live=True, transport=fake, demo=True)
    assert calls == [(p, f"fixture {i}") for p in ("openai", "anthropic") for i in range(2)]
    result = collect(path, tmp_path, phase="main", max_calls=4, live=True, transport=fake, demo=True, resume=Path(result["run_directory"]))
    assert len(calls) == 4 and result["status"] == "completed"
    with open(result["summary_csv"], encoding="utf-8-sig", newline="") as stream:
        overall = [r for r in csv.DictReader(stream) if r["level"] == "overall"]
    assert len(overall) == 2 and all(r["C"] == "2" and r["missing"] == "0" for r in overall)
    assert result["summary_csv"].endswith("summary.csv")
    assert result["effects_csv"].endswith("effects.csv")
    assert result["trials_csv"].endswith("trials.csv")
    run_dir = Path(result["run_directory"])
    assert (run_dir / "prompts.json").is_file()
    with (run_dir / "trials.csv").open(encoding="utf-8-sig", newline="") as stream:
        trials = list(csv.DictReader(stream))
    assert len(trials) == 4 and all(row["status"] == "ok" for row in trials)


def test_single_model_run_does_not_call_other_providers(tmp_path):
    path = setup_plan(tmp_path)
    calls = []
    def fake(s, inputs, key, timeout):
        calls.append(s["provider"])
        return Reply("ok", decision="E")
    result = collect(path, tmp_path, phase="main", max_calls=2, live=True, transport=fake,
                     demo=True, target_model_id="gpt-5.6-sol")
    assert calls == ["openai", "openai"]
    with open(result["summary_csv"], encoding="utf-8-sig", newline="") as stream:
        overall = [row for row in csv.DictReader(stream) if row["level"] == "overall"]
    assert len(overall) == 1 and overall[0]["model_id"] == "gpt-5.6-sol"


def test_api_error_stops_and_preserves_unattempted_models_as_missing(tmp_path):
    path = setup_plan(tmp_path)
    calls = []
    def fake(*args):
        calls.append(1)
        return Reply("api_error", http_status=401)
    result = collect(path, tmp_path, phase="main", max_calls=4, live=True, transport=fake, demo=True)
    assert result["status"] == "stopped_on_error" and len(calls) == 1
    with open(result["summary_csv"], encoding="utf-8-sig", newline="") as stream:
        rows = [r for r in csv.DictReader(stream) if r["level"] == "overall"]
    assert len(rows) == 2 and sum(int(r["invalid"]) for r in rows) == 1
    assert sum(int(r["missing"]) for r in rows) == 3 and all(r["R"] == "0" for r in rows)


def test_retryable_503_is_retried_and_every_attempt_is_journaled(tmp_path):
    path = setup_plan(tmp_path)
    replies = [Reply("api_error", http_status=503, error_type="http_error"), Reply("ok", decision="E")]
    calls = []
    def fake(*args):
        calls.append(1)
        return replies.pop(0) if replies else Reply("ok", decision="E")
    result = collect(path, tmp_path, phase="main", max_calls=4, live=True, transport=fake, demo=True)
    assert len(calls) == 5 and result["status"] == "completed"
    run_dir = Path(result["run_directory"])
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sum(event["kind"] == "attempt_started" for event in events) == 5
    assert sum(event["kind"] == "attempt_finished" for event in events) == 5
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["automatic_retries"] == 1 and manifest["api_attempts"] == 5
    with (run_dir / "trials.csv").open(encoding="utf-8-sig", newline="") as stream:
        trials = list(csv.DictReader(stream))
    assert trials[0]["attempt_count"] == "2" and trials[0]["retry_count"] == "1"


@pytest.mark.parametrize("reply", [
    Reply("api_error", http_status=401, error_type="http_error"),
    Reply("interrupted_unknown", error_type="transport_timeout"),
    Reply("invalid_decision"),
])
def test_non_retryable_results_are_not_retried(tmp_path, reply):
    path = setup_plan(tmp_path)
    calls = []
    def fake(*args):
        calls.append(1)
        return reply
    result = collect(path, tmp_path, phase="main", max_calls=4, live=True, transport=fake, demo=True)
    assert result["status"] == "stopped_on_error" and len(calls) == 1
    manifest = json.loads((Path(result["run_directory"]) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["automatic_retries"] == 0 and manifest["api_attempts"] == 1


def test_interrupted_request_is_not_resent(tmp_path):
    path = setup_plan(tmp_path)
    def interrupted(*args):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        collect(path, tmp_path, phase="main", max_calls=4, live=True, transport=interrupted, demo=True)
    calls = []
    def fake(*args):
        calls.append(1)
        return Reply("ok", decision="E")
    directory = next((tmp_path / "runs").iterdir())
    result = collect(path, tmp_path, phase="main", max_calls=4, live=True, transport=fake, demo=True, resume=directory)
    assert len(calls) == 3 and result["status"] == "finished_with_invalid"
    assert result["response_status_counts"]["interrupted_unknown"] == 1


def test_pilot_selection_covers_every_condition_behavior_cell():
    cases = []
    domains = ("files", "notes", "events", "records")
    for ci, condition in enumerate(("omitted", "consistent", "inconsistent")):
        for bi, behavior in enumerate(("read", "add", "destructive")):
            index = ci * 3 + bi
            cases.append({"case_id": f"case{index}", "condition": condition, "behavior": behavior,
                          "domain": domains[index % 4], "repetition": 1})
    selected = select_pilot_cases(list(reversed(cases)), 9)
    assert len(selected) == 9
    assert {(c["condition"], c["behavior"]) for c in selected} == {
        (condition, behavior)
        for condition in ("omitted", "consistent", "inconsistent")
        for behavior in ("read", "add", "destructive")
    }


def test_request_limiter_spaces_every_attempt_including_retries(tmp_path):
    current = [0.0]
    sleeps = []
    def monotonic():
        return current[0]
    def sleep(seconds):
        sleeps.append(seconds)
        current[0] += seconds
    limiter = RequestLimiter(
        tmp_path, "openai", 1,
        {"requests_per_minute": 4, "requests_per_day": 20,
         "daily_window_seconds": 86400, "quota_bucket": "fixture"},
        sleep_fn=sleep, monotonic_fn=monotonic,
    )
    assert limiter.wait() == 0
    assert limiter.wait() == 15
    assert limiter.wait() == 15
    assert sleeps == [15, 15]


def test_collection_applies_rpm_spacing_to_initial_calls_and_retry(tmp_path, monkeypatch):
    limits = {"openai": {"requests_per_minute": 4, "requests_per_day": 20,
                          "daily_window_seconds": 86400, "quota_bucket": "fixture"}}
    path = setup_plan(tmp_path, request_limits=limits)
    current = [0.0]
    monkeypatch.setattr(runner_module.time, "monotonic", lambda: current[0])
    monkeypatch.setattr(runner_module.time, "sleep", lambda seconds: current.__setitem__(0, current[0] + seconds))
    replies = [Reply("api_error", http_status=503, error_type="http_error"),
               Reply("ok", decision="E"), Reply("ok", decision="E")]
    starts = []
    def fake(*args):
        starts.append(current[0])
        return replies.pop(0)
    result = collect(path, tmp_path, phase="main", max_calls=2, live=True, transport=fake,
                     demo=True, target_model_id="gpt-5.6-sol")
    assert result["status"] == "completed"
    assert starts == [0.0, 15.0, 30.0]


def test_daily_budget_pauses_before_starting_an_unfinishable_trial(tmp_path):
    limits = {"openai": {"requests_per_minute": 600, "requests_per_day": 3,
                          "daily_window_seconds": 86400, "quota_bucket": "fixture"}}
    path = setup_plan(tmp_path, request_limits=limits, retry_attempts=3)
    calls = []
    def fake(*args):
        calls.append(1)
        return Reply("ok", decision="E")
    result = collect(path, tmp_path, phase="main", max_calls=2, live=True, transport=fake,
                     demo=True, target_model_id="gpt-5.6-sol")
    assert result["status"] == "paused_rate_limit" and len(calls) == 1
    assert result["rate_limit_pause"]["attempts_used"] == 1
    resumed = collect(path, tmp_path, phase="main", max_calls=2, live=True, transport=fake,
                      demo=True, target_model_id="gpt-5.6-sol",
                      resume=Path(result["run_directory"]))
    assert resumed["status"] == "paused_rate_limit" and len(calls) == 1
    with open(Path(result["run_directory"]) / "trials.csv", encoding="utf-8-sig", newline="") as stream:
        trials = list(csv.DictReader(stream))
    assert [row["status"] for row in trials] == ["ok", "missing"]
