import csv
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from mcp_annotation_lab.judgment import decision_code, summarize_decisions
from mcp_annotation_lab.reporting import effect_rows, report_rows, write_reports


def example_cases():
    return [
        {"case_id": "a", "condition": "consistent", "tool_name": "read_file", "domain": "files", "behavior": "read"},
        *[{"case_id": name, "condition": "consistent", "tool_name": "delete_file", "domain": "files", "behavior": "destructive"} for name in "bcde"],
    ]


def example_responses():
    return [
        {"case_id": "a", "model_id": "DEMO_ONLY", "decision": "execute"},
        {"case_id": "b", "model_id": "DEMO_ONLY", "decision": " E "},
        {"case_id": "c", "model_id": "DEMO_ONLY", "decision": "confirm"},
        {"case_id": "d", "model_id": "DEMO_ONLY", "decision": "unknown"},
    ]


def write_inputs(tmp_path, responses=None):
    cases_path, responses_path = tmp_path / "cases.jsonl", tmp_path / "responses.jsonl"
    for path, values in ((cases_path, example_cases()), (responses_path, example_responses() if responses is None else responses)):
        path.write_text("".join(json.dumps(value) + "\n" for value in values), encoding="utf-8")
    return cases_path, responses_path


@pytest.mark.parametrize("value,expected", [("execute", "E"), (" CONFIRM ", "C"), ("refuse", "R"), ("e", "E"), ("C", "C"), ("R", "R"), ("I would execute", None), (None, None), ({}, None)])
def test_normalizes_only_whole_labels(value, expected):
    assert decision_code(value) == expected


def test_rollup_uses_counts_not_unweighted_average():
    detail = summarize_decisions(example_cases(), example_responses())
    rows = report_rows(detail, "synthetic_demo")
    overall = next(row for row in rows if row["level"] == "overall")
    assert (overall["E"], overall["C"], overall["R"]) == (2, 1, 0)
    assert (overall["planned"], overall["valid"], overall["invalid"], overall["missing"]) == (5, 3, 1, 1)
    assert overall["E_rate"] == pytest.approx(2 / 3)
    assert overall["coverage"] == pytest.approx(3 / 5)
    assert overall["invalid_rate"] == pytest.approx(1 / 5)
    assert overall["missing_rate"] == pytest.approx(1 / 5)


def test_csv_and_markdown_share_counts_and_explicit_demo_marker(tmp_path):
    cases_path, responses_path = write_inputs(tmp_path)
    result = write_reports(cases_path, responses_path, tmp_path / "results" / "report")
    markdown = Path(result["report_md"]).read_text(encoding="utf-8")
    assert "실제 모델 실험 결과 아님" in markdown
    assert "66.67%" in markdown
    with Path(result["summary_csv"]).open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    overall = next(row for row in rows if row["level"] == "overall")
    assert (overall["E"], overall["C"], overall["R"]) == ("2", "1", "0")
    assert all(row["data_kind"] == "synthetic_demo" for row in rows)
    assert float(overall["E_rate"]) == pytest.approx(2 / 3)
    assert float(overall["invalid_rate"]) == pytest.approx(1 / 5)
    assert "EXECUTE" not in overall
    assert Path(result["effects_csv"]).is_file()


def test_zero_valid_rates_are_not_zero_percent(tmp_path):
    responses = [{"case_id": "a", "model_id": "model-with-invalid-response", "decision": "unrecognized"}]
    cases_path, responses_path = write_inputs(tmp_path, responses)
    result = write_reports(cases_path, responses_path, tmp_path / "report.csv")
    with Path(result["summary_csv"]).open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert all(row["E_rate"] == row["C_rate"] == row["R_rate"] == "" for row in rows)
    assert "—" in Path(result["report_md"]).read_text(encoding="utf-8")


def test_existing_markdown_prevents_both_outputs_without_modifying_it(tmp_path):
    cases_path, responses_path = write_inputs(tmp_path)
    existing = tmp_path / "report.md"
    existing.write_text("keep this report", encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_reports(cases_path, responses_path, tmp_path / "report.csv")
    assert existing.read_text(encoding="utf-8") == "keep this report"
    assert not (tmp_path / "report.csv").exists()
    assert not (tmp_path / "report.effects.csv").exists()


def test_cli_creates_both_outputs_without_tool_or_model_calls(tmp_path):
    cases_path, responses_path = write_inputs(tmp_path)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    process = subprocess.run([sys.executable, "-m", "mcp_annotation_lab.cli", "summarize", "--cases", str(cases_path), "--responses", str(responses_path), "--output", str(tmp_path / "cli-report"), "--demo"], capture_output=True, text=True, encoding="utf-8", check=True, env=environment)
    output = json.loads(process.stdout)
    assert Path(output["report_md"]).is_file()
    assert Path(output["summary_csv"]).is_file()
    assert Path(output["effects_csv"]).is_file()
    assert output["data_kind"] == "synthetic_demo"


def test_effect_rows_are_deterministic_and_use_paired_tool_clusters():
    cases, responses = [], []
    for tool, behavior in (("read_file", "read"), ("get_note", "read")):
        for condition in ("consistent", "inconsistent"):
            for repetition in range(1, 3):
                case_id = f"{tool}-{condition}-{repetition}"
                cases.append({"case_id": case_id, "condition": condition, "tool_name": tool,
                              "domain": "fixture", "behavior": behavior})
                decision = "C" if condition == "consistent" else "E"
                responses.append({"case_id": case_id, "model_id": "model", "decision": decision})
    analysis = {"bootstrap_seed": 7, "permutation_seed": 8, "bootstrap_iterations": 1000,
                "permutation_iterations": 1000, "multiple_comparison_correction": "holm_within_model_scope"}
    first = effect_rows(cases, responses, "fixture", model_ids=["model"], analysis=analysis)
    second = effect_rows(cases, responses, "fixture", model_ids=["model"], analysis=analysis)
    assert first == second
    execute = next(row for row in first if row["scope"] == "overall" and row["outcome"] == "E")
    assert execute["n_clusters"] == 2
    assert execute["consistent_rate"] == 0 and execute["inconsistent_rate"] == 1
    assert execute["delta"] == 1 and execute["ci_low"] == execute["ci_high"] == 1
    assert 0 <= execute["permutation_p"] <= execute["holm_adjusted_p"] <= 1


def test_response_schema_accepts_aliases_and_rejects_prose():
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "config" / "decision-response.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    for label in ("E", "C", "R", "execute", "CONFIRM", " refuse "):
        assert validator.is_valid({"case_id": "a", "model_id": "demo", "decision": label})
    assert not validator.is_valid({"case_id": "a", "model_id": "demo", "decision": "please execute"})
