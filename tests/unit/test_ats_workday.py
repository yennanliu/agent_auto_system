"""Workday discovery adapter (ats_workday) against a real captured response. No network."""
import json
from pathlib import Path

import httpx
import pytest

from src.automation.tools import ats_apply_tool as ats
from src.automation.tools.ats_router import BoardRef
from src.automation.tools.ats_workday import WorkdayAdapter, split_token

FIX = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "ats" / "workday_jobs.json")
                 .read_text(encoding="utf-8"))
API = "https://3m.wd1.myworkdayjobs.com/wday/cxs/3m/Search/jobs"


class _Resp:
    def __init__(self, status, data=None):
        self.status_code, self._data = status, data

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("POST", API)
            raise httpx.HTTPStatusError("x", request=req, response=httpx.Response(self.status_code, request=req))


class _Client:
    def __init__(self, *responses):
        self.responses, self.bodies = list(responses), []

    def post(self, url, json=None):
        assert url == API
        self.bodies.append(json)
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def _no_stored_passwords(monkeypatch, tmp_path):
    """Never read the real Keychain / settings DB: no tenant has a password
    unless a test sets WORKDAY_PASSWORD_*."""
    import src.automation.tools.ats_workday as wd
    real = wd.password_for

    def env_only(host, email=""):
        import os
        return os.getenv(wd.env_name(host)) or None
    monkeypatch.setattr(wd, "password_for", env_only)
    monkeypatch.setenv("WORKDAY_ACCOUNTS_FILE", str(tmp_path / "workday_accounts.json"))
    yield real


def _adapter():
    sleeps = []
    return WorkdayAdapter(sleep=sleeps.append), sleeps


def test_split_token():
    assert split_token("ngc.wd1/Northrop_Grumman_External_Site") == ("ngc", "wd1", "Northrop_Grumman_External_Site")
    with pytest.raises(ValueError):
        split_token("ngc")


def test_discover_uses_the_first_keywords_as_search_text():
    ad, _ = _adapter()
    client = _Client(_Resp(200, FIX), _Resp(200, FIX))
    jobs = ad.discover(client, BoardRef("workday", "3m.wd1/Search"), search=("engineer", "", "data"))
    assert [b["searchText"] for b in client.bodies] == ["engineer", "data"]
    assert all(b["limit"] == 20 and b["offset"] == 0 for b in client.bodies)  # short page → stop
    j = jobs[0]
    assert j["platform"] == "workday" and j["token"] == "3m.wd1/Search" and j["company"] == "3m"
    assert j["url"].startswith("https://3m.wd1.myworkdayjobs.com/Search/job/")
    assert j["location"] == FIX["jobPostings"][0]["locationsText"]
    assert len(jobs) == len(FIX["jobPostings"])                           # deduped across terms


def test_discover_pages_until_total():
    ad, sleeps = _adapter()
    full = {"total": 45, "jobPostings": [{"title": f"Engineer {i}", "externalPath": f"/job/US/E_{i}",
                                          "locationsText": "US"} for i in range(20)]}
    page2 = {"total": 45, "jobPostings": [{"title": f"Engineer {i}", "externalPath": f"/job/US/E_{i}",
                                           "locationsText": "US"} for i in range(20, 40)]}
    last = {"total": 45, "jobPostings": [{"title": "Engineer 40", "externalPath": "/job/US/E_40"}]}
    client = _Client(_Resp(200, full), _Resp(200, page2), _Resp(200, last))
    jobs = ad.discover(client, BoardRef("workday", "3m.wd1/Search"), search=("engineer",))
    assert [b["offset"] for b in client.bodies] == [0, 20, 40] and len(jobs) == 41
    assert sleeps == [1.0, 1.0]                                           # paced between pages


def test_one_job_ref_searches_its_requisition_id():
    ad, _ = _adapter()
    client = _Client(_Resp(200, FIX))
    ad.discover(client, BoardRef("workday", "3m.wd1/Search", "Sr-EHS-Engineer_R01164059"))
    assert client.bodies[0]["searchText"] == "R01164059"


def test_429_backs_off_then_succeeds():
    ad, sleeps = _adapter()
    client = _Client(_Resp(429), _Resp(429), _Resp(200, FIX))
    assert ad.discover(client, BoardRef("workday", "3m.wd1/Search"), search=("engineer",))
    assert sleeps == [5, 10]


def test_persistent_429_raises_for_the_scanner_to_pause():
    ad, _ = _adapter()
    client = _Client(*[_Resp(429)] * 5)
    with pytest.raises(httpx.HTTPStatusError):
        ad.discover(client, BoardRef("workday", "3m.wd1/Search"))


