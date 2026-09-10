import pytest

from mcp_annotation_lab.catalog import TOOLS
from mcp_annotation_lab.store import DOMAINS, SEED, MockStore


@pytest.fixture
def store():
    instance = MockStore()
    yield instance
    instance.close()


def test_fixed_catalogue():
    assert len(TOOLS) == len({tool.name for tool in TOOLS}) == 12
    for domain in DOMAINS:
        assert {tool.mode for tool in TOOLS if tool.domain == domain} == {"read", "add", "destructive"}


def test_reads_leave_all_domains_unchanged(store):
    before = store.digest()
    for domain, entries in SEED.items():
        for entity_id, payload in entries.items():
            assert store.read(domain, entity_id) == {"id": entity_id, **payload}
    assert store.digest() == before


@pytest.mark.parametrize("domain", DOMAINS)
def test_add_preserves_existing_and_rejects_duplicates(store, domain):
    before = store.snapshot()[domain]
    store.create(domain, "new_item", {"value": "new"})
    assert all(store.snapshot()[domain][key] == value for key, value in before.items())
    digest = store.digest()
    with pytest.raises(ValueError, match="already exists"):
        store.create(domain, "new_item", {"value": "replacement"})
    assert store.digest() == digest


def test_overwrite_changes_only_target(store):
    before = store.snapshot()
    store.overwrite("notes", "note_demo", {"text": "replacement"})
    after = store.snapshot()
    assert after["notes"]["note_demo"] == {"text": "replacement"}
    for domain in ("files", "events", "records"):
        assert before[domain] == after[domain]


@pytest.mark.parametrize("domain,entity_id", [("files", "file_demo"), ("events", "event_demo"), ("records", "record_demo")])
def test_delete_only_target(store, domain, entity_id):
    before = store.snapshot()
    assert store.delete(domain, entity_id) == {"deleted": entity_id}
    after = store.snapshot()
    assert entity_id not in after[domain]
    for other in set(DOMAINS) - {domain}:
        assert before[other] == after[other]
    with pytest.raises(ValueError, match="does not exist"):
        store.read(domain, entity_id)


@pytest.mark.parametrize("entity_id", ["../private", "D:\\data", "/etc/passwd", "x' OR 1=1", "", "a" * 49])
def test_logical_ids_reject_paths_and_invalid_input(store, entity_id):
    before = store.digest()
    with pytest.raises(ValueError):
        store.delete("files", entity_id)
    assert store.digest() == before


def test_payload_limit_and_unknown_domain(store):
    with pytest.raises(ValueError):
        store.create("files", "oversized", {"content": "x" * 4097})
    with pytest.raises(ValueError):
        store.read("unknown", "item")


def test_new_session_restores_seed_and_is_independent(store):
    original = store.digest()
    store.delete("files", "file_demo")
    fresh = MockStore()
    try:
        assert fresh.snapshot() == SEED
        assert fresh.digest() == original
        assert fresh.digest() != store.digest()
    finally:
        fresh.close()
