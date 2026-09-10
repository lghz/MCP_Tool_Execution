"""Exactly twelve truthful tools over local stdio; no model/API client."""

from datetime import datetime

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from .catalog import BY_NAME
from .store import MockStore


def build_server(store: MockStore | None = None) -> MCPServer:
    data = store if store is not None else MockStore()
    server = MCPServer(
        "mcp-annotation-lab",
        version="0.1.0",
        description="Closed synthetic file, note, event and record store. All state is session-local and annotations match tool behavior.",
        log_level="WARNING",
    )

    def register(name: str):
        spec = BY_NAME[name]
        return server.tool(name=name, description=spec.description, annotations=ToolAnnotations(**spec.annotations), structured_output=True)

    @register("read_file")
    def read_file(file_id: str) -> dict[str, str]:
        return data.read("files", file_id)

    @register("create_file")
    def create_file(file_id: str, content: str) -> dict[str, str]:
        return data.create("files", file_id, {"content": content})

    @register("delete_file")
    def delete_file(file_id: str) -> dict[str, str]:
        return data.delete("files", file_id)

    @register("get_note")
    def get_note(note_id: str) -> dict[str, str]:
        return data.read("notes", note_id)

    @register("create_note")
    def create_note(note_id: str, text: str) -> dict[str, str]:
        return data.create("notes", note_id, {"text": text})

    @register("overwrite_note")
    def overwrite_note(note_id: str, text: str) -> dict[str, str]:
        return data.overwrite("notes", note_id, {"text": text})

    @register("get_event")
    def get_event(event_id: str) -> dict[str, str]:
        return data.read("events", event_id)

    @register("create_event")
    def create_event(event_id: str, title: str, start_time: str) -> dict[str, str]:
        instant = datetime.fromisoformat(start_time)
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("start_time must be an ISO 8601 timestamp with a UTC offset")
        return data.create("events", event_id, {"title": title, "start_time": instant.isoformat()})

    @register("delete_event")
    def delete_event(event_id: str) -> dict[str, str]:
        return data.delete("events", event_id)

    @register("get_record")
    def get_record(record_id: str) -> dict[str, str]:
        return data.read("records", record_id)

    @register("insert_record")
    def insert_record(record_id: str, value: str) -> dict[str, str]:
        return data.create("records", record_id, {"value": value})

    @register("delete_record")
    def delete_record(record_id: str) -> dict[str, str]:
        return data.delete("records", record_id)

    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
