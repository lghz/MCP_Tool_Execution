"""Deterministic infrastructure check. It does not measure LLM decisions."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Callable

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from .catalog import BY_NAME
from .store import SEED, MockStore

READS = (
    ("files", "read_file", "file_id", "file_demo"),
    ("notes", "get_note", "note_id", "note_demo"),
    ("events", "get_event", "event_id", "event_demo"),
    ("records", "get_record", "record_id", "record_demo"),
)
CREATES = (
    ("create_file", {"file_id": "file_new", "content": "New virtual content"}),
    ("create_note", {"note_id": "note_new", "text": "New virtual note"}),
    ("create_event", {"event_id": "event_new", "title": "Virtual meeting", "start_time": "2026-09-11T10:00:00+09:00"}),
    ("insert_record", {"record_id": "record_new", "value": "New virtual value"}),
)


def wire(value: Any) -> dict[str, Any]:
    return value.model_dump(mode="json", by_alias=True, exclude_none=True)


async def run_smoke(observer: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    server = StdioServerParameters(command=sys.executable, args=["-m", "mcp_annotation_lab.server"])
    async with Client(server, read_timeout_seconds=30) as client:
        listing = await client.list_tools()
        if listing.next_cursor is not None:
            raise AssertionError("Unexpected pagination in the 12-tool catalogue")
        tools = [wire(tool) for tool in listing.tools]
        if observer:
            observer({"kind": "catalogue_received", "tools": tools, "protocol_version": client.protocol_version})
        if len(tools) != 12 or {tool["name"] for tool in tools} != set(BY_NAME):
            raise AssertionError("MCP tools/list did not return the fixed 12-tool catalogue")
        for tool in tools:
            expected = BY_NAME[tool["name"]]
            if tool.get("annotations") != expected.annotations:
                raise AssertionError(f"Annotation serialization mismatch: {tool['name']}")
            if tool.get("description") != expected.description:
                raise AssertionError(f"Description serialization mismatch: {tool['name']}")

        async def call(name: str, arguments: dict[str, Any], *, expected_error: bool = False) -> dict[str, Any]:
            started = perf_counter()
            response = await client.call_tool(name, arguments)
            actual_error = bool(response.is_error)
            event = {
                "kind": "infrastructure_call",
                "sequence": len(calls) + 1,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "tool_name": name,
                "arguments": arguments,
                "response": wire(response),
                "duration_ms": round((perf_counter() - started) * 1000, 3),
                "expected_error": expected_error,
                "actual_error": actual_error,
                "passed": actual_error == expected_error,
            }
            calls.append(event)
            if observer:
                observer(event)
            if actual_error != expected_error:
                raise AssertionError(f"Unexpected tool result: {name}: {wire(response)}")
            return response.structured_content or {}

        for domain, name, id_key, entity_id in READS:
            result = await call(name, {id_key: entity_id})
            if result != {"id": entity_id, **SEED[domain][entity_id]}:
                raise AssertionError(f"Incorrect initial state: {name}")

        for name, arguments in CREATES:
            await call(name, arguments)
        for name, arguments in CREATES:
            await call(name, arguments, expected_error=True)

        await call("delete_file", {"file_id": "file_new"})
        await call("overwrite_note", {"note_id": "note_new", "text": "Replacement virtual note"})
        await call("delete_event", {"event_id": "event_new"})
        await call("delete_record", {"record_id": "record_new"})

        await call("read_file", {"file_id": "file_new"}, expected_error=True)
        note = await call("get_note", {"note_id": "note_new"})
        if note.get("text") != "Replacement virtual note":
            raise AssertionError("Note overwrite did not replace the prior content")
        await call("get_event", {"event_id": "event_new"}, expected_error=True)
        await call("get_record", {"record_id": "record_new"}, expected_error=True)

        for domain, name, id_key, entity_id in READS:
            result = await call(name, {id_key: entity_id})
            if result != {"id": entity_id, **SEED[domain][entity_id]}:
                raise AssertionError(f"An unrelated seed item changed: {name}")

        protocol_version = client.protocol_version
        server_info = wire(client.server_info) if client.server_info else None

    seed_store = MockStore()
    try:
        seed_digest = seed_store.digest()
    finally:
        seed_store.close()

    return {
        "kind": "infrastructure_smoke",
        "condition": "truthful_baseline",
        "llm_inference_performed": False,
        "transport": "stdio",
        "protocol_version": protocol_version,
        "server_info": server_info,
        "seed_sha256": seed_digest,
        "tools": tools,
        "calls": calls,
        "status": "passed",
    }
