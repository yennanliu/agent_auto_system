"""Unit tests for the custom-automation store (Phase 3G)."""
import json

import pytest

from src import custom_automations as ca


@pytest.fixture
def store(test_engine, mocker):
    """Point the store's get_engine() at the in-memory test DB."""
    import src.database as _db
    mocker.patch.object(_db, "engine", test_engine)
    return ca


def test_slugify():
    assert ca.slugify("Tagline Writer") == "tagline_writer"
    assert ca.slugify("  A / B! ") == "a_b"
    assert ca.slugify("x" * 100) == "x" * 40  # capped


def test_is_custom():
    assert ca.is_custom("custom:x") is True
    assert ca.is_custom("hacker_news_digest") is False


def test_create_get_and_enabled(store):
    row = store.create(
        name="Tagline Writer", instructions="write a tagline",
        fields=[{"name": "product", "label": "Product", "type": "text"}],
    )
    assert row.slug == "tagline_writer"
    got = store.get_by_job_type("custom:tagline_writer")
    assert got and got.id == row.id
    assert store.is_enabled("custom:tagline_writer") is True
    assert store.get_by_job_type("hacker_news_digest") is None  # built-ins aren't custom


def test_duplicate_rejected(store):
    store.create(name="Dup", instructions="x")
    with pytest.raises(ValueError, match="already exists"):
        store.create(name="Dup", instructions="y")


@pytest.mark.parametrize("name,instructions", [("", "x"), ("X", ""), ("!!!", "x")])
def test_invalid_definitions_rejected(store, name, instructions):
    with pytest.raises(ValueError):
        store.create(name=name, instructions=instructions)


def test_fields_normalized_and_capped(store):
    fields = [{"name": f"f{i}", "label": f"F{i}", "type": "text"} for i in range(20)]
    fields += [{"name": "", "type": "text"},                    # dropped: no name
               {"name": "Weird Name!", "type": "bogus"}]        # name + type normalized
    row = store.create(name="Many", instructions="x", fields=fields)
    parsed = json.loads(row.fields_json)
    assert len(parsed) <= ca.MAX_FIELDS
    assert all(f["type"] in ca._ALLOWED_FIELD_TYPES for f in parsed)
    assert all(f["name"] and " " not in f["name"] for f in parsed)


def test_length_and_temperature_caps(store):
    row = store.create(name="Long", instructions="a" * 9000, temperature=5.0)
    assert len(row.instructions) <= ca.MAX_INSTRUCTIONS
    assert 0.0 <= row.temperature <= 1.0


def test_manifest_entries_shape_and_toggle(store):
    row = store.create(name="Toggle Me", instructions="x",
                       fields=[{"name": "q", "label": "Q", "type": "text"}])
    entry = next(e for e in store.manifest_entries() if e["job_type"] == "custom:toggle_me")
    assert entry["custom_ui"] is False and entry["custom"] is True
    assert [f["name"] for f in entry["fields"]] == ["q"]
    assert entry["steps"][0] == ["Start", "Starting"]

    store.set_enabled(row.id, False)
    assert store.is_enabled("custom:toggle_me") is False
    assert not any(e["job_type"] == "custom:toggle_me" for e in store.manifest_entries())


def test_to_public_and_delete(store):
    row = store.create(name="Bye", instructions="do the thing", output_hint="JSON")
    pub = store.to_public(row)
    assert pub["job_type"] == "custom:bye" and pub["enabled"] is True
    assert store.delete(row.id) is True
    assert store.get_by_job_type("custom:bye") is None
    assert store.delete(999999) is False
