"""Discovery sources (Phase 2): HN "Who is hiring?", Remote OK, Remotive,
We Work Remotely — and how their jobs flow into run_ats_apply.

Fixtures are trimmed REAL responses (tests/fixtures/ats/), with every e-mail
address rewritten to @example.com. No network.
"""
import json
from pathlib import Path

import pytest

from src.automation.tools import ats_apply_tool as ats
from src.automation.tools import ats_sources as src
from src.automation.tools.ats_router import BoardRef

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "ats"


class _Resp:
    def __init__(self, data=None, text="", status=200):
        self._data, self.text, self.status_code = data, text, status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _load(name):
    text = (FIX / name).read_text(encoding="utf-8")
    return json.loads(text) if name.endswith(".json") else text


class _Client:
    """Serves the captured source responses; records requests."""
    def __init__(self, overrides=None):
        self.calls = []
        self.overrides = overrides or {}

    def get(self, url, params=None):
        self.calls.append((url, params))
        for key, resp in self.overrides.items():
            if key in url:
                return resp
        if "search_by_date" in url:
            return _Resp(_load("hn_search.json"))
        if "/items/" in url:
            return _Resp(_load("hn_thread.json"))
        if "remoteok.com" in url:
            return _Resp(_load("remoteok.json"))
        if "remotive.com" in url:
            return _Resp(_load("remotive.json"))
        if "weworkremotely.com" in url:
            return _Resp(text=_load("weworkremotely.rss"))
        return _Resp({}, status=404)

    def post(self, url, json=None):
        return _Resp({}, status=404)

    def close(self):
        pass


# ── parsing helpers ──────────────────────────────────────────────────────────

def test_emails_handle_obfuscation_and_sentence_ends():
    text = ("mail jobs [at] acme [dot] com or hr@acme.io.Apply now; ceo at acme dot co; "
            "x@mail.acme.co.uk, logo@2x.png")
    assert src._emails(text) == ["jobs@acme.com", "hr@acme.io", "ceo@acme.co", "x@mail.acme.co.uk"]


def test_emails_keep_valid_upper_case_domains():
    assert src._emails("Write to JOBS@ACME.CO.UK today") == ["jobs@acme.co.uk"]
    assert src._emails("hr@Mail.Acme.Com") == ["hr@mail.acme.com"]
    assert src._emails("hr@acme.io.Apply now") == ["hr@acme.io"]


def test_emails_do_not_join_across_paragraphs():
    assert src._emails(src._text("Email jobs@acme.com.<p>Application tips below")) == ["jobs@acme.com"]


def test_route_keeps_specific_jobs_over_the_bare_board():
    refs, other = src._route([
        "https://jobs.lever.co/acme/11111111-1111-1111-1111-111111111111",
        "https://jobs.lever.co/acme",
        "https://jobs.ashbyhq.com/beta",
        "https://acme.com/careers",
        "https://acme.wd1.myworkdayjobs.com/x",     # Workday: routed (listed for manual apply)
        "https://www.icims.com/jobs/1",             # unsupported → kept as a manual link
    ])
    assert refs == [BoardRef("lever", "acme", "11111111-1111-1111-1111-111111111111"),
                    BoardRef("ashby", "beta"), BoardRef("workday", "acme.wd1/x")]
    assert other == ["https://acme.com/careers", "https://www.icims.com/jobs/1"]


def test_careers_link_prefers_a_jobs_page():
    links = ["https://arxiv.org/abs/1", "https://acme.com/careers/eng", "https://news.ycombinator.com/x"]
    assert src._careers_link(links) == "https://acme.com/careers/eng"
    assert src._careers_link(["https://acme.com"]) == "https://acme.com"
    assert src._careers_link([]) == ""


def test_parse_hn_post_reads_the_pipe_header():
    p = src.parse_hn_post({"id": 7, "text": "Acme | Backend Engineer | REMOTE (EU)<p>We build "
                                            "<a href=\"https://jobs.lever.co/acme\">things</a>."})
    assert p["company"] == "Acme" and p["header"].startswith("Acme | Backend Engineer")
    assert p["links"] == ["https://jobs.lever.co/acme"]


# ── HN "Who is hiring?" on the captured thread ───────────────────────────────

def test_hn_finds_latest_thread_and_routes_ats_links():
    client = _Client()
    out = src.hn_source(client, [], [], log=lambda _m: None)
    assert client.calls[0][1]["tags"] == "story,author_whoishiring"
    assert any(r.platform == "ashby" for r in out["refs"])
    assert any(r.platform == "lever" and r.job_id for r in out["refs"])
    # A post that links specific Lever jobs does not also add the whole board.
    lever = [r for r in out["refs"] if r.platform == "lever"]
    assert all(r.job_id for r in lever)


