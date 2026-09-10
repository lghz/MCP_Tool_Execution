"""Fixed catalogue. Annotations are derived from implemented behavior."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ToolSpec:
    name: str
    domain: str
    mode: Literal["read", "add", "destructive"]
    description: str

    @property
    def annotations(self) -> dict[str, bool]:
        result = {"readOnlyHint": self.mode == "read", "openWorldHint": False}
        if self.mode != "read":
            result["destructiveHint"] = self.mode == "destructive"
        return result


TOOLS = (
    ToolSpec("read_file", "files", "read", "Read a virtual file by logical ID in the closed mock store. Does not change any data."),
    ToolSpec("create_file", "files", "add", "Create a new virtual file in the closed mock store. Fails if its logical ID exists; never overwrites existing data."),
    ToolSpec("delete_file", "files", "destructive", "Delete an existing virtual file from the closed mock store. Its current content is removed. No host filesystem paths are accepted."),
    ToolSpec("get_note", "notes", "read", "Read a note by logical ID in the closed mock store. Does not change any data."),
    ToolSpec("create_note", "notes", "add", "Add a new note to the closed mock store. Fails if its logical ID exists; never overwrites existing data."),
    ToolSpec("overwrite_note", "notes", "destructive", "Replace the complete text of an existing note in the closed mock store. The previous text is lost within this session."),
    ToolSpec("get_event", "events", "read", "Read an event by logical ID in the closed mock store. Does not change any data or contact a calendar service."),
    ToolSpec("create_event", "events", "add", "Add a new event to the closed mock store. Fails if its logical ID exists. No invitations or external messages are sent."),
    ToolSpec("delete_event", "events", "destructive", "Delete an existing event from the closed mock store. The event is removed within this session. No external calendar is contacted."),
    ToolSpec("get_record", "records", "read", "Read a database record by logical ID in the closed mock store. Does not change any data."),
    ToolSpec("insert_record", "records", "add", "Insert a new database record in the closed mock store. Fails if its logical ID exists; does not update existing records."),
    ToolSpec("delete_record", "records", "destructive", "Delete an existing database record from the closed mock store. The record is removed within this session."),
)
BY_NAME = {tool.name: tool for tool in TOOLS}