@pytest.mark.parametrize("status", [403, 404, 410, 422])
def test_board_errors_are_dead_never_waited_on(status):
    ad, sleeps = _adapter()
    with pytest.raises(RuntimeError, match="no such Workday site"):
        ad.discover(_Client(_Resp(status)), BoardRef("workday", "3m.wd1/Search"))
    assert sleeps == []


def test_runner_lists_workday_jobs_as_manual_without_a_resume(tmp_path, mocker):
    mocker.patch("playwright.sync_api.sync_playwright")
    launch = mocker.patch.object(ats, "launch_browser")
    ad, _ = _adapter()
    client = _Client(_Resp(200, FIX))
    out = ats.run_ats_apply(boards="https://3m.wd1.myworkdayjobs.com/en-US/Search", keywords="engineer",
                            first_name="A", last_name="B", email="a@b.c", resume_path="",
                            client=client, adapters={"workday": ad}, ledger_path=str(tmp_path / "l.json"))
    assert "error" not in out and out["manual_count"] == len(FIX["jobPostings"])
    assert all("no Workday account stored for 3m.wd1" in m["reason"] for m in out["manual"])
    assert all("WORKDAY_PASSWORD_3M_WD1" in m["reason"] for m in out["manual"])
    launch.assert_not_called()


# ── review fixes (PR #113) ───────────────────────────────────────────────────

from src.automation.tools.ats_workday import search_terms  # noqa: E402


def test_search_terms_expand_role_shorthand_round_robin():
    assert search_terms(["swe"])[0] == "software engineer"                # never the bare "swe"
    assert search_terms(["swe", "sre", "backend"]) == [
        "software engineer", "site reliability", "backend",               # every keyword first
        "software developer", "reliability engineer"]                     # then 2nd alternatives, cap 5
    assert search_terms(["Python", "python", " "]) == ["Python"]
    assert search_terms([]) == []


def test_discover_sends_expanded_terms_for_shorthand():
    ad, _ = _adapter()
    client = _Client(*[_Resp(200, FIX)] * 3)
    ad.discover(client, BoardRef("workday", "3m.wd1/Search"), search=("swe",))
    assert [b["searchText"] for b in client.bodies] == [
        "software engineer", "software developer", "software development engineer"]


def test_two_job_links_on_one_workday_board_are_both_looked_up(tmp_path, mocker):
    """A Workday job ref searches one requisition; it must not be cached as the
    whole board, or the second job on that board would be 'not found'."""
    mocker.patch("playwright.sync_api.sync_playwright")
    mocker.patch.object(ats, "launch_browser")
    posting = {"jobPostings": FIX["jobPostings"][:1], "total": 1}
    posting2 = {"jobPostings": FIX["jobPostings"][1:2], "total": 1}
    ad, _ = _adapter()
    client = _Client(_Resp(200, posting), _Resp(200, posting2))
    slug1 = FIX["jobPostings"][0]["externalPath"].rsplit("/", 1)[-1]
    slug2 = FIX["jobPostings"][1]["externalPath"].rsplit("/", 1)[-1]
    out = ats.run_ats_apply(
        boards=f"workday:3m.wd1/Search/{slug1}, workday:3m.wd1/Search/{slug2}",
        first_name="A", last_name="B", email="a@b.c", client=client, adapters={"workday": ad},
        ledger_path=str(tmp_path / "l.json"))
    assert len(client.bodies) == 2                                        # two searches, not one
    assert sorted(m["job_id"] for m in out["manual"]) == sorted([slug1, slug2])
    assert not any("not found" in w for w in out["warnings"])


def test_board_listing_adapters_still_list_each_board_once(tmp_path, mocker):
    mocker.patch("playwright.sync_api.sync_playwright")
    mocker.patch.object(ats, "launch_browser")
    calls = []

    class Board(ats.Adapter):
        platform, label, can_apply = "lever", "Lever", False

        def discover(self, client, ref):
            calls.append(ref.job_id)
            return [{"platform": "lever", "token": "acme", "job_id": j, "title": "Engineer",
                     "company": "acme", "location": "", "url": "u", "apply_url": "u"}
                    for j in ("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222")]
    ats.run_ats_apply(boards="lever:acme/11111111-1111-1111-1111-111111111111, "
                             "lever:acme/22222222-2222-2222-2222-222222222222",
                      first_name="A", last_name="B", email="a@b.c", client=_Client(),
                      adapters={"lever": Board()}, ledger_path=str(tmp_path / "l.json"))
    assert len(calls) == 1


# ── apply (Workday wizard, planned in the browser) ──────────────────────────

from src.automation.tools import ats_workday as wd  # noqa: E402
from src.automation.tools.ats_common import build_profile  # noqa: E402