def test_hn_manual_leads_link_back_and_skip_retracted_posts():
    out = src.hn_source(_Client(), [], [], log=lambda _m: None)
    assert out["manual"], "careers-page / e-mail posts become manual leads"
    for m in out["manual"]:
        assert m["source"] == "hn" and m["source_url"].startswith("https://news.ycombinator.com/item?id=")
        assert m["url"] or m["email"]
        assert "@" not in m["email"] or m["email"].endswith("example.com") or ", " in m["email"]
    # "UPDATE (Nov 13): We're no longer taking applications" is not a lead.
    assert not any("no longer" in m["title"].lower() for m in out["manual"])


def test_hn_keyword_and_location_filters_apply_to_the_post():
    everything = src.hn_source(_Client(), [], [], log=lambda _m: None)
    narrowed = src.hn_source(_Client(), ["engineer"], ["remote"], log=lambda _m: None)
    assert len(narrowed["refs"]) + len(narrowed["manual"]) < len(everything["refs"]) + len(everything["manual"])
    none = src.hn_source(_Client(), ["zzz-no-such-role"], [], log=lambda _m: None)
    assert none["refs"] == [] and none["manual"] == []


def test_hn_without_a_thread_raises():
    client = _Client({"search_by_date": _Resp({"hits": [{"objectID": "1", "title": "Ask HN: Who wants to be hired?"}]})})
    with pytest.raises(RuntimeError, match="Who is hiring"):
        src.hn_source(client, [], [])


# ── remote boards ────────────────────────────────────────────────────────────

def test_remoteok_lists_listings_and_ignores_a_policy_footer_link():
    """The fixture's only ATS link is Penumbra's "AAP Policy Statement" footer — a
    *different* Lever posting. It must not become the Clinical Data Manager
    listing's application target; the listing stays a manual lead."""
    out = src.remoteok_source(_Client(), [], [], log=lambda _m: None)
    assert out["refs"] == []
    titles = [m["title"] for m in out["manual"]]
    assert "Clinical Data Manager" in titles
    assert all(m["url"].lower().startswith("https://remoteok.com/") for m in out["manual"])
    assert all("Remote OK" in m["reason"] for m in out["manual"])          # credited, as its terms ask


@pytest.mark.parametrize("html_text,expected", [
    ("<p>Apply: <a href='https://jobs.ashbyhq.com/acme/11111111-1111-1111-1111-111111111111'>here</a></p>",
     ["https://jobs.ashbyhq.com/acme/11111111-1111-1111-1111-111111111111"]),          # single quotes
    ('Apply <a href="https://jobs.lever.co/acme">at Lever</a>', ["https://jobs.lever.co/acme"]),
    ('please see <a href="https://jobs.lever.co/acme/x">Our AAP Policy Statement</a>', []),
    ('We are an equal opportunity employer, see <a href="https://acme.com/eeo">this</a>', []),
    ("Privacy notice: https://boards.greenhouse.io/acme/jobs/1", []),
    ("Apply here: https://boards.greenhouse.io/acme/jobs/1", ["https://boards.greenhouse.io/acme/jobs/1"]),
])
def test_links_quote_styles_and_boilerplate(html_text, expected):
    assert src._links(html_text) == expected


def test_board_listing_routes_only_a_link_presented_as_how_to_apply():
    promo = {"title": "Staff DevOps Engineer", "company": "Bloomreach", "location": "Remote",
             "url": "https://weworkremotely.com/remote-jobs/x",
             "html": "Not a fit? take a look at the Senior DevOps position - Czechia: "
                     "https://job-boards.greenhouse.io/bloomreach/jobs/8329244"}
    apply = {**promo, "url": "https://weworkremotely.com/remote-jobs/y",
             "html": "To apply: <a href='https://job-boards.greenhouse.io/bloomreach/jobs/1'>here</a>"}
    out = src._board_jobs("weworkremotely", "We Work Remotely", [promo, apply], [], [], lambda _m: None)
    assert out["refs"] == [BoardRef("greenhouse", "bloomreach", "1")]
    assert [m["url"] for m in out["manual"]] == ["https://weworkremotely.com/remote-jobs/x"]


