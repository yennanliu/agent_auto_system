import os

os.environ.setdefault("OTEL_ENABLED", "false")
os.environ["CREWAI_DISABLE_TELEMETRY"] = "true"  # forced: real crew kickoffs must never phone home

import json

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from src.models import Job  # ensure both tables are in metadata


@pytest.fixture(autouse=True)
def _stub_evaluate(mocker):
    """Keep the executor's evaluate node hermetic by default (no real LLM judge call).

    Tests that exercise the evaluator itself patch src.automation.harness.evaluator
    internals directly instead.
    """
    from src.automation.harness.evaluator import EvalResult
    mocker.patch(
        "src.automation.executor.evaluate",
        return_value=EvalResult(score=90.0, confidence=0.9, notes="stub", method="heuristic"),
    )


@pytest.fixture(scope="function")
def test_engine():
    # StaticPool: all connections share the same in-memory SQLite database.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    yield engine
    SQLModel.metadata.drop_all(engine)


@pytest.fixture
def db_session(test_engine):
    with Session(test_engine) as s:
        yield s


@pytest_asyncio.fixture
async def anon_client(test_engine, mocker):
    """ASGI client with NO authenticated session (for testing the auth gate itself)."""
    import src.database as _db
    from src.main import app

    # Patch the module-level engine so get_session(), get_engine(), and init_db()
    # all operate on the in-memory test database.
    mocker.patch.object(_db, "engine", test_engine)
    mocker.patch("src.routers.runs.get_engine", return_value=test_engine)
    mocker.patch("src.automation.executor.get_engine", return_value=test_engine)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def seed_admin(test_engine):
    """An admin user with a known password, available before the client logs in."""
    from src.auth import hash_password
    from src.models import User

    with Session(test_engine) as s:
        user = User(
            username="tester",
            password_hash=hash_password("password123"),
            is_admin=True,
            is_active=True,
            allowed_automations="*",
        )
        s.add(user)
        s.commit()
        s.refresh(user)
        return user


@pytest_asyncio.fixture
async def client(anon_client, seed_admin):
    """Authenticated client: most tests assume access. Logs in the seeded admin
    so its session cookie rides along on every subsequent request."""
    resp = await anon_client.post(
        "/api/auth/login", json={"username": "tester", "password": "password123"}
    )
    assert resp.status_code == 200, resp.text
    yield anon_client


@pytest.fixture
def seed_job(db_session):
    job = Job(
        name="Test Form Job",
        job_type="google_form_fill",
        payload=json.dumps({
            "company_name": "Acme Corp",
            "company_size": "0-10",
            "ai_problem": "Automate test triage",
        }),
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job