PROFILE = build_profile(first_name="Ada", last_name="Lovelace", email="ada@example.com",
                        phone="+1 415 555 0100", current_location="San Francisco, CA",
                        authorized_countries="United States", years_experience=5,
                        extra_answers="relative presently employed: No")


def _f(label, kind="listbox", required=True, options=(), value=""):
    return {"index": 0, "label": label, "kind": kind, "required": required,
            "options": list(options), "value": value}


def test_host_and_env_name():
    assert wd.host_of("3m.wd1/Search") == "3m.wd1"
    assert wd.host_of("Wf.WD1/WellsFargoJobs") == "wf.wd1"
    assert wd.env_name("3m.wd1") == "WORKDAY_PASSWORD_3M_WD1"


def test_password_from_env_first(monkeypatch, _no_stored_passwords):
    real = _no_stored_passwords
    monkeypatch.setenv("WORKDAY_PASSWORD_ACME_WD5", "s3cret!")
    assert real("acme.wd5", "a@b.c") == "s3cret!"


def test_password_falls_back_to_the_settings_store(monkeypatch, _no_stored_passwords, mocker):
    real = _no_stored_passwords
    monkeypatch.delenv("WORKDAY_PASSWORD_ACME_WD5", raising=False)
    mocker.patch("src.settings_store.get_workday_password", return_value="from-db")
    assert real("acme.wd5", "") == "from-db"


def test_workday_system_fields_get_defaults():
    plan = wd.plan_field(_f("Phone Device Type", options=["Select One", "Landline", "Mobile"]), PROFILE)
    assert plan == {"action": "pick", "value": "Mobile"}
    plan = wd.plan_field(_f("Country", options=["Canada", "United States", "Mexico"]), PROFILE)
    assert plan["value"] == "United States"                     # from "San Francisco, CA"
    plan = wd.plan_field(_f("How Did You Hear About Us?", options=["LinkedIn", "3M Website", "Referral"]),
                         PROFILE)
    assert plan["value"] == "3M Website"


def test_names_come_from_the_profile_by_label():
    assert wd.plan_field(_f("First Name", "text"), PROFILE) == {"action": "fill", "value": "Ada"}
    assert wd.plan_field(_f("Last Name", "text"), PROFILE)["value"] == "Lovelace"


def test_work_authorization_and_sponsorship():
    yn = ["Yes", "No"]
    assert wd.plan_field(_f("Are you legally authorized to work in the United States?", "radio",
                            options=yn), PROFILE)["value"] == "Yes"
    assert wd.plan_field(_f("Will you now or in the future require sponsorship?", options=yn),
                         PROFILE)["value"] == "No"


def test_job_country_reads_the_first_segment():
    from src.automation.tools.ats_common import job_countries
    assert wd.job_country("US-Minnesota-Maplewood") == "United States"
    assert job_countries(wd.job_country("Canada-Ontario-London")) == {"canada"}   # not the UK
    assert job_countries(wd.job_country("2 Locations")) == set()                  # unknown stays unknown


def test_personal_facts_are_never_guessed_but_extra_answers_count():
    yn = ["Yes", "No"]
    at_3m = {**PROFILE, "job_company": "3m"}
    assert wd.plan_field(_f("Have you ever been employed by Epic Systems?", options=yn), at_3m) is None
    assert wd.plan_field(_f("Have you ever been employed by a State government?", options=yn), at_3m) is None
    assert wd.plan_field(_f("Have you ever been employed by 3M or any of its subsidiaries?", options=yn),
                         at_3m)["value"] == "No"
    assert wd.plan_field(_f("Do you have a relative presently employed by 3M?", options=yn),
                         PROFILE)["value"] == "No"


def test_signed_agreement_is_not_consent():
    lbl = "Have you signed an agreement with a current or former employer containing a non-compete?"
    assert wd.plan_field(_f(lbl, options=["Yes", "No"]), PROFILE) is None


def test_demographics_decline():
    plan = wd.plan_field(_f("Please select your sex.", options=["Male", "Female", "Not declared"]), PROFILE)
    assert plan["value"] == "Not declared"


def test_only_a_required_terms_box_is_ticked():
    terms = _f("Terms and Conditions", "checkbox", options=["I have read and consent to the terms"])
    assert wd.plan_field(terms, PROFILE) == {"action": "tick", "value": "checked"}
    assert wd.plan_field({**terms, "required": False}, PROFILE) is None
    assert wd.plan_field(_f("Subscribe to job alerts", "checkbox", options=["Yes"]), PROFILE) is None


