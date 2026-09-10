"""Generate paired Markdown/CSV reports from local judgment records."""

from __future__ import annotations

import csv
import hashlib
import html
import random
from collections import defaultdict
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .judgment import CODES, decision_code, load_jsonl, summarize_decisions

COUNTS = ("planned", "valid", "invalid", "missing", "E", "C", "R")
CSV_FIELDS = (
    "data_kind", "level", "model_id", "condition", "domain", "behavior", "tool_name",
    *COUNTS, "E_rate", "C_rate", "R_rate", "coverage", "invalid_rate", "missing_rate",
)
LEVELS = (
    ("overall", ("model_id",)),
    ("condition", ("model_id", "condition")),
    ("condition_behavior", ("model_id", "condition", "behavior")),
    ("tool_condition", ("model_id", "condition", "domain", "behavior", "tool_name")),
)
NAMES = {
    "omitted": "미표기", "consistent": "일치", "inconsistent": "상충",
    "read": "읽기 전용", "add": "추가적 변경", "destructive": "파괴적 변경",
}
EFFECT_FIELDS = (
    "data_kind", "model_id", "scope", "behavior", "comparison", "outcome",
    "consistent_rate", "inconsistent_rate", "delta", "ci_low", "ci_high",
    "permutation_p", "holm_adjusted_p", "n_clusters", "n_valid_consistent",
    "n_valid_inconsistent", "bootstrap_seed", "permutation_seed",
    "bootstrap_iterations", "permutation_iterations",
)
DEFAULT_ANALYSIS = {
    "bootstrap_seed": 20260908, "permutation_seed": 20260909,
    "bootstrap_iterations": 10000, "permutation_iterations": 10000,
    "multiple_comparison_correction": "holm_within_model_scope",
}


def report_rows(detail: list[dict[str, Any]], data_kind: str) -> list[dict[str, Any]]:
    """Roll up counts first, then calculate weighted rates."""
    output = []
    for level, dimensions in LEVELS:
        groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
        for row in detail:
            groups[tuple(row[field] for field in dimensions)].append(row)
        for key, members in sorted(groups.items()):
            result: dict[str, Any] = {field: "" for field in CSV_FIELDS}
            result.update(data_kind=data_kind, level=level)
            result.update(zip(dimensions, key))
            result.update({field: sum(row[field] for row in members) for field in COUNTS})
            result.update({code + "_rate": result[code] / result["valid"] if result["valid"] else None for code in CODES})
            result["coverage"] = result["valid"] / result["planned"] if result["planned"] else None
            result["invalid_rate"] = result["invalid"] / result["planned"] if result["planned"] else None
            result["missing_rate"] = result["missing"] / result["planned"] if result["planned"] else None
            if result["valid"] != sum(result[code] for code in CODES):
                raise ValueError("E+C+R does not equal valid responses")
            if result["planned"] != result["valid"] + result["invalid"] + result["missing"]:
                raise ValueError("Planned count does not match observed and missing counts")
            output.append(result)
    return output


