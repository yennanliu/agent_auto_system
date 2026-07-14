"""Unit tests for SSO account resolution (find → link → provision).

The OAuth network dance is not exercised here — only the pure DB-side policy in
``src.oauth.resolve_sso_user`` and the username helper.
"""
from sqlmodel import select

from src.auth import hash_password
from src.models import User
from src.oauth import _unique_username, resolve_sso_user


def _mk(session, **kw):
    user = User(**{"username": "u", "is_active": True, **kw})
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


# ── Provisioning (open policy) ────────────────────────────────────────────────

def test_provisions_new_account(db_session):
    user = resolve_sso_user(
        db_session, provider="google", sub="G1",
        email="alice@example.com", email_verified=True, name="Alice",
    )
    db_session.commit()
    assert user.id is not None
    assert user.oauth_provider == "google"
    assert user.oauth_sub == "G1"
    assert user.email == "alice@example.com"
    assert not user.password_hash          # SSO-only: no password login (empty sentinel)
    assert user.is_admin is False          # never auto-granted admin
    assert user.allowed_automations == "[]"  # no automations until granted


def test_unverified_email_not_trusted_onto_new_account(db_session):
    user = resolve_sso_user(
        db_session, provider="github", sub="H1",
        email="spoof@example.com", email_verified=False, name="mallory",
    )
    db_session.commit()
    # Identity is created, but the unverified email is not stored.
    assert user.oauth_sub == "H1"
    assert user.email is None
    assert user.username == "mallory"  # derived from name, not the email


# ── Returning user (already linked) ───────────────────────────────────────────

def test_returns_existing_linked_account(db_session):
    existing = _mk(db_session, username="bob", oauth_provider="google", oauth_sub="G2")
    got = resolve_sso_user(
        db_session, provider="google", sub="G2",
        email="bob@example.com", email_verified=True, name="Bob",
    )
    assert got.id == existing.id
    # No duplicate account created.
    assert len(db_session.exec(select(User)).all()) == 1


def test_refreshes_email_on_linked_login(db_session):
    _mk(db_session, username="carol", oauth_provider="google", oauth_sub="G3", email=None)
    got = resolve_sso_user(
        db_session, provider="google", sub="G3",
        email="carol@example.com", email_verified=True, name="Carol",
    )
    db_session.commit()
    assert got.email == "carol@example.com"


# ── Linking by verified email ─────────────────────────────────────────────────

def test_links_verified_email_to_password_account(db_session):
    pw = _mk(db_session, username="dave", email="dave@example.com",
             password_hash=hash_password("pw"))
    got = resolve_sso_user(
        db_session, provider="google", sub="G4",
        email="dave@example.com", email_verified=True, name="Dave",
    )
    db_session.commit()
    assert got.id == pw.id
    assert got.oauth_provider == "google"
    assert got.oauth_sub == "G4"
    assert got.password_hash is not None  # keeps its password login too
    assert len(db_session.exec(select(User)).all()) == 1


def test_links_legacy_account_whose_username_is_the_email(db_session):
    legacy = _mk(db_session, username="erin@example.com",
                 password_hash=hash_password("pw"))  # predates the email column
    got = resolve_sso_user(
        db_session, provider="github", sub="H5",
        email="erin@example.com", email_verified=True, name="Erin",
    )
    db_session.commit()
    assert got.id == legacy.id
    assert got.oauth_provider == "github"
    assert got.email == "erin@example.com"  # backfilled


def test_unverified_email_does_not_link(db_session):
    _mk(db_session, username="frank", email="frank@example.com",
        password_hash=hash_password("pw"))
    got = resolve_sso_user(
        db_session, provider="google", sub="G6",
        email="frank@example.com", email_verified=False, name="Frank",
    )
    db_session.commit()
    assert got.oauth_provider == "google"  # a new, separate account
    assert got.email is None
    assert len(db_session.exec(select(User)).all()) == 2


def test_does_not_link_to_account_already_linked_elsewhere(db_session):
    # An account linked to Google should not be hijacked by a GitHub login that
    # happens to report the same email — a fresh account is created instead.
    _mk(db_session, username="gina", email="gina@example.com",
        oauth_provider="google", oauth_sub="G7")
    got = resolve_sso_user(
        db_session, provider="github", sub="H7",
        email="gina@example.com", email_verified=True, name="Gina",
    )
    db_session.commit()
    assert got.oauth_provider == "github"
    assert got.oauth_sub == "H7"
    assert len(db_session.exec(select(User)).all()) == 2


# ── Username uniqueness ───────────────────────────────────────────────────────

def test_unique_username_appends_suffix_on_collision(db_session):
    _mk(db_session, username="alice")
    assert _unique_username(db_session, "alice@corp.com") == "alice2"


def test_unique_username_sanitises_and_falls_back(db_session):
    assert _unique_username(db_session, "!!!@corp.com") == "user"
    assert _unique_username(db_session, "a.b_c@corp.com") == "a.b_c"
