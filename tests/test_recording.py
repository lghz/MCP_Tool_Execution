import json

import pytest

from mcp_annotation_lab import cli


def test_recording_failure_preserves_events_and_failed_status(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)

    async def broken_run(observer):
        observer({"kind": "infrastructure_call", "sequence": 1, "passed": False})
        raise RuntimeError("Synthetic transport failure")

    monkeypatch.setattr(cli, "run_smoke", broken_run)
    with pytest.raises(RuntimeError, match="Synthetic transport failure"):
        cli.save_smoke()
    run = next((tmp_path / "runs").iterdir())
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["finished_at_utc"]
    assert json.loads((run / "events.jsonl").read_text(encoding="utf-8"))["passed"] is False
    assert not (run / "summary.json").exists()