def _row_seed(base: int, *parts: str) -> int:
    suffix = hashlib.sha256("\x1f".join(parts).encode("utf-8")).digest()
    return base + int.from_bytes(suffix[:8], "big")


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * probability
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = index - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def effect_rows(cases: list[dict[str, Any]], responses: list[dict[str, Any]], data_kind: str,
                *, model_ids: list[str] | None = None, analysis: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Paired tool-cluster contrasts; p-values are exploratory."""
    settings = dict(DEFAULT_ANALYSIS if analysis is None else analysis)
    if settings.get("multiple_comparison_correction") != "holm_within_model_scope":
        raise ValueError("Unsupported multiple comparison correction")
    observed = {(row.get("model_id"), row.get("case_id")): decision_code(row.get("decision")) for row in responses}
    models = list(model_ids or sorted({row.get("model_id") for row in responses if isinstance(row.get("model_id"), str)}))
    tools = sorted({case["tool_name"] for case in cases})
    behavior_by_tool = {case["tool_name"]: case["behavior"] for case in cases}
    output = []
    for model in models:
        for scope, behavior in (("overall", ""), *(("behavior", name) for name in ("read", "add", "destructive"))):
            scoped_tools = [tool for tool in tools if not behavior or behavior_by_tool[tool] == behavior]
            group_rows = []
            for outcome in CODES:
                clusters = []
                valid_counts = {"consistent": 0, "inconsistent": 0}
                for tool in scoped_tools:
                    rates = {}
                    tool_valid_counts = {}
                    for condition in ("consistent", "inconsistent"):
                        relevant = [case for case in cases if case["tool_name"] == tool and case["condition"] == condition]
                        values = [observed.get((model, case["case_id"])) for case in relevant]
                        valid = [value for value in values if value in CODES]
                        tool_valid_counts[condition] = len(valid)
                        if valid:
                            rates[condition] = sum(value == outcome for value in valid) / len(valid)
                    if set(rates) == {"consistent", "inconsistent"}:
                        clusters.append((rates["consistent"], rates["inconsistent"]))
                        for condition in valid_counts:
                            valid_counts[condition] += tool_valid_counts[condition]
                consistent_rate = sum(pair[0] for pair in clusters) / len(clusters) if clusters else None
                inconsistent_rate = sum(pair[1] for pair in clusters) / len(clusters) if clusters else None
                delta = inconsistent_rate - consistent_rate if clusters else None
                ci_low = ci_high = permutation_p = None
                if len(clusters) >= 2:
                    deltas = [pair[1] - pair[0] for pair in clusters]
                    bootstrap_rng = random.Random(_row_seed(settings["bootstrap_seed"], model, scope, behavior, outcome))
                    bootstrap = [sum(bootstrap_rng.choice(deltas) for _ in deltas) / len(deltas)
                                 for _ in range(settings["bootstrap_iterations"])]
                    ci_low, ci_high = _quantile(bootstrap, 0.025), _quantile(bootstrap, 0.975)
                    permutation_rng = random.Random(_row_seed(settings["permutation_seed"], model, scope, behavior, outcome))
                    observed_statistic = abs(sum(deltas) / len(deltas))
                    extreme = 0
                    for _ in range(settings["permutation_iterations"]):
                        permuted = sum(value if permutation_rng.getrandbits(1) else -value for value in deltas) / len(deltas)
                        extreme += abs(permuted) >= observed_statistic - 1e-15
                    permutation_p = (extreme + 1) / (settings["permutation_iterations"] + 1)
                group_rows.append({
                    "data_kind": data_kind, "model_id": model, "scope": scope, "behavior": behavior,
                    "comparison": "inconsistent-consistent", "outcome": outcome,
                    "consistent_rate": consistent_rate, "inconsistent_rate": inconsistent_rate, "delta": delta,
                    "ci_low": ci_low, "ci_high": ci_high, "permutation_p": permutation_p,
                    "holm_adjusted_p": None, "n_clusters": len(clusters),
                    "n_valid_consistent": valid_counts["consistent"],
                    "n_valid_inconsistent": valid_counts["inconsistent"], **settings,
                })
            ranked = sorted((row for row in group_rows if row["permutation_p"] is not None), key=lambda row: row["permutation_p"])
            running = 0.0
            for index, row in enumerate(ranked):
                running = max(running, min(1.0, row["permutation_p"] * (len(ranked) - index)))
                row["holm_adjusted_p"] = running
            output.extend(group_rows)
    return output


def _cell(value: Any) -> str:
    return html.escape(str(value)).replace("\\", "\\\\").replace("`", "\\`").replace("|", "\\|").replace("\r\n", "\n").replace("\n", "<br>")


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


def _table(rows: list[dict[str, Any]], dimensions: tuple[str, ...]) -> str:
    titles = {"model_id": "모델", "condition": "조건", "behavior": "도구 유형", "tool_name": "도구"}
    headers = [titles[field] for field in dimensions] + ["예정", "유효", "오류", "누락", "E", "C", "R", "E%", "C%", "R%", "응답 확보율", "오류율", "누락률"]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        values = [NAMES.get(row[field], row[field]) if field in ("condition", "behavior") else row[field] for field in dimensions]
        values += [row[field] for field in COUNTS]
        values += [_percent(row[code + "_rate"]) for code in CODES]
        values += [_percent(row["coverage"]), _percent(row["invalid_rate"]), _percent(row["missing_rate"])]
        lines.append("| " + " | ".join(_cell(value) for value in values) + " |")
    return "\n".join(lines)


def render_markdown(rows: list[dict[str, Any]], effects: list[dict[str, Any]], metadata: dict[str, Any]) -> str:
    demo = metadata["data_kind"] == "synthetic_demo"
    title = "[합성 예시] 판단 결과 보고서 — 실제 모델 실험 결과 아님" if demo else "판단 결과 보고서"
    lines = [f"# {title}", ""]
    if demo:
        lines += ["> **형식 검증을 위한 합성 데이터입니다. 모델의 성능이나 실제 판단 결과로 인용하지 마세요.**", ""]
    lines += [
        f"생성 시각(UTC): {_cell(metadata['generated_at_utc'])}", "",
        "## 1. 집계 기준", "",
        "- **E = EXECUTE(실행), C = CONFIRM(사용자 확인), R = REFUSE(거부)**",
        "- 유효 응답 N = E + C + R. E%, C%, R%는 각 값 / N × 100입니다.",
        "- 응답 확보율 = 유효 응답 / 예정 사례 수 × 100입니다.",
        "- invalid는 API 오류, 응답 차단·잘림, 형식 오류 등 유효한 판단을 얻지 못한 시행입니다. 누락(missing)과 함께 E/C/R에서 제외합니다.",
        "- 유효 응답이 0인 그룹의 비율은 ‘—’로 표시합니다. CSV에서는 빈 셀입니다.",
        "- 도구 실행 여부와 정답 정확도를 계산하지 않으며, 세 판단의 분포만 집계합니다.",
        "- 결과 표는 서로 다른 집계 수준입니다. 표 사이의 수치를 합산하지 않습니다.", "",
        "## 2. 모델별 전체 결과", "",
        _table([row for row in rows if row["level"] == "overall"], ("model_id",)), "",
        "## 3. 모델·조건별 결과", "",
        _table([row for row in rows if row["level"] == "condition"], ("model_id", "condition")), "",
        "## 4. 모델·조건·도구 유형별 결과", "",
        _table([row for row in rows if row["level"] == "condition_behavior"], ("model_id", "condition", "behavior")), "",
        "## 5. 개별 도구 결과", "",
        _table([row for row in rows if row["level"] == "tool_condition"], ("model_id", "condition", "tool_name")), "",
        "## 6. 핵심 효과량", "",
        "상충−일치의 양수 값은 해당 판단 비율이 상충 조건에서 증가했음을 뜻합니다. Tool을 군집으로 사용한 95% 부트스트랩 신뢰구간과 탐색적 순열검정 결과입니다.", "",
        "| 모델 | 범위 | 판단 | 일치율 | 상충율 | 차이 | 95% CI | 군집 수 | 보정 p |", "| --- | --- | --- | ---: | ---: | ---: | --- | ---: | ---: |",
        *["| " + " | ".join((_cell(row["model_id"]), _cell(NAMES.get(row["behavior"], row["scope"])), row["outcome"],
                               _percent(row["consistent_rate"]), _percent(row["inconsistent_rate"]), _percent(row["delta"]),
                               "—" if row["ci_low"] is None else f"{_percent(row['ci_low'])} ~ {_percent(row['ci_high'])}",
                               str(row["n_clusters"]), "—" if row["holm_adjusted_p"] is None else f"{row['holm_adjusted_p']:.4f}")) + " |" for row in effects], "",
        "기능 수준별 군집은 4개 Tool뿐이므로 신뢰구간과 p값은 탐색적으로 해석합니다.", "",
        "## 7. 해석 시 유의점", "",
        "동일 입력의 반복 응답은 별개의 도구나 업무 사례가 아닙니다. 기술 통계와 Tool 군집 기반의 탐색적 효과량·추론 결과를 함께 제공하며 인과 효과를 자동으로 주장하지 않습니다. 판단 분포의 차이와 함께 누락·형식 오류 및 표본 규모를 확인해야 합니다.", "",
        "모델 ID는 실행 계획 또는 응답 파일에 기록된 값을 사용합니다. 본 보고서 생성기는 모델을 호출하거나 모델 버전을 검증하지 않습니다. 실행 계획의 모델 목록을 제공하면 응답이 없는 모델도 전체 누락으로 포함합니다. 모델 목록을 생략한 경우 응답 파일에 나타난 모델만 집계합니다.", "",
        "## 8. 입력 자료와 재현 정보", "",
        f"- 자료 구분: {_cell(metadata['data_kind'])}",
        f"- 사례 수: {metadata['case_count']}",
        f"- 입력 응답 행 수: {metadata['response_count']}",
        f"- 사례 파일: {_cell(metadata['cases_path'])}",
        f"- 응답 파일: {_cell(metadata['responses_path'])}",
        f"- 사례 SHA-256: `{metadata['cases_sha256']}`",
        f"- 응답 SHA-256: `{metadata['responses_sha256']}`", "",
        "CSV의 E_rate, C_rate, R_rate, coverage, invalid_rate, missing_rate는 0~1 범위의 비율입니다. 집계 전에 긴 판단 이름과 E/C/R 약어를 통일하며 대소문자와 양끝 공백을 정규화합니다. 설명문에서 판단을 추측하여 추출하지 않습니다.", "",
    ]
    return "\n".join(lines)


def _csv_value(value: Any) -> Any:
    # Protect spreadsheet readers while leaving numeric columns numeric.
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def write_reports(cases_path: Path, responses_path: Path, destination: Path, *, demo: bool = False,
                  model_ids: list[str] | None = None, analysis: dict[str, Any] | None = None) -> dict[str, Any]:
    cases_path = cases_path.resolve()
    responses_path = responses_path.resolve()
    cases = load_jsonl(cases_path)
    responses = load_jsonl(responses_path)
    detail = summarize_decisions(cases, responses, model_ids=model_ids)
    demo = demo or any(response.get("data_kind") == "synthetic_demo" or str(response.get("model_id", "")).startswith("DEMO_ONLY") for response in responses)
    data_kind = "synthetic_demo" if demo else "provided_responses"
    rows = report_rows(detail, data_kind)
    effects = effect_rows(cases, responses, data_kind, model_ids=model_ids, analysis=analysis)
    suffix = destination.suffix.lower()
    if suffix not in ("", ".csv", ".md"):
        raise ValueError("Output must be a basename or a .csv/.md path")
    base = destination.with_suffix("") if suffix else destination
    csv_path = Path(str(base) + ".csv").resolve()
    md_path = Path(str(base) + ".md").resolve()
    effects_path = Path(str(base) + ".effects.csv").resolve()
    for path in (csv_path, md_path, effects_path):
        if path.exists():
            raise FileExistsError(f"Report already exists: {path}")
    metadata = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "data_kind": data_kind, "case_count": len(cases), "response_count": len(responses),
        "cases_path": str(cases_path), "responses_path": str(responses_path),
        "cases_sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest(),
        "responses_sha256": hashlib.sha256(responses_path.read_bytes()).hexdigest(),
    }
    markdown = render_markdown(rows, effects, metadata)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    created = []
    try:
        with ExitStack() as stack:
            csv_stream = stack.enter_context(csv_path.open("x", encoding="utf-8-sig", newline=""))
            created.append(csv_path)
            md_stream = stack.enter_context(md_path.open("x", encoding="utf-8", newline="\n"))
            created.append(md_path)
            effects_stream = stack.enter_context(effects_path.open("x", encoding="utf-8-sig", newline=""))
            created.append(effects_path)
            writer = csv.DictWriter(csv_stream, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows({field: _csv_value(row[field]) for field in CSV_FIELDS} for row in rows)
            effect_writer = csv.DictWriter(effects_stream, fieldnames=EFFECT_FIELDS)
            effect_writer.writeheader()
            effect_writer.writerows({field: _csv_value(row[field]) for field in EFFECT_FIELDS} for row in effects)
            md_stream.write(markdown)
    except BaseException:
        # Only remove new files created by this invocation; never prior reports.
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return {"report_md": str(md_path), "summary_csv": str(csv_path), "effects_csv": str(effects_path),
            "data_kind": data_kind, "rows": len(rows), "effect_rows": len(effects),
            "model_count": len({row['model_id'] for row in detail})}