def test_remotive_searches_every_keyword_alternative():
    client = _Client()
    out = src.remotive_source(client, ["python", "designer"], [], log=lambda _m: None)
    assert [c[1] for c in client.calls] == [{"search": "python"}, {"search": "designer"}]
    urls = [m["url"] for m in out["manual"]]
    assert len(urls) == len(set(urls))                                     # merged, deduped
    many = _Client()
    out = src.remotive_source(many, [f"k{i}" for i in range(7)], [], log=lambda _m: None)
    assert len(many.calls) == 5 and any("first 5 keywords" in w for w in out["warnings"])
    client = _Client()
    out = src.remotive_source(client, ["engineer"], [], log=lambda _m: None)
    assert client.calls[0][1] == {"search": "engineer"}
    # The captured Lemon.io link has no "apply" context: kept as a manual lead.
    out_all = src.remotive_source(_Client(), [], [], log=lambda _m: None)
    assert out_all["refs"] == [] and any("Lemon" in m["company"] for m in out_all["manual"])
    assert all(m["source"] == "remotive" for m in out["manual"])


def test_weworkremotely_parses_rss_and_survives_a_dead_feed():
    jobs = src.parse_wwr_feed(_load("weworkremotely.rss"))
    assert jobs and all(j["url"].startswith("https://weworkremotely.com/") for j in jobs)
    assert jobs[0]["company"] and jobs[0]["title"]                        # "Company: Role" split
    dead = _Client({"remote-devops-sysadmin-jobs.rss": _Resp(status=503)})
    out = src.weworkremotely_source(dead, [], [], log=lambda _m: None)
    assert any("unavailable" in w for w in out["warnings"])
    assert len(out["manual"]) == len(jobs)                                 # same items in every feed: deduped


def test_collect_merges_dedupes_and_reports_unknown_or_failing_sources():
    failing = _Client({"remotive.com": _Resp(status=500)})
    out = src.collect(failing, ["HN", "remote ok", "remotive", "wwr", "monster"], [], [],
                      log=lambda _m: None)
    assert any("unknown source 'monster'" in w for w in out["warnings"])
    assert any(w.startswith("remotive: source unavailable") for w in out["warnings"])
    assert len(out["refs"]) == len(set(out["refs"]))
    keys = [m.get("url") or m.get("email") for m in out["manual"]]
    assert len(keys) == len(set(keys)) and all(m["status"] == "manual" for m in out["manual"])


def test_resolve_sources_aliases():
    assert src.resolve_sources(["Hacker News", "wwr", "remote-ok", "x"]) == (
        ["hn", "weworkremotely", "remoteok"], ["x"])


# ── into run_ats_apply ───────────────────────────────────────────────────────

EXPLICIT = "11111111-1111-1111-1111-111111111111"
BOARDS = {  # like the real adapters: discover() always lists the whole board
    "acme": [("job-1", "Backend Engineer"), ("job-2", "Backend Engineer"), (EXPLICIT, "Backend Engineer")],
    "beta": [("a", "Backend Engineer"), ("b", "Sales Lead")],
}


class _Adapter(ats.Adapter):
    """Lists a fixed board; records discoveries and applications."""
    def __init__(self, platform):
        self.platform, self.label, self.applied, self.discovered = platform, platform, [], []

    def discover(self, client, ref):
        self.discovered.append(ref.company)
        return [{"platform": self.platform, "token": ref.company, "job_id": i, "title": t,
                 "company": ref.company, "location": "Remote", "url": "u", "apply_url": "a"}
                for i, t in BOARDS[ref.company]]

    def load_form(self, client, job):
        return {}

    def plan(self, form, job, profile, answer_fn):
        return [], []

    def apply(self, page, job, answers, dry_run, log):
        self.applied.append((job["token"], job["job_id"]))
        return {**ats.entry_for(job, answers), "status": "prepared", "submitted": False}


@pytest.fixture
def run(tmp_path, mocker):
    resume = tmp_path / "cv.pdf"
    resume.write_bytes(b"%PDF")
    mocker.patch.object(ats, "launch_browser")
    mocker.patch("playwright.sync_api.sync_playwright")
    mocker.patch.object(src, "SOURCES", {
        "hn": lambda client, kw, loc, log: {
            "refs": [BoardRef("lever", "acme", "job-1"), BoardRef("lever", "acme", "job-2"),
                     BoardRef("ashby", "beta")],
            "manual": [{"source": "hn", "company": "Gamma", "title": "Gamma | Eng", "url": "https://gamma.io/jobs",
                        "email": "", "source_url": "https://news.ycombinator.com/item?id=1", "reason": "r"}],
            "warnings": []},
        "remoteok": lambda client, kw, loc, log: {"refs": [], "manual": [], "warnings": ["remoteok: x"]},
    })
    adapters = {"lever": _Adapter("lever"), "ashby": _Adapter("ashby")}

    def _run(**kw):
        args = dict(first_name="A", last_name="B", email="a@b.c", resume_path=str(resume),
                    ledger_path=str(tmp_path / "l.json"), client=_Client(), adapters=adapters,
                    max_applications=10)
        return ats.run_ats_apply(**{**args, **kw})
    _run.adapters = adapters
    return _run