def test_unknown_widgets_are_left_alone():
    assert wd.plan_field(_f("Skills", "selectinput"), PROFILE) is None
    assert wd.plan_field(_f("Start date", "other"), PROFILE) is None


def _job(i="Sr-Engineer_R1"):
    return {"platform": "workday", "token": "3m.wd1/Search", "job_id": i, "title": "Senior Engineer",
            "company": "3m", "location": "US", "url": f"https://3m.wd1.myworkdayjobs.com/Search/job/US/{i}",
            "apply_url": f"https://3m.wd1.myworkdayjobs.com/Search/job/US/{i}"}


class _LiveFake(wd.WorkdayAdapter):
    """Discovery and the browser are faked; the runner's routing is real."""

    def __init__(self, result):
        super().__init__(sleep=lambda _s: None)
        self.result, self.calls = result, []

    def discover(self, client, ref, search=()):
        return [_job()]

    def load_form(self, client, job):
        raise AssertionError("plans_in_browser adapters never load a form over HTTP")

    def apply_live(self, page, job, profile, answer_fn, dry_run, log):
        self.calls.append((job["job_id"], profile["email"], dry_run))
        return {**ats.entry_for(job, []), **self.result}


@pytest.fixture
def live_runner(tmp_path, mocker, monkeypatch):
    mocker.patch("playwright.sync_api.sync_playwright")
    mocker.patch.object(ats, "launch_browser")
    resume = tmp_path / "cv.pdf"
    resume.write_bytes(b"%PDF-1.4")

    def run(adapter, **kw):
        return ats.run_ats_apply(boards="workday:3m.wd1/Search", keywords="engineer", first_name="A",
                                 last_name="B", email="a@b.c", resume_path=str(resume),
                                 adapters={"workday": adapter}, client=object(),
                                 ledger_path=str(tmp_path / "l.json"), **kw)
    return run


def test_with_a_stored_password_the_runner_applies_in_the_browser(live_runner, monkeypatch):
    monkeypatch.setenv("WORKDAY_PASSWORD_3M_WD1", "pw")
    ad = _LiveFake({"status": "prepared", "submitted": False})
    out = live_runner(ad)
    assert ad.calls == [("Sr-Engineer_R1", "a@b.c", True)]
    assert out["applied_count"] == 1 and out["manual_count"] == 0


def test_without_a_password_the_job_is_manual_and_no_browser_opens(live_runner):
    ad = _LiveFake({"status": "prepared"})
    out = live_runner(ad)
    assert ad.calls == [] and out["manual_count"] == 1
    assert "no Workday account stored for 3m.wd1" in out["manual"][0]["reason"]


def test_an_unanswerable_step_skips_the_job_with_its_questions(live_runner, monkeypatch):
    monkeypatch.setenv("WORKDAY_PASSWORD_3M_WD1", "pw")
    ad = _LiveFake({"status": "skipped", "submitted": False, "unanswered": ["Epic Systems?"],
                    "reason": "unanswered required question(s): Epic Systems?"})
    out = live_runner(ad)
    assert out["applied_count"] == 0 and out["skipped"][0]["unanswered"] == ["Epic Systems?"]


def test_an_unconfirmed_live_submit_is_recorded_pending(live_runner, monkeypatch, tmp_path):
    monkeypatch.setenv("WORKDAY_PASSWORD_3M_WD1", "pw")
    ad = _LiveFake({"status": "unconfirmed", "submitted": False})
    out = live_runner(ad, dry_run=False)
    assert out["unconfirmed_count"] == 1
    from src.automation.tools.ats_common import load_ledger
    assert load_ledger(str(tmp_path / "l.json"))["workday:3m.wd1/Search/Sr-Engineer_R1"]["status"] == "pending"


# ── local accounts file (opt-in account creation) ───────────────────────────

def test_accounts_file_is_git_ignored():
    """The accounts file holds passwords: data/ must stay in .gitignore. Asked of
    git when this is a checkout; otherwise the .gitignore rule itself is checked.
    The Docker image has neither (it ships app files only), so it skips there."""
    import shutil
    import subprocess
    repo = Path(__file__).resolve().parents[2]
    if shutil.which("git") and (repo / ".git").exists():
        out = subprocess.run(["git", "check-ignore", "-q", wd.DEFAULT_ACCOUNTS_PATH], cwd=repo)
        assert out.returncode == 0, f"{wd.DEFAULT_ACCOUNTS_PATH} must be git-ignored: it holds passwords"
    elif not (repo / ".gitignore").exists():
        pytest.skip("not a source checkout (e.g. the Docker image): nothing here can be committed")
    else:
        rules = {line.strip() for line in (repo / ".gitignore").read_text().splitlines()}
        assert rules & {"data/", "data", "/data/", "/data", wd.DEFAULT_ACCOUNTS_PATH}, \
            ".gitignore must ignore data/: it holds Workday passwords"


