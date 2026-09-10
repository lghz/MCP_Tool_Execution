from copy import deepcopy
import hashlib
import json

import pytest

from mcp_annotation_lab.catalog import TOOLS
from mcp_annotation_lab.judgment import VIGNETTES, annotation_profile, build_cases, export_cases, summarize_decisions


def fixture_catalogue():
    return {"tools": [{"name": tool.name, "inputSchema": {"type": "object"}} for tool in TOOLS]}


def test_balanced_reproducible_dataset_with_analysis_labels_kept_outside_input():
    cases = build_cases(fixture_catalogue())
    assert cases == build_cases(fixture_catalogue())
    assert len(cases) == len({case["case_id"] for case in cases}) == 180
    assert len({case["input_sha256"] for case in cases}) == 36
    for condition in ("omitted", "consistent", "inconsistent"):
        assert sum(case["condition"] == condition for case in cases) == 60
    for case in cases:
        assert set(case["model_input"]) == {"system", "user"}
        assert case["case_id"] not in case["model_input"]["user"]
        assert case["condition"] not in case["model_input"]["user"]


def test_condition_changes_only_annotation_text():
    cases = build_cases(fixture_catalogue(), repetitions=1)
    for tool in TOOLS:
        selected = [case for case in cases if case["tool_name"] == tool.name]
        prefixes = {case["model_input"]["user"].split("\n\n도구 메타데이터:\n")[0] for case in selected}
        assert len(prefixes) == 1
        assert len({case["model_input"]["system"] for case in selected}) == 1


def test_one_fixed_request_preserves_every_input_value():
    for _, request, arguments in VIGNETTES.values():
        for value in arguments.values():
            assert str(value) in request


def test_annotation_profiles_use_two_fields_and_safe_conflicts_for_writes():
    assert annotation_profile("read", "consistent") == {"readOnlyHint": True, "destructiveHint": False}
    assert annotation_profile("read", "inconsistent") == {"readOnlyHint": False, "destructiveHint": True}
    for behavior in ("add", "destructive"):
        assert annotation_profile(behavior, "inconsistent") == {"readOnlyHint": True, "destructiveHint": False}


def test_invalid_and_missing_are_not_refusals_and_models_are_separate():
    cases = build_cases(fixture_catalogue(), repetitions=1)
    case = cases[0]
    responses = [
        {"case_id": case["case_id"], "model_id": "synthetic-test-a", "decision": "CONFIRM"},
        {"case_id": case["case_id"], "model_id": "synthetic-test-b", "decision": "not a valid label"},
    ]
    rows = summarize_decisions(cases, responses)
    a = [row for row in rows if row["model_id"] == "synthetic-test-a"]
    b = [row for row in rows if row["model_id"] == "synthetic-test-b"]
    assert sum(row["C"] for row in a) == 1
    assert sum(row["invalid"] for row in b) == 1
    assert sum(row["R"] for row in b) == 0
    assert sum(row["missing"] for row in b) == len(cases) - 1


def test_rejects_duplicate_and_unknown_records():
    cases = build_cases(fixture_catalogue(), repetitions=1)
    response = {"case_id": cases[0]["case_id"], "model_id": "synthetic-test", "decision": "EXECUTE"}
    with pytest.raises(ValueError, match="Duplicate"):
        summarize_decisions(cases, [response, deepcopy(response)])
    with pytest.raises(ValueError, match="unknown"):
        summarize_decisions(cases, [{**response, "case_id": "not-in-dataset"}])


def test_export_hash_matches_actual_file_and_never_overwrites(tmp_path):
    catalogue = {**fixture_catalogue(), "protocol_version": "synthetic-test"}
    target = tmp_path / "dataset"
    manifest = export_cases(catalogue, target)
    payload = (target / "cases.jsonl").read_bytes()
    assert hashlib.sha256(payload).hexdigest() == manifest["dataset_sha256"]
    assert len([json.loads(line) for line in payload.decode("utf-8").splitlines()]) == 180
    with pytest.raises(FileExistsError):
        export_cases(catalogue, target)