def test_sources_feed_the_adapters_and_return_manual_leads(run):
    out = run(sources="hn, remoteok", keywords="engineer")
    lever, ashby = run.adapters["lever"], run.adapters["ashby"]
    assert sorted(lever.applied) == [("acme", "job-1"), ("acme", "job-2")]
    assert lever.discovered == ["acme"]                       # one listing for two job links
    assert ashby.applied == [("beta", "a")]                   # board ref, "Sales Lead" filtered out
    assert [m["company"] for m in out["manual"]] == ["Gamma"]
    assert out["manual_count"] == 1 and "remoteok: x" in out["warnings"]
    assert "found via hn, remoteok" in out["summary"]


def test_sourced_job_links_are_still_filtered(run):
    run(sources="hn", keywords="sales")
    assert run.adapters["lever"].applied == []                # job-1/2 are "Backend Engineer"
    assert run.adapters["ashby"].applied == [("beta", "b")]


def test_pasted_job_links_still_bypass_filters(run):
    run(boards=f"lever:acme/{EXPLICIT}", keywords="sales")      # a "Backend Engineer" job
    assert run.adapters["lever"].applied == [("acme", EXPLICIT)]


def test_jobs_interleave_by_board_not_by_link(run, mocker):
    """Three HN links into one board and one into another: with a cap of 2 the
    second board still gets a turn."""
    mocker.patch.object(src, "SOURCES", {"hn": lambda c, kw, loc, log: {
        "refs": [BoardRef("lever", "acme", "job-1"), BoardRef("lever", "acme", "job-2"),
                 BoardRef("lever", "acme", EXPLICIT), BoardRef("ashby", "beta")],
        "manual": [], "warnings": []}})
    run(sources="hn", keywords="engineer", max_applications=2)
    assert run.adapters["lever"].applied == [("acme", "job-1")]
    assert run.adapters["ashby"].applied == [("beta", "a")]


def test_case_sensitive_board_tokens_are_listed_separately(run, mocker):
    calls = []

    def discover(client, ref):
        calls.append(ref.company)
        if ref.company == "beta":
            raise RuntimeError("no such board")
        return [{"platform": "ashby", "token": ref.company, "job_id": "a", "title": "Backend Engineer",
                 "company": ref.company, "location": "Remote", "url": "u", "apply_url": "a"}]
    mocker.patch.object(run.adapters["ashby"], "discover", side_effect=discover)
    out = run(boards="ashby:beta, ashby:Beta", keywords="engineer")
    assert calls == ["beta", "Beta"] and out["jobs_found"] == 1


def test_board_and_source_refs_are_deduped(run):
    run(boards="https://jobs.ashbyhq.com/beta", sources="hn", keywords="engineer")
    assert run.adapters["ashby"].discovered == ["beta"]


def test_sources_only_with_no_ats_jobs_still_returns_leads_without_a_resume(run, mocker):
    mocker.patch.object(src, "SOURCES", {"hn": lambda c, kw, loc, log: {
        "refs": [], "manual": [{"source": "hn", "company": "G", "title": "t", "url": "https://g.io/jobs",
                                "email": "", "source_url": "s", "reason": "r"}], "warnings": []}})
    out = run(sources="hn", resume_path="/nope.pdf")
    assert "error" not in out and out["manual_count"] == 1 and out["jobs_found"] == 0
    assert "1 lead(s) listed for manual apply" in out["summary"]


def test_needs_boards_or_sources(run):
    out = run(boards="", sources="")
    assert "No usable boards or sources" in out["error"]


def test_flow_accepts_sources_without_boards(mocker):
    from src.automation.flows.ats_apply_flow import ATSApplyFlow
    runner = mocker.patch("src.automation.flows.ats_apply_flow.run_ats_apply",
                          return_value={"applied": [], "jobs_found": 0})
    mocker.patch("src.automation.flows.ats_apply_flow.append_log")
    ATSApplyFlow().kickoff(inputs={"sources": "hn", "first_name": "a", "last_name": "b", "email": "c"})
    assert runner.call_args.kwargs["sources"] == "hn" and runner.call_args.kwargs["boards"] == ""