def test_save_load_mark_forget(tmp_path):
    import os
    import stat
    path = str(tmp_path / "sub" / "accounts.json")
    wd.save_account("acme.wd1", "Ada@Example.com", "pw1", path=path)
    assert wd.load_accounts(path)["acme.wd1"]["ada@example.com"]["password"] == "pw1"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    wd.mark_account("acme.wd1", "ada@example.com", "active", path=path)
    assert wd.load_accounts(path)["acme.wd1"]["ada@example.com"]["status"] == "active"
    wd.forget_account("acme.wd1", "ada@example.com", path=path)
    assert wd.load_accounts(path) == {"acme.wd1": {}}
    assert wd.load_accounts(str(tmp_path / "missing.json")) == {}


def test_generated_passwords_meet_workday_rules():
    import re as _re
    seen = set()
    for _ in range(50):
        pw = wd.generate_password()
        assert len(pw) == 20 and all(_re.search(p, pw) for p in (r"[A-Z]", r"[a-z]", r"[0-9]", r"[^A-Za-z0-9]"))
        seen.add(pw)
    assert len(seen) == 50


def test_password_for_reads_the_local_file(monkeypatch, _no_stored_passwords, mocker):
    real = _no_stored_passwords
    mocker.patch("src.settings_store.get_workday_password", return_value=None)
    monkeypatch.setattr(wd.sys, "platform", "linux")
    wd.save_account("acme.wd5", "a@b.c", "local-pw", status="active")
    assert real("acme.wd5", "A@B.C") == "local-pw"
    wd.mark_account("acme.wd5", "a@b.c", "create_failed")
    assert real("acme.wd5", "a@b.c") is None                                  # a failed creation is not an account


def test_opt_in_makes_a_job_without_an_account_applicable():
    ad = wd.WorkdayAdapter()
    assert ad.manual_reason_for(_job(), {"email": "a@b.c"})
    ad.create_accounts = True
    assert ad.manual_reason_for(_job(), {"email": "a@b.c"}) is None
    assert ad.manual_reason_for(_job(), {"email": ""})                         # no e-mail: still manual


def test_runner_passes_the_opt_in_to_the_adapter(live_runner):
    ad = _LiveFake({"status": "prepared"})
    out = live_runner(ad, create_workday_accounts=True)
    assert ad.create_accounts is True and ad.calls and out["manual_count"] == 0


# Micron (Taiwan), live 2026-11-21: the Country Phone Code widget holds the
# "+886", and Workday rejects "+886 963335868" in Phone Number as an invalid format.
@pytest.mark.parametrize("phone,code,expected", [
    ("+886 963335868", "Taiwan (+886)", "963335868"),
    ("+1 415 555 0100", "United States of America (+1)", "4155550100"),
    ("+1 415 555 0100", "Taiwan (+886)", "+1 415 555 0100"),   # another country's code: as typed
    ("0963-335-868", "Taiwan (+886)", "0963-335-868"),         # already national
    ("+886 963335868", "", "+886 963335868"),                  # no widget value: unchanged
])
def test_national_number(phone, code, expected):
    assert wd.national_number(phone, code) == expected


def test_phone_number_drops_the_code_the_widget_already_holds():
    tw = build_profile(first_name="Ada", last_name="Lovelace", email="ada@example.com",
                       phone="+886 963335868")
    plan = wd.plan_field({**_f("Phone Number", kind="text"), "phone_code": "Taiwan (+886)"}, tw)
    assert plan == {"action": "fill", "value": "963335868"}
    # Without a country-code widget on the step the number is typed in full.
    assert wd.plan_field(_f("Phone Number", kind="text"), tw)["value"] == "+886 963335868"


# HP (Taiwan), live 2026-11-23: My Experience requires a work-experience entry
# (title, company, From MM/YYYY), the degree and a language with its level.
HP_DEGREES = ["Graduate Certificate", "High School or GED (±11 years of education)",
              "Bachelors (±16 years of education)", "Masters (±18 years of education)",
              "Doctorate (over 19 years of education)", "Other (other type of degree or diploma)"]
EXP = build_profile(first_name="Ada", last_name="Lovelace", email="ada@example.com",
                    current_company="Self-employed", current_title="Sr. Backend Engineer",
                    current_start="2025-06", school="National Taiwan University",
                    degree="Master's Degree", languages="English, Chinese",
                    extra_answers="english proficiency: 4 - Fluent")


def _sec(field, label="x", kind="text", options=()):
    return {**_f(label, kind=kind, options=options), "field": field}


