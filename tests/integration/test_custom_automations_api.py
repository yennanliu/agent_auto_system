"""Integration tests for admin-authored no-code automations (Phase 3G)."""

_DEF = {
    "name": "Tagline Writer",
    "instructions": "Write a punchy one-line tagline for the given product.",
    "icon": "🏷️",
    "description": "Product → tagline",
    "output_hint": "A JSON object with a 'tagline' string.",
    "fields": [{"name": "product", "label": "Product", "type": "text", "required": True}],
    "temperature": 0.5,
}


async def test_create_lists_and_appears_in_manifest(client):
    resp = await client.post("/api/admin/custom-automations", json=_DEF)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["job_type"] == "custom:tagline_writer"
    assert created["enabled"] is True

    listed = (await client.get("/api/admin/custom-automations")).json()
    assert any(a["slug"] == "tagline_writer" for a in listed)

    manifest = (await client.get("/api/automations/manifest")).json()["automations"]
    entry = next(a for a in manifest if a["job_type"] == "custom:tagline_writer")
    assert entry["custom_ui"] is False  # renders via the generic form
    assert [f["name"] for f in entry["fields"]] == ["product"]
    assert entry["steps"][0] == ["Start", "Starting"]


async def test_custom_job_passes_the_run_gate(client):
    """A created custom automation is runnable — POST /api/jobs is not blocked."""
    await client.post("/api/admin/custom-automations", json=_DEF)
    job = await client.post("/api/jobs", json={
        "name": "tagline run", "job_type": "custom:tagline_writer",
        "payload": {"product": "a smart kettle"},
    })
    assert job.status_code == 201, job.text


async def test_disabled_custom_automation_is_blocked(client):
    await client.post("/api/admin/custom-automations", json=_DEF)
    row = (await client.get("/api/admin/custom-automations")).json()[0]
    await client.patch(f"/api/admin/custom-automations/{row['id']}", json={"enabled": False})
    # Disabled → dropped from the manifest and blocked at the run gate.
    manifest = (await client.get("/api/automations/manifest")).json()["automations"]
    assert not any(a["job_type"] == "custom:tagline_writer" for a in manifest)
    job = await client.post("/api/jobs", json={
        "name": "x", "job_type": "custom:tagline_writer", "payload": {"product": "z"},
    })
    assert job.status_code == 403


async def test_delete(client):
    await client.post("/api/admin/custom-automations", json=_DEF)
    row = (await client.get("/api/admin/custom-automations")).json()[0]
    assert (await client.delete(f"/api/admin/custom-automations/{row['id']}")).status_code == 204
    assert (await client.get("/api/admin/custom-automations")).json() == []


async def test_invalid_definition_rejected(client):
    resp = await client.post("/api/admin/custom-automations",
                             json={"name": "", "instructions": "x"})
    assert resp.status_code == 400


async def test_requires_admin(anon_client):
    """Unauthenticated (and non-admin) callers cannot create custom automations."""
    resp = await anon_client.post("/api/admin/custom-automations", json=_DEF)
    assert resp.status_code in (401, 403)
