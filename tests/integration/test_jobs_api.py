import json

PAYLOAD = {
    "name": "My Form Job",
    "job_type": "google_form_fill",
    "payload": {"company_name": "Acme", "company_size": "0-10", "ai_problem": "triage"},
}


async def test_create_job_returns_201(client):
    resp = await client.post("/api/jobs", json=PAYLOAD)
    assert resp.status_code == 201
    data = resp.json()
    assert data["id"] is not None
    assert data["name"] == "My Form Job"
    assert data["job_type"] == "google_form_fill"


async def test_list_jobs_empty(client):
    resp = await client.get("/api/jobs")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_jobs_after_create(client):
    await client.post("/api/jobs", json=PAYLOAD)
    await client.post("/api/jobs", json={**PAYLOAD, "name": "Job 2"})
    resp = await client.get("/api/jobs")
    assert len(resp.json()) == 2


async def test_get_job_by_id(client):
    created = (await client.post("/api/jobs", json=PAYLOAD)).json()
    resp = await client.get(f"/api/jobs/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]


async def test_get_job_not_found(client):
    resp = await client.get("/api/jobs/9999")
    assert resp.status_code == 404


async def test_delete_job(client):
    created = (await client.post("/api/jobs", json=PAYLOAD)).json()
    resp = await client.delete(f"/api/jobs/{created['id']}")
    assert resp.status_code == 204

    resp = await client.get(f"/api/jobs/{created['id']}")
    assert resp.status_code == 404


async def test_delete_job_not_found(client):
    resp = await client.delete("/api/jobs/9999")
    assert resp.status_code == 404


async def test_patch_job_replaces_payload(client):
    created = (await client.post("/api/jobs", json=PAYLOAD)).json()
    new_payload = {**PAYLOAD["payload"], "company_name": "Globex"}
    resp = await client.patch(f"/api/jobs/{created['id']}", json={"payload": new_payload})
    assert resp.status_code == 200
    assert json.loads(resp.json()["payload"]) == new_payload
    assert resp.json()["name"] == "My Form Job"  # untouched


async def test_patch_job_without_payload_keeps_it(client):
    created = (await client.post("/api/jobs", json=PAYLOAD)).json()
    resp = await client.patch(f"/api/jobs/{created['id']}", json={"name": "Renamed"})
    assert json.loads(resp.json()["payload"]) == PAYLOAD["payload"]