def test_my_experience_current_job_education_and_language():
    assert wd.plan_field(_sec("formField-jobTitle"), EXP) == {"action": "fill", "value": "Sr. Backend Engineer"}
    assert wd.plan_field(_sec("formField-companyName"), EXP) == {"action": "fill", "value": "Self-employed"}
    assert wd.plan_field(_sec("formField-currentlyWorkHere", kind="checkbox",
                              options=["I currently work here"]), EXP)["action"] == "tick"
    assert wd.plan_field(_sec("formField-startDate", kind="date"), EXP) == {"action": "date", "value": "06/2025"}
    assert wd.plan_field(_sec("formField-endDate", kind="date"), EXP) is None
    assert wd.plan_field(_sec("formField-schoolName"), EXP)["value"] == "National Taiwan University"
    assert wd.plan_field(_sec("formField-degree", kind="listbox", options=HP_DEGREES), EXP)["value"] == \
        "Masters (±18 years of education)"
    langs = ["Select One", "Chinese, Cantonese", "Chinese, Mandarin", "English"]
    assert wd.plan_field(_sec("formField-language", kind="listbox", options=langs), EXP)["value"] == "English"
    levels = ["1 - Beginner", "2 - Intermediate", "3 - Advanced", "4 - Fluent"]
    assert wd.plan_field(_sec("formField-98cbd30d", label="Overall", kind="listbox", options=levels),
                         EXP)["value"] == "4 - Fluent"


def test_my_experience_never_guesses():
    bare = build_profile(first_name="A", last_name="B", email="a@b.c", languages="Chinese")
    for field, kind in (("formField-jobTitle", "text"), ("formField-companyName", "text"),
                        ("formField-currentlyWorkHere", "checkbox"), ("formField-startDate", "date"),
                        ("formField-schoolName", "text")):
        assert wd.plan_field(_sec(field, kind=kind, options=["I currently work here"]), bare) is None, field
    # "Chinese" is ambiguous on a list with Cantonese and Mandarin; no level without an answer.
    langs = ["Chinese, Cantonese", "Chinese, Mandarin", "English"]
    assert wd.plan_field(_sec("formField-language", kind="listbox", options=langs), bare) is None
    assert wd.plan_field(_sec("formField-x", label="Overall", kind="listbox", options=["4 - Fluent"]), bare) is None


@pytest.mark.parametrize("degree,options,expected", [
    ("Master's Degree", HP_DEGREES, "Masters (±18 years of education)"),
    ("MSc Physics", HP_DEGREES, "Masters (±18 years of education)"),
    ("Bachelor of Science", HP_DEGREES, "Bachelors (±16 years of education)"),
    ("PhD", HP_DEGREES, "Doctorate (over 19 years of education)"),
    # Western Digital (SmartRecruiters): two master's options, so no pick.
    ("Master's Degree", ["Bachelors of Science", "Master of Science", "Master of Arts"], None),
    ("", HP_DEGREES, None),
])
def test_degree_option_by_level(degree, options, expected):
    from src.automation.tools.ats_common import degree_option
    assert degree_option(options, degree) == expected


# Applied Materials (Taiwan), live 2026-11-23: the tenant id "amat" doesn't
# spell "Have you ever worked at Applied Materials…?"; the posting's
# hiringOrganization does.
@pytest.mark.parametrize("raw,clean", [
    ("0027 Applied Materials Taiwan, Ltd", "Applied Materials"), ("HP Inc.", "HP"),
    ("Micron Technology, Inc.", "Micron Technology"), ("3M Company", "3M"),
    ("Western Digital Corporation", "Western Digital"), ("NVIDIA", "NVIDIA"), ("", ""),
])
def test_clean_company_name(raw, clean):
    from src.automation.tools.ats_common import clean_company_name
    assert clean_company_name(raw) == clean


def test_prior_employment_by_the_employers_own_name():
    from src.automation.tools.ats_common import rule_answer, with_job_location
    p = with_job_location(PROFILE, None, "amat", aliases=("Applied Materials",))
    assert rule_answer("Have you ever worked at Applied Materials as a regular employee, "
                       "contingent worker, intern, etc.?", p) == "No"
    assert rule_answer("Have you ever worked at Applied Signal before?", p) is None


def test_a_source_prompt_is_answered_like_a_listbox():
    """Applied Materials, live: "How Did You Hear About Us?" is a selectinput
    prompt; its items get the same source answer as a listbox."""
    from src.automation.tools.ats_common import with_job_location
    items = ["Applied Materials Corporate Website", "I currently work at/for Applied Materials",
             "Job Board or Social Media", "Staffing Agency"]
    p = with_job_location(PROFILE, None, "amat", aliases=("Applied Materials",))
    f = {**_f("How Did You Hear About Us?", kind="selectinput", options=items)}
    assert wd.plan_field(f, p) == {"action": "pick", "value": "Applied Materials Corporate Website"}
    # A search-as-you-type prompt with no items (skills) is left alone.
    assert wd.plan_field(_f("Type to Add Skills", kind="selectinput", required=False), p) is None


