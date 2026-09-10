"""All tool-visible data lives in a private in-memory SQLite database.

Files are virtual records, not host filesystem paths. A fresh store starts from
the same seed. Only the experiment host constructs or resets stores.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from threading import RLock
from typing import Any

DOMAINS = ("files", "notes", "events", "records")
ID_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
SEED = {
    "files": {"file_demo": {"content": "Synthetic file content."}},
    "notes": {"note_demo": {"text": "Synthetic research note."}},
    "events": {"event_demo": {"title": "Synthetic meeting", "start_time": "2026-09-10T09:00:00+09:00"}},
    "records": {"record_demo": {"value": "Synthetic record value."}},
}


class MockStore:
    def __init__(self) -> None:
        self._lock = RLock()
        self._db = sqlite3.connect(":memory:", check_same_thread=False)
        for domain in DOMAINS:
            self._db.execute(f"CREATE TABLE {domain} (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        for domain, entries in SEED.items():
            for entity_id, payload in entries.items():
                self.create(domain, entity_id, payload)

    @staticmethod
    def _validate(domain: str, entity_id: str) -> None:
        if domain not in DOMAINS:
            raise ValueError("Unknown mock data domain")
        if not isinstance(entity_id, str) or not ID_PATTERN.fullmatch(entity_id):
            raise ValueError("Use a logical ID: lowercase letters, digits, underscore or hyphen, maximum 48 characters")

    @staticmethod
    def _payload(payload: dict[str, Any]) -> str:
        if "id" in payload:
            raise ValueError("The id field is reserved")
        if any(not isinstance(value, str) or len(value) > 4096 for value in payload.values()):
            raise ValueError("Each payload value must be a string of at most 4096 characters")
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def read(self, domain: str, entity_id: str) -> dict[str, Any]:
        self._validate(domain, entity_id)
        with self._lock:
            row = self._db.execute(f"SELECT payload FROM {domain} WHERE id = ?", (entity_id,)).fetchone()
        if row is None:
            raise ValueError(f"Mock item does not exist: {entity_id}")
        return {"id": entity_id, **json.loads(row[0])}

    def create(self, domain: str, entity_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._validate(domain, entity_id)
        encoded = self._payload(payload)
        with self._lock, self._db:
            if self._db.execute(f"SELECT COUNT(*) FROM {domain}").fetchone()[0] >= 1000:
                raise ValueError("Mock domain capacity reached")
            try:
                self._db.execute(f"INSERT INTO {domain} VALUES (?, ?)", (entity_id, encoded))
            except sqlite3.IntegrityError as error:
                raise ValueError(f"Mock item already exists: {entity_id}; no overwrite performed") from error
            return self.read(domain, entity_id)

    def overwrite(self, domain: str, entity_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._validate(domain, entity_id)
        encoded = self._payload(payload)
        with self._lock, self._db:
            cursor = self._db.execute(f"UPDATE {domain} SET payload = ? WHERE id = ?", (encoded, entity_id))
            if cursor.rowcount != 1:
                raise ValueError(f"Mock item does not exist: {entity_id}")
            return self.read(domain, entity_id)

    def delete(self, domain: str, entity_id: str) -> dict[str, str]:
        self._validate(domain, entity_id)
        with self._lock, self._db:
            cursor = self._db.execute(f"DELETE FROM {domain} WHERE id = ?", (entity_id,))
            if cursor.rowcount != 1:
                raise ValueError(f"Mock item does not exist: {entity_id}")
        return {"deleted": entity_id}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                domain: {entity_id: json.loads(payload) for entity_id, payload in self._db.execute(f"SELECT id, payload FROM {domain} ORDER BY id")}
                for domain in DOMAINS
            }

    def digest(self) -> str:
        data = json.dumps(self.snapshot(), ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(data).hexdigest()

    def close(self) -> None:
        with self._lock:
            self._db.close()

