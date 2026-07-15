"""Integration tests for GET /api/system — catalog shape and completeness."""


async def test_system_returns_200(client):
    resp = await client.get("/api/system")
    assert resp.status_code == 200


async def test_system_has_four_categories(client):
    data = (await client.get("/api/system")).json()
    assert set(data.keys()) >= {"agents", "tools", "crews", "workflows"}


# ── Agents ────────────────────────────────────────────────────────────────────

async def test_agents_list_not_empty(client):
    data = (await client.get("/api/system")).json()
    assert len(data["agents"]) > 0


async def test_all_expected_agents_present(client):
    data = (await client.get("/api/system")).json()
    ids = {a["id"] for a in data["agents"]}
    assert "form_agent" in ids
    assert "web_scraper_agent" in ids
    assert "hn_analyst" in ids
    assert "x_analyst" in ids
    assert "email_sender_agent" in ids
    assert "google_sheet_agent" in ids


async def test_each_agent_has_required_fields(client):
    data = (await client.get("/api/system")).json()
    for agent in data["agents"]:
        assert "id" in agent
        assert "name" in agent
        assert "role" in agent
        assert "goal" in agent
        assert "tools" in agent
        assert "crew" in agent


async def test_agents_have_source_code(client):
    data = (await client.get("/api/system")).json()
    for agent in data["agents"]:
        assert agent.get("source_code"), f"Agent {agent['id']} missing source_code"


# ── Tools ─────────────────────────────────────────────────────────────────────

async def test_all_expected_tools_present(client):
    data = (await client.get("/api/system")).json()
    ids = {t["id"] for t in data["tools"]}
    assert "google_form_inspector" in ids
    assert "google_form_submit" in ids
    assert "web_scraper" in ids
    assert "hn_top_stories" in ids
    assert "x_post_scraper" in ids
    assert "gmail_send_email" in ids
    assert "google_sheet_reader" in ids


async def test_gmail_tool_has_inputs(client):
    data = (await client.get("/api/system")).json()
    gmail_tool = next(t for t in data["tools"] if t["id"] == "gmail_send_email")
    input_names = {inp["name"] for inp in gmail_tool["inputs"]}
    assert "to" in input_names
    assert "subject" in input_names
    assert "body" in input_names
    assert "cc" in input_names


async def test_web_scraper_tool_has_no_question_input(client):
    """After removing the question field, web_scraper tool should only need url."""
    data = (await client.get("/api/system")).json()
    tool = next(t for t in data["tools"] if t["id"] == "web_scraper")
    input_names = {inp["name"] for inp in tool["inputs"]}
    assert "url" in input_names
    assert "question" not in input_names


async def test_tools_have_source_code(client):
    data = (await client.get("/api/system")).json()
    for tool in data["tools"]:
        assert tool.get("source_code"), f"Tool {tool['id']} missing source_code"


# ── Crews ─────────────────────────────────────────────────────────────────────

async def test_all_expected_crews_present(client):
    data = (await client.get("/api/system")).json()
    ids = {c["id"] for c in data["crews"]}
    assert "form_filler_crew" in ids
    assert "web_scraper_crew" in ids
    assert "hn_digest_crew" in ids
    assert "x_scraper_crew" in ids
    assert "email_sender_crew" in ids
    assert "google_sheet_crew" in ids


async def test_crews_have_tasks_with_config_code(client):
    data = (await client.get("/api/system")).json()
    for crew in data["crews"]:
        for task in crew.get("tasks", []):
            assert "config_code" in task, (
                f"Crew {crew['id']} task {task['name']} missing config_code"
            )


async def test_web_scraper_crew_task_has_no_question_placeholder(client):
    data = (await client.get("/api/system")).json()
    ws_crew = next(c for c in data["crews"] if c["id"] == "web_scraper_crew")
    for task in ws_crew["tasks"]:
        assert "{question}" not in task["description"]


# ── Workflows ─────────────────────────────────────────────────────────────────

async def test_all_expected_workflows_present(client):
    data = (await client.get("/api/system")).json()
    ids = {w["id"] for w in data["workflows"]}
    assert "form_fill_flow" in ids
    assert "web_scraper_flow" in ids
    assert "hn_digest_flow" in ids
    assert "x_scraper_flow" in ids
    assert "email_sender_flow" in ids
    assert "google_sheet_flow" in ids
    assert "pipeline" in ids


async def test_web_scraper_flow_has_no_question_state_field(client):
    data = (await client.get("/api/system")).json()
    ws_flow = next(w for w in data["workflows"] if w["id"] == "web_scraper_flow")
    field_names = {f["name"] for f in ws_flow["state_fields"]}
    assert "url" in field_names
    assert "question" not in field_names


async def test_email_sender_flow_has_required_state_fields(client):
    data = (await client.get("/api/system")).json()
    email_flow = next(w for w in data["workflows"] if w["id"] == "email_sender_flow")
    field_names = {f["name"] for f in email_flow["state_fields"]}
    assert "to" in field_names
    assert "subject" in field_names
    assert "body" in field_names
    assert "cc" in field_names


async def test_email_sender_flow_has_two_steps(client):
    data = (await client.get("/api/system")).json()
    email_flow = next(w for w in data["workflows"] if w["id"] == "email_sender_flow")
    assert len(email_flow["steps"]) == 2
    assert email_flow["steps"][0]["decorator"] == "@start()"


async def test_workflows_have_source_code(client):
    data = (await client.get("/api/system")).json()
    for wf in data["workflows"]:
        assert wf.get("source_code"), f"Workflow {wf['id']} missing source_code"


async def test_google_sheet_tool_has_url_and_limit_inputs(client):
    data = (await client.get("/api/system")).json()
    tool = next(t for t in data["tools"] if t["id"] == "google_sheet_reader")
    input_names = {inp["name"] for inp in tool["inputs"]}
    assert "url" in input_names
    assert "limit" in input_names


async def test_pipeline_workflow_has_source_code(client):
    data = (await client.get("/api/system")).json()
    pipeline_wf = next(w for w in data["workflows"] if w["id"] == "pipeline")
    assert pipeline_wf.get("source_code")