def test_a_choose_all_checkbox_group_ticks_only_the_named_boxes():
    """Applied Materials, live 2026-11-23: "choose all cities that you would accept"."""
    cities = ["Linkou", "Hsinchu", "Taichung", "Tainan", "Kaohsiung"]
    q = "Applied Materials Taiwan has offices in 5 cities. Please choose all cities that you would accept."
    p = build_profile(first_name="A", last_name="B", email="a@b.c",
                      extra_answers="cities that you would accept: Linkou, Hsinchu")
    assert wd.plan_field(_f(q, kind="checkbox", options=cities), p) == \
        {"action": "ticks", "value": ["Linkou", "Hsinchu"]}
    # No answer, or a city that isn't offered: nothing is ticked.
    assert wd.plan_field(_f(q, kind="checkbox", options=cities), PROFILE) is None
    other = build_profile(first_name="A", last_name="B", email="a@b.c",
                          extra_answers="cities that you would accept: Linkou, Taipei")
    assert wd.plan_field(_f(q, kind="checkbox", options=cities), other) is None


# Intel (US), live 2026-11-23: the Self Identify step (CC-305 disability form).
def test_self_identify_signs_today_and_declines():
    from datetime import datetime
    assert wd.plan_field(_sec("formField-dateSignedOn", label="Date", kind="date"), PROFILE) == \
        {"action": "date", "value": datetime.now().strftime("%m/%d/%Y")}
    boxes = ["Yes, I have a disability, or have had one in the past",
             "No, I do not have a disability and have not had one in the past", "I do not want to answer"]
    assert wd.plan_field(_sec("formField-disabilityStatus", label="Please check one of the boxes below:",
                              kind="checkbox", options=boxes), PROFILE) == \
        {"action": "ticks", "value": ["I do not want to answer"]}


@pytest.mark.parametrize("plan,value,same", [
    ({"action": "pick", "value": "Yes"}, "No", False),          # a stale draft answer is corrected
    ({"action": "pick", "value": "Yes"}, "Yes", True),
    ({"action": "fill", "value": "963335868"}, "963335868", True),
    ({"action": "ticks", "value": ["Linkou", "Hsinchu"]}, "Linkou, Hsinchu", True),
    ({"action": "ticks", "value": ["Neither"]}, "Linkou", False),
    ({"action": "tick", "value": "checked"}, "I agree", True),
    ({"action": "date", "value": "06/2025"}, "6/2025", True),
    ({"action": "date", "value": "11/23/2026"}, "", False),
    # Workday's own re-formatting is not a different answer (Intel, live).
    ({"action": "fill", "value": "963335868"}, "0963 335 868", True),
    ({"action": "fill", "value": "Self-employed"}, "Self-Employed", True),
    ({"action": "fill", "value": "963335868"}, "0912 345 678", False),
])
def test_same_answer_detection(plan, value, same):
    assert wd._same(plan, {"value": value}) is same


def test_next_field_includes_empty_prompts_and_rechecks_filled_choices():
    """Trend Micro, live 2026-11-24: the re-check filter skipped every empty
    selectinput, so "How Did You Hear About Us?" was never answered."""
    fields = [
        {"label": "Phone Code", "kind": "selectinput", "value": "Taiwan (+886)"},   # filled prompt: left alone
        {"label": "How Did You Hear About Us?", "kind": "selectinput", "value": ""},
        {"label": "Resume", "kind": "file", "value": "cv.pdf"},
        {"label": "Sponsorship?", "kind": "listbox", "value": "No"},                # filled choice: re-checked
    ]
    tried = set()
    order = []
    while (f := wd.next_field(fields, tried)) is not None:
        tried.add((f["label"], f["kind"]))
        order.append(f["label"])
    assert order == ["How Did You Hear About Us?", "Sponsorship?"]


def test_education_years_and_field_of_study():
    """Trend Micro, live 2026-11-24: year-only From/To and a searchable Field of Study."""
    p = build_profile(first_name="A", last_name="B", email="a@b.c", discipline="Physics",
                      extra_answers="first year attended: 2011\nlast year attended: 2013")
    assert wd.plan_field(_sec("formField-firstYearAttended", kind="date"), p) == {"action": "date", "value": "2011"}
    assert wd.plan_field(_sec("formField-lastYearAttended", kind="date"), p) == {"action": "date", "value": "2013"}
    assert wd.experience_plan(_sec("formField-fieldOfStudy", kind="selectinput"), p) == \
        {"action": "search", "value": "Physics"}
    bare = build_profile(first_name="A", last_name="B", email="a@b.c")
    assert wd.plan_field(_sec("formField-firstYearAttended", kind="date"), bare) is None
    assert wd.experience_plan(_sec("formField-fieldOfStudy", kind="selectinput"), bare) is None


