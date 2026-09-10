"""Offline project commands. This module has no LLM API integration."""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import importlib.metadata
import json
import platform
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from .catalog import BY_NAME, TOOLS
from .judgment import export_cases
from .reporting import write_reports
from .smoke import run_smoke, wire

ROOT = Path(__file__).resolve().parents[2]


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def environment() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "project_root": str(ROOT),
        "mcp_version": importlib.metadata.version("mcp"),
        "lab_version": importlib.metadata.version("mcp-annotation-lab"),
        "sqlite_storage": "private in-memory database per server process",
        "transport": "stdio",
        "tool_count": len(TOOLS),
        "tool_groups": dict(Counter(tool.mode for tool in TOOLS)),
        "mcp_server_annotation_condition": "truthful_baseline",
        "llm_api_integration": "text_only_adapters_available; use runner plan for experiment configuration",
        "llm_inference_performed": False,
        "research_endpoint": "judgment_only_EXECUTE_CONFIRM_REFUSE",
    }


def source_hashes() -> dict[str, str]:
    paths = sorted((ROOT / "src" / "mcp_annotation_lab").glob("*.py"))
    paths.extend(path for path in (ROOT / "pyproject.toml", ROOT / "requirements.lock", ROOT / "config" / "local-baseline.json") if path.is_file())
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


async def fetch_catalogue() -> dict[str, Any]:
    parameters = StdioServerParameters(command=sys.executable, args=["-m", "mcp_annotation_lab.server"])
    async with Client(parameters, read_timeout_seconds=30) as client:
        result = await client.list_tools()
        if result.next_cursor is not None:
            raise ValueError("Unexpected paginated catalogue")
        return {"protocol_version": client.protocol_version, "tools": [wire(tool) for tool in result.tools]}


def save_smoke() -> tuple[Path, dict[str, Any]]:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ") + "_" + uuid.uuid4().hex[:8]
    run_dir = ROOT / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "kind": "infrastructure_smoke",
        "status": "running",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": environment(),
        "source_sha256": source_hashes(),
        "dependencies": dict(sorted((distribution.metadata["Name"], distribution.version) for distribution in importlib.metadata.distributions())),
    }
    (run_dir / "manifest.json").write_text(json_text(manifest), encoding="utf-8")

    def observe(event: dict[str, Any]) -> None:
        if event["kind"] == "catalogue_received":
            (run_dir / "catalogue.json").write_text(json_text(event), encoding="utf-8")
        with (run_dir / "events.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    try:
        report = asyncio.run(run_smoke(observer=observe))
        (run_dir / "report.json").write_text(json_text(report), encoding="utf-8")
        manifest.update(status="passed", protocol_version=report["protocol_version"], seed_sha256=report["seed_sha256"])
        counts = Counter(call["tool_name"] for call in report["calls"])
        with (run_dir / "summary.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["tool_name", "domain", "mode", "call_count", "expected_error_count", "passed_count"])
            writer.writeheader()
            for name, spec in BY_NAME.items():
                calls = [call for call in report["calls"] if call["tool_name"] == name]
                writer.writerow({"tool_name": name, "domain": spec.domain, "mode": spec.mode, "call_count": counts[name], "expected_error_count": sum(call["expected_error"] for call in calls), "passed_count": sum(call["passed"] for call in calls)})
        summary = {
            "run_directory": str(run_dir),
            "status": "passed",
            "tools_verified": len(report["tools"]),
            "calls_verified": len(report["calls"]),
            "expected_errors_verified": sum(call["expected_error"] for call in report["calls"]),
            "llm_inference_performed": False,
        }
        (run_dir / "summary.json").write_text(json_text(summary), encoding="utf-8")
        return run_dir, summary
    except BaseException as error:
        manifest.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed", error_type=type(error).__name__, error_message=str(error))
        raise
    finally:
        manifest["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        (run_dir / "manifest.json").write_text(json_text(manifest), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local MCP infrastructure checks; no LLM API usage")
    parser.add_argument("command", choices=["doctor", "catalog", "smoke", "prepare", "summarize"])
    parser.add_argument("--cases", type=Path, help="Local cases.jsonl for summarize")
    parser.add_argument("--responses", type=Path, help="Local observed decisions JSONL for summarize")
    parser.add_argument("--output", type=Path, help="Report basename or .csv/.md path; creates both files without overwriting")
    parser.add_argument("--demo", action="store_true", help="Mark output as synthetic demonstration data")
    args = parser.parse_args()
    if args.command == "doctor":
        print(json_text(environment()), end="")
    elif args.command == "catalog":
        print(json_text(asyncio.run(fetch_catalogue())), end="")
    elif args.command == "smoke":
        _, summary = save_smoke()
        print(json_text(summary), end="")
    elif args.command == "prepare":
        dataset_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        destination = ROOT / "datasets" / dataset_id
        manifest = export_cases(asyncio.run(fetch_catalogue()), destination)
        print(json_text({"dataset_directory": str(destination), **manifest}), end="")
    else:
        if not all((args.cases, args.responses, args.output)):
            parser.error("summarize requires --cases, --responses and --output")
        result = write_reports(args.cases, args.responses, args.output, demo=args.demo)
        print(json_text(result), end="")


if __name__ == "__main__":
    main()
