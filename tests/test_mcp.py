import asyncio

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from mcp_annotation_lab.smoke import run_smoke
from mcp_annotation_lab.server import build_server


def test_real_stdio_transport_catalogue_and_all_tools():
    report = asyncio.run(run_smoke())
    assert report["status"] == "passed"
    assert len(report["tools"]) == 12
    assert len(report["calls"]) == 24
    assert len({call["tool_name"] for call in report["calls"]}) == 12
    assert all(call["passed"] for call in report["calls"])
    assert sum(call["expected_error"] for call in report["calls"]) == 7


def test_server_rejects_invalid_event_without_creating_it():
    async def check():
        async with Client(build_server()) as client:
            invalid = await client.call_tool("create_event", {"event_id": "bad_event", "title": "Example", "start_time": "2026-09-11T10:00:00"})
            assert invalid.is_error
            missing = await client.call_tool("get_event", {"event_id": "bad_event"})
            assert missing.is_error
    asyncio.run(check())