def test_language_level_takes_the_first_answer_the_scale_offers():
    p = build_profile(first_name="A", last_name="B", email="a@b.c", languages="English",
                      extra_answers="english proficiency: 4 - Fluent\nenglish proficiency: 3 - Advanced")
    hp = ["1 - Beginner", "2 - Intermediate", "3 - Advanced", "4 - Fluent"]
    trend = ["1 - Beginner", "2 - Intermediate", "3 - Advanced", "4 - Native"]
    assert wd.plan_field(_sec("formField-x", label="Overall", kind="listbox", options=hp), p)["value"] == "4 - Fluent"
    assert wd.plan_field(_sec("formField-x", label="Overall", kind="listbox", options=trend), p)["value"] == \
        "3 - Advanced"


def test_a_never_worked_here_box_is_ticked_for_the_hiring_company():
    """Adobe, live 2026-11-24."""
    from src.automation.tools.ats_common import with_job_location
    boxes = ["Employee", "Intern", "Temporary Agency or Vendor", "Other", "I have not worked for Adobe in the past."]
    q = "Have you ever worked at Adobe in the following capacity:"
    p = with_job_location(PROFILE, None, "adobe")
    assert wd.plan_field(_f(q, kind="checkbox", options=boxes), p) == \
        {"action": "ticks", "value": ["I have not worked for Adobe in the past."]}
    other = with_job_location(PROFILE, None, "acme")
    assert wd.plan_field(_f(q, kind="checkbox", options=boxes), other) is None   # another company: never guessed


def test_a_required_confirm_the_statement_box_is_ticked():
    """Adobe, live 2026-11-24: the box under its Terms & Conditions."""
    f = _f("Check this box to confirm the statement above.", kind="checkbox", options=[""])
    assert wd.plan_field(f, PROFILE) == {"action": "tick", "value": "checked"}
    assert wd.plan_field({**f, "required": False}, PROFILE) is None


def test_job_country_from_the_url_when_the_location_is_a_count():
    """PayPal, live 2026-11-24: "2 Locations" left sponsorship unanswerable."""
    url = ("https://paypal.wd1.myworkdayjobs.com/jobs/job/San-Jose-California-United-States-of-America/"
           "Sr-Staff-Software-Engineer_R0135015-1")
    assert wd.job_country("2 Locations", url) == "United States"
    assert wd.job_country("2 Locations") == "2 Locations"           # no URL: still unknown
    assert wd.job_country("US-Minnesota-Maplewood", url) == "United States"


def test_an_acknowledgement_date_is_today():
    """PayPal, live 2026-11-24."""
    from datetime import datetime
    q = ("I understand that a positive declaration does not mean … Please enter today's date on your "
         "acknowledgement.")
    assert wd.plan_field(_sec("formField-46c8", label=q, kind="date"), PROFILE) == \
        {"action": "date", "value": datetime.now().strftime("%m/%d/%Y")}


def test_a_job_link_falls_back_to_the_requisition_id_without_its_suffix():
    """Capital One, live 2026-11-24: "R1004419-1" finds nothing, "R1004419" finds it."""
    class _Seq:
        def __init__(self):
            self.terms = []

        def post(self, url, json):
            self.terms.append(json["searchText"])
            postings = [] if json["searchText"].endswith("-1") else [
                {"title": "Full Stack Engineer 4", "externalPath": "/job/New-York-NY/Full-Stack-Engineer-4_R1004419-1",
                 "locationsText": "New York, NY"}]
            return type("R", (), {"status_code": 200, "raise_for_status": lambda self: None,
                                  "json": lambda self: {"total": len(postings), "jobPostings": postings}})()
    from src.automation.tools.ats_router import parse_board_ref
    c = _Seq()
    ref = parse_board_ref("https://capitalone.wd12.myworkdayjobs.com/Capital_One/job/New-York-NY/"
                          "Full-Stack-Engineer-4_R1004419-1")
    jobs = wd.WorkdayAdapter(sleep=lambda _s: None).discover(c, ref)
    assert c.terms == ["R1004419-1", "R1004419"]
    assert [j["job_id"] for j in jobs] == ["Full-Stack-Engineer-4_R1004419-1"]
