"""Eval regression set — golden cases per automation, scored offline and live.

A *case* (``evals/<job_type>/<id>.yaml``) pins one input for one automation:
its ``payload``, canned ``fixtures`` standing in for every tool/scrape the run
would otherwise do against a live site, ``expect``-ations, and a
``reference_output`` (a known-good result). Two tiers read the same files:

* **offline** (:func:`offline_problems`, PR CI, no API keys) — every case file
  matches the schema, every registered automation has a case or a justified
  entry in ``evals/skip.yaml``, and each reference output passes the
  deterministic validator plus its own expectations.
* **live** (:func:`run_live`, nightly) — the automation's real LLM step runs
  against the fixtures, the existing judge (``evaluator.evaluate``) scores it,
  and the score is compared with the committed ``evals/baseline.json``.

The judge is never re-implemented here: independence from the run model is
``evaluator``'s invariant. A result the judge could not score independently
(heuristic fallback or self-graded) is reported as *unscored* — it is neither
compared with nor written to the baseline. See doc/evals.md.
"""
from __future__ import annotations

import contextlib
import importlib
import json
import re
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from src.automation import spec
from src.automation.harness.validator import validate

EVALS_ROOT = Path(__file__).resolve().parents[3] / "evals"
SKIP_FILE = "skip.yaml"
BASELINE_FILE = "baseline.json"
CUSTOM_DIR = "custom"          # directory for `custom:<slug>` cases
CUSTOM_PREFIX = "custom:"
DEFAULT_TOLERANCE = 10.0       # judge points a case may drop below its baseline
_MIN_SKIP_REASON = 20          # a skip needs an actual justification, not "n/a"
_RUN_ATTEMPTS = 3              # live: tries per case on a transient provider error
_RETRY_BACKOFF_SECS = 5.0

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_CASE_KEYS = {"id", "job_type", "description", "payload", "fixtures", "expect", "reference_output"}
_EXPECT_KEYS = {"valid", "contains", "not_contains", "assertions", "min_judge_score"}
_ASSERT_OPS = {"type", "equals", "one_of", "min_len", "max_len", "non_empty", "min", "max"}
_TYPES: dict[str, type | tuple[type, ...]] = {
    "str": str, "int": int, "number": (int, float), "bool": bool, "list": list, "dict": dict,
}


@dataclass
class Case:
    id: str
    job_type: str
    path: Path
    payload: dict
    expect: dict
    reference_output: dict
    fixtures: dict = field(default_factory=dict)
    description: str = ""

    @property
    def key(self) -> str:
        return f"{self.job_type}/{self.id}"


# ── Loading + schema ─────────────────────────────────────────────────────────

def _dir_for(job_type: str) -> str:
    return CUSTOM_DIR if job_type.startswith(CUSTOM_PREFIX) else job_type


def _schema_problems(raw: object, path: Path) -> list[str]:
    """Problems with one parsed case file (empty == well-formed)."""
    if not isinstance(raw, dict):
        return ["case file must be a mapping"]
    out: list[str] = []
    unknown = set(raw) - _CASE_KEYS
    if unknown:
        out.append(f"unknown key(s): {sorted(unknown)}")

    case_id, job_type = raw.get("id"), raw.get("job_type")
    if not isinstance(case_id, str) or not _ID_RE.match(case_id):
        out.append("'id' must be a lowercase slug ([a-z0-9_-])")
    elif case_id != path.stem:
        out.append(f"'id' ({case_id!r}) must equal the file name ({path.stem!r})")
    if not isinstance(job_type, str) or not job_type:
        out.append("'job_type' must be a non-empty string")
    elif _dir_for(job_type) != path.parent.name:
        out.append(f"job_type {job_type!r} belongs in evals/{_dir_for(job_type)}/, "
                   f"not evals/{path.parent.name}/")

    for key in ("payload", "expect", "reference_output"):
        if not isinstance(raw.get(key), dict):
            out.append(f"'{key}' is required and must be a mapping")
    if not isinstance(raw.get("fixtures", {}), dict):
        out.append("'fixtures' must be a mapping")
    if not isinstance(raw.get("description", ""), str):
        out.append("'description' must be a string")

    expect = raw.get("expect")
    if isinstance(expect, dict):
        out += _expect_problems(expect)
    return out


def _expect_problems(expect: dict) -> list[str]:
    out: list[str] = []
    unknown = set(expect) - _EXPECT_KEYS
    if unknown:
        out.append(f"expect: unknown key(s): {sorted(unknown)}")
    if not isinstance(expect.get("valid", True), bool):
        out.append("expect.valid must be a boolean")
    for key in ("contains", "not_contains"):
        items = expect.get(key, [])
        if not isinstance(items, list) or not all(isinstance(i, str) and i for i in items):
            out.append(f"expect.{key} must be a list of non-empty strings")
    score = expect.get("min_judge_score")
    if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float))
                              or not 0 <= score <= 100):
        out.append("expect.min_judge_score must be a number in 0-100")

    assertions = expect.get("assertions", [])
    if not isinstance(assertions, list):
        return [*out, "expect.assertions must be a list"]
    for n, a in enumerate(assertions):
        where = f"expect.assertions[{n}]"
        if not isinstance(a, dict) or not isinstance(a.get("path"), str) or not a["path"]:
            out.append(f"{where}: needs a string 'path'")
            continue
        ops = set(a) - {"path"}
        if not ops:
            out.append(f"{where}: needs at least one check ({sorted(_ASSERT_OPS)})")
        if ops - _ASSERT_OPS:
            out.append(f"{where}: unknown check(s) {sorted(ops - _ASSERT_OPS)}")
        if "type" in a and a["type"] not in _TYPES:
            out.append(f"{where}: type must be one of {sorted(_TYPES)}")
        if "one_of" in a and not isinstance(a["one_of"], list):
            out.append(f"{where}: one_of must be a list")
    if not any(expect.get(k) for k in ("contains", "assertions")):
        out.append("expect: needs at least one of 'contains' / 'assertions'")
    return out


def load_cases(root: Path | None = None) -> tuple[list[Case], list[str]]:
    """Parse every case under ``root``. Returns (well-formed cases, problems)."""
    root = root or EVALS_ROOT
    cases: list[Case] = []
    problems: list[str] = []
    seen: set[str] = set()
    if not root.is_dir():
        return [], [f"evals directory not found: {root}"]
    for path in sorted(root.glob("*/*")):
        if path.suffix not in (".yaml", ".yml", ".json"):
            continue
        rel = path.relative_to(root)
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))  # YAML ⊇ JSON
        except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
            problems.append(f"{rel}: cannot parse ({str(exc)[:160]})")
            continue
        errs = _schema_problems(raw, path)
        if errs:
            problems += [f"{rel}: {e}" for e in errs]
            continue
        case = Case(
            id=raw["id"], job_type=raw["job_type"], path=path, payload=raw["payload"],
            expect=raw["expect"], reference_output=raw["reference_output"],
            fixtures=raw.get("fixtures") or {}, description=raw.get("description", ""),
        )
        if case.key in seen:
            problems.append(f"{rel}: duplicate case {case.key}")
            continue
        seen.add(case.key)
        cases.append(case)
    return cases, problems


def load_skips(root: Path | None = None) -> tuple[dict[str, str], list[str]]:
    """``evals/skip.yaml``: job_type → why it has no eval cases."""
    path = (root or EVALS_ROOT) / SKIP_FILE
    if not path.is_file():
        return {}, []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return {}, [f"{SKIP_FILE}: cannot parse ({str(exc)[:160]})"]
    if not isinstance(raw, dict):
        return {}, [f"{SKIP_FILE}: must be a mapping of job_type → reason"]
    problems = [
        f"{SKIP_FILE}: {jt!r} needs a real reason (>= {_MIN_SKIP_REASON} chars)"
        for jt, reason in raw.items()
        if not isinstance(reason, str) or len(reason.strip()) < _MIN_SKIP_REASON
    ]
    return {str(k): str(v) for k, v in raw.items()}, problems


def coverage_problems(cases: list[Case], skips: dict[str, str],
                      job_types: list[str] | None = None) -> list[str]:
    """Every registered automation is either covered by a case or skipped with a
    reason — never both, never neither — so a new automation can't ship unseen."""
    registered = list(spec.REGISTRY) if job_types is None else job_types
    covered = {c.job_type for c in cases}
    out: list[str] = []
    for jt in registered:
        if jt in covered and jt in skips:
            out.append(f"{jt}: has eval cases but is also in {SKIP_FILE} — remove the skip")
        elif jt not in covered and jt not in skips:
            out.append(f"{jt}: no eval case under evals/{jt}/ and no entry in {SKIP_FILE} "
                       "(add a case, or a skip with the reason — see doc/evals.md)")
    out += [f"{SKIP_FILE}: {jt!r} is not a registered automation (stale entry?)"
            for jt in skips if jt not in registered]
    out += [f"{c.key}: job_type is not a registered automation"
            for c in cases if c.job_type not in registered and not c.job_type.startswith(CUSTOM_PREFIX)]
    out += [f"{c.key}: no eval adapter for this job_type (add one to evalset._ADAPTERS)"
            for c in cases if _adapter(c.job_type) is None]
    return out


# ── Expectations ─────────────────────────────────────────────────────────────

_MISSING = object()
_SEG_RE = re.compile(r"^([^\[\]]*)((?:\[(?:\*|\d+)\])*)$")


def resolve_path(obj: object, path: str) -> list[object]:
    """Values at a dotted path. ``a.b[0].c`` indexes; ``a[*].c`` fans out over a
    list. A missing key/index yields ``_MISSING`` so the caller can report it."""
    values = [obj]
    for seg in path.split("."):
        m = _SEG_RE.match(seg)
        if not m:
            return [_MISSING]
        name, indexes = m.group(1), re.findall(r"\[(\*|\d+)\]", m.group(2))
        if name:
            values = [v.get(name, _MISSING) if isinstance(v, dict) else _MISSING for v in values]
        for idx in indexes:
            nxt: list[object] = []
            for v in values:
                if not isinstance(v, list):
                    nxt.append(_MISSING)
                elif idx == "*":
                    nxt.extend(v)
                else:
                    nxt.append(v[int(idx)] if int(idx) < len(v) else _MISSING)
            values = nxt
    return values


def _assertion_failures(output: dict, a: dict) -> list[str]:
    path = a["path"]
    values = resolve_path(output, path)
    if not values:
        return [f"{path}: matched nothing"]
    out: list[str] = []
    for v in values:
        if v is _MISSING:
            out.append(f"{path}: not found")
            continue
        shown = json.dumps(v, ensure_ascii=False, default=str)[:80]
        if "type" in a:
            wanted = _TYPES[a["type"]]
            # bool is an int in Python; don't let true/false pass as a number.
            if not isinstance(v, wanted) or (isinstance(v, bool) and a["type"] != "bool"):
                out.append(f"{path}: expected {a['type']}, got {shown}")
        if "equals" in a and v != a["equals"]:
            out.append(f"{path}: expected {a['equals']!r}, got {shown}")
        if "one_of" in a and v not in a["one_of"]:
            out.append(f"{path}: {shown} not in {a['one_of']}")
        if a.get("non_empty") and not (v.strip() if isinstance(v, str) else v):
            out.append(f"{path}: is empty")
        for op in ("min_len", "max_len"):
            if op in a:
                size = len(v) if hasattr(v, "__len__") else None
                if size is None or (size < a[op] if op == "min_len" else size > a[op]):
                    out.append(f"{path}: length {size} violates {op}={a[op]}")
        for op in ("min", "max"):
            if op in a:
                numeric = isinstance(v, (int, float)) and not isinstance(v, bool)
                if not numeric or (v < a[op] if op == "min" else v > a[op]):
                    out.append(f"{path}: {shown} violates {op}={a[op]}")
    return out


def check_expectations(case: Case, output: object) -> list[str]:
    """Deterministic failures of ``output`` against the case (empty == pass):
    the job's validator, must-contain facts, structural assertions, and the
    job-specific consistency check against the fixtures."""
    if not isinstance(output, dict):
        return ["output is not a JSON object"]
    expect = case.expect
    out: list[str] = []

    vr = validate(case.job_type, output)
    want_valid = expect.get("valid", True)
    if vr.valid != want_valid:
        out.append(f"validator: expected valid={want_valid}, got valid={vr.valid}"
                   + (f" ({vr.reason})" if vr.reason else ""))

    haystack = json.dumps(output, ensure_ascii=False).lower()
    out += [f"missing expected fact: {s!r}" for s in expect.get("contains", [])
            if s.lower() not in haystack]
    out += [f"contains forbidden text: {s!r}" for s in expect.get("not_contains", [])
            if s.lower() in haystack]
    for a in expect.get("assertions", []):
        out += _assertion_failures(output, a)

    adapter = _adapter(case.job_type)
    if adapter and adapter.consistency:
        out += adapter.consistency(case, output)
    return out


# ── Adapters: how each automation runs from fixtures ─────────────────────────
# One per covered job type. `fixture_problems` is checked offline; `run` executes
# the real flow/crew with every tool and network touchpoint replaced by the
# case's fixtures, so the only live dependency is the LLM under test.

@dataclass(frozen=True)
class Adapter:
    fixture_problems: Callable[[Case], list[str]]
    run: Callable[[Case, str, str], object]            # (case, provider, model) → raw output
    consistency: Callable[[Case, dict], list[str]] | None = None


def _kickoff(job_type: str, payload: dict, provider: str, model: str) -> object:
    """Run the registered flow exactly as the executor does (run_id=0 → no DB logs)."""
    s = spec.REGISTRY[job_type]
    flow_cls = getattr(importlib.import_module(s.flow_module), s.flow_class)
    raw = flow_cls().kickoff(inputs={
        **payload, "run_id": 0, "llm_provider": provider, "llm_model": model,
    })
    return raw.raw if hasattr(raw, "raw") else raw


def _tool_flow(tool_module: str, tool_class: str, tool_name: str) -> Adapter:
    """A single-crew flow whose agent calls one tool: stub that tool's ``_run``
    to return ``fixtures.tools.<tool_name>`` and run the real flow."""

    def fixture_problems(case: Case) -> list[str]:
        tools = case.fixtures.get("tools")
        if not isinstance(tools, dict) or set(tools) != {tool_name}:
            return [f"fixtures.tools must define exactly {tool_name!r} (the tool's canned return value)"]
        return []

    def run(case: Case, provider: str, model: str) -> object:
        canned = case.fixtures["tools"][tool_name]
        tool_cls = getattr(importlib.import_module(tool_module), tool_class)
        with mock.patch.object(tool_cls, "_run", lambda self, *a, **k: canned):
            return _kickoff(case.job_type, case.payload, provider, model)

    return Adapter(fixture_problems, run)


# profit_health_check — the fixture is a small synthetic upload. profit_calc is
# deterministic, so it runs for real against the fixture CSVs.
_UPLOAD_REQUIRED = ("sales.csv", "cost.csv")
_UPLOAD_ALLOWED = {"sales.csv", "cost.csv", "ads.csv", "returns.csv"}
_SKU_NUMERIC = ("revenue", "cost", "ad_spend", "refunds", "net_profit", "margin_pct", "units")
_FLAG_GROUPS = ("most_profitable", "fake_hits", "ad_eats_profit", "high_return_rate")


def _profit_fixture_problems(case: Case) -> list[str]:
    upload = case.fixtures.get("upload")
    if not isinstance(upload, dict) or not all(isinstance(v, str) for v in upload.values()):
        return ["fixtures.upload must map CSV file names to their text content"]
    out = [f"fixtures.upload is missing {name}" for name in _UPLOAD_REQUIRED if not upload.get(name)]
    if set(upload) - _UPLOAD_ALLOWED:
        out.append(f"fixtures.upload: unknown file(s) {sorted(set(upload) - _UPLOAD_ALLOWED)}")
    if not case.payload.get("upload_id"):
        out.append("payload.upload_id is required")
    return out


@contextlib.contextmanager
def _fixture_upload(case: Case) -> Iterator[None]:
    with tempfile.TemporaryDirectory(prefix="evalset-upload-") as tmp:
        dest = Path(tmp) / str(case.payload["upload_id"])
        dest.mkdir(parents=True)
        for name, content in case.fixtures["upload"].items():
            (dest / name).write_text(content, encoding="utf-8")
        with mock.patch("src.routers.uploads.UPLOAD_ROOT", Path(tmp)), \
                mock.patch("src.automation.flows.profit_health_flow.UPLOAD_ROOT", Path(tmp)), \
                mock.patch("src.automation.flows.profit_health_flow.render_report_pdf"):
            yield


def _profit_run(case: Case, provider: str, model: str) -> object:
    with _fixture_upload(case):
        return _kickoff(case.job_type, case.payload, provider, model)


def _profit_consistency(case: Case, output: dict) -> list[str]:
    """The report's numbers must be profit_calc's numbers for the fixture CSVs —
    the automation's core contract ("all arithmetic is deterministic")."""
    from src.automation.tools.profit_calc_tool import compute_profit

    upload = case.fixtures.get("upload") or {}
    truth = compute_profit(upload.get("sales.csv", ""), upload.get("cost.csv", ""),
                           upload.get("ads.csv", ""), upload.get("returns.csv", ""))
    rows = output.get("skus")
    by_sku = {r.get("sku"): r for r in rows if isinstance(r, dict)} if isinstance(rows, list) else {}
    out: list[str] = []
    for m in truth.skus:
        row = by_sku.get(m.sku)
        if row is None:
            out.append(f"profit_calc: SKU {m.sku} missing from report")
            continue
        for f in _SKU_NUMERIC:
            want, got = getattr(m, f), row.get(f)
            if isinstance(got, bool) or not isinstance(got, (int, float)) or abs(got - want) > 0.011:
                out.append(f"profit_calc: {m.sku}.{f} is {got!r}, tool computed {want}")
    flags = output.get("flags") if isinstance(output.get("flags"), dict) else {}
    for group in _FLAG_GROUPS:
        want_set = set(getattr(truth.flags, group))
        got_list = flags.get(group)
        if not isinstance(got_list, list) or set(got_list) != want_set:
            out.append(f"profit_calc: flags.{group} is {got_list!r}, tool computed {sorted(want_set)}")
    return out


# email_collect — discovery/extraction/verification are deterministic scrapers;
# the prompt-sensitive part is the qualify step. Feed the funnel canned
# businesses (each already carrying its `emails`) and run the real flow.
def _email_fixture_problems(case: Case) -> list[str]:
    businesses = case.fixtures.get("businesses")
    ok = isinstance(businesses, list) and businesses and all(
        isinstance(b, dict) and b.get("name") and isinstance(b.get("emails"), list) and b["emails"]
        for b in businesses
    )
    return [] if ok else ["fixtures.businesses must be a non-empty list of "
                          "{name, website, category, emails: [...]}"]


@contextlib.contextmanager
def _fixture_funnel(case: Case) -> Iterator[None]:
    businesses = json.loads(json.dumps(case.fixtures["businesses"]))  # the flow mutates rows
    flow_mod = "src.automation.flows.email_collect_flow"
    verified = {"confidence": "medium", "mx_found": True, "smtp_status": "skipped"}
    with mock.patch(f"{flow_mod}.EmailCollectFlow._discover",
                    lambda self, query, region: (businesses, [])), \
            mock.patch(f"{flow_mod}.extract_emails", lambda *a, **k: {"emails": []}), \
            mock.patch(f"{flow_mod}.verify_email", lambda email, **k: {**verified, "email": email}):
        yield


def _email_run(case: Case, provider: str, model: str) -> object:
    with _fixture_funnel(case):
        return _kickoff(case.job_type, case.payload, provider, model)


# custom:<slug> — the definition normally lives in the DB; a case carries it in
# `fixtures.custom` and runs the same no-tools DynamicCrew as executor._run_custom.
def _custom_fixture_problems(case: Case) -> list[str]:
    d = case.fixtures.get("custom")
    if not isinstance(d, dict) or not d.get("name") or not d.get("instructions"):
        return ["fixtures.custom must define the automation: {name, instructions, "
                "output_hint?, temperature?}"]
    return []


def _custom_run(case: Case, provider: str, model: str) -> object:
    from src.automation.crews.dynamic_crew import DynamicCrew
    from src.automation.harness.provider import resolve

    d = case.fixtures["custom"]
    definition = SimpleNamespace(name=d["name"], instructions=d["instructions"],
                                 output_hint=d.get("output_hint", ""))
    llm, _, _ = resolve(provider, model, temperature=float(d.get("temperature", 0.3)))
    raw = DynamicCrew(definition, case.payload, llm=llm).crew().kickoff()
    return raw.raw if hasattr(raw, "raw") else raw


_ADAPTERS: dict[str, Adapter] = {
    "hacker_news_digest": _tool_flow("src.automation.tools.hn_tool", "HNTopStoriesTool", "hn_top_stories"),
    "web_scraper": _tool_flow("src.automation.tools.web_scraper_tool", "WebScraperTool", "web_scraper"),
    "google_sheet_reader": _tool_flow("src.automation.tools.google_sheet_tool", "GoogleSheetTool",
                                      "google_sheet_reader"),
    "profit_health_check": Adapter(_profit_fixture_problems, _profit_run, _profit_consistency),
    "email_collect": Adapter(_email_fixture_problems, _email_run),
}
_CUSTOM_ADAPTER = Adapter(_custom_fixture_problems, _custom_run)


def _adapter(job_type: str) -> Adapter | None:
    return _CUSTOM_ADAPTER if job_type.startswith(CUSTOM_PREFIX) else _ADAPTERS.get(job_type)


# ── Offline tier ─────────────────────────────────────────────────────────────

def offline_problems(root: Path | None = None) -> list[str]:
    """Everything the offline tier checks. Empty list == green."""
    cases, problems = load_cases(root)
    skips, skip_problems = load_skips(root)
    problems += skip_problems
    problems += coverage_problems(cases, skips)
    for case in cases:
        adapter = _adapter(case.job_type)
        if adapter is None:
            continue  # already reported by coverage_problems
        fixture_errs = adapter.fixture_problems(case)
        problems += [f"{case.key}: {e}" for e in fixture_errs]
        if fixture_errs:
            continue  # the consistency check reads the fixtures
        problems += [f"{case.key}: reference_output: {e}"
                     for e in check_expectations(case, case.reference_output)]
    return problems


# ── Live tier ────────────────────────────────────────────────────────────────

@dataclass
class CaseResult:
    key: str
    status: str = "pass"            # pass | fail | error | unscored
    score: float | None = None
    confidence: float | None = None
    judge: str = ""
    judge_notes: str = ""
    baseline: float | None = None
    delta: float | None = None
    comparison: str = ""            # ok | regression | new | skipped: <why>
    failures: list[str] = field(default_factory=list)
    duration_secs: float = 0.0
    output: object = None


def _label(provider: str, model: str) -> str:
    """Same display form evaluator uses for ``EvalResult.judge_model``."""
    return model if "/" in model else f"{provider}/{model}"


def load_baseline(root: Path | None = None) -> dict:
    path = (root or EVALS_ROOT) / BASELINE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    return data if isinstance(data, dict) and isinstance(data.get("scores"), dict) else {"scores": {}}


def compare_to_baseline(result: CaseResult, baseline: dict, run_model: str, tolerance: float) -> None:
    """Fill ``result.comparison`` (and fail it on a regression). An absent or
    non-comparable baseline entry is never a failure — only a recorded score,
    from the same run model and judge, that this run fell short of is."""
    entry = baseline.get("scores", {}).get(result.key)
    if not isinstance(entry, dict) or not isinstance(entry.get("score"), (int, float)):
        result.comparison = "new"
        return
    if baseline.get("run_model") and baseline["run_model"] != run_model:
        result.comparison = f"skipped: baseline recorded with {baseline['run_model']}"
        return
    if entry.get("judge") and entry["judge"] != result.judge:
        result.comparison = f"skipped: baseline judged by {entry['judge']}"
        return
    result.baseline = float(entry["score"])
    result.delta = round(result.score - result.baseline, 1)
    if result.delta < -tolerance:
        result.comparison = "regression"
        result.status = "fail"
        result.failures.append(
            f"judge score {result.score} is {-result.delta} below baseline {result.baseline} "
            f"(tolerance {tolerance})")
    else:
        result.comparison = "ok"


def run_case(case: Case, provider: str, model: str, baseline: dict,
             tolerance: float = DEFAULT_TOLERANCE) -> CaseResult:
    """Execute one case's LLM step from fixtures, then validate → judge → compare."""
    from src.automation.executor import _parse_result
    from src.automation.harness.evaluator import evaluate
    from src.automation.harness.provider import _is_retriable

    res = CaseResult(key=case.key)
    t0 = time.monotonic()
    for attempt in range(1, _RUN_ATTEMPTS + 1):
        try:
            raw = _adapter(case.job_type).run(case, provider, model)
            break
        except Exception as exc:  # noqa: BLE001 — one broken case must not hide the others
            # A provider blip (503/429/timeout) is not a quality signal: retry the
            # same model. No cross-model fallback — the score must stay comparable.
            if attempt < _RUN_ATTEMPTS and _is_retriable(exc):
                time.sleep(_RETRY_BACKOFF_SECS * attempt)
                continue
            res.status = "error"
            res.failures.append(f"run raised {type(exc).__name__}: {str(exc)[:300]}")
            res.duration_secs = round(time.monotonic() - t0, 1)
            return res
    output = raw if isinstance(raw, dict) else _parse_result(raw if isinstance(raw, str) else str(raw))
    res.output = output
    res.duration_secs = round(time.monotonic() - t0, 1)

    res.failures = check_expectations(case, output)
    if res.failures:
        res.status = "fail"

    ev = evaluate(case.job_type, output, provider, model)
    res.judge, res.judge_notes = ev.judge_model, ev.notes
    if ev.method != "llm" or ev.judge_model == _label(provider, model):
        # No independent judge → the number is not a quality measurement. Report
        # it as unscored rather than letting it pass (or poison) the gate.
        why = "self-graded by the run model" if ev.method == "llm" else "heuristic fallback"
        res.failures.append(f"no independent judge score ({why}): {ev.notes[:200]}")
        if res.status == "pass":
            res.status = "unscored"
        return res

    res.score, res.confidence = ev.score, ev.confidence
    floor = case.expect.get("min_judge_score")
    if floor is not None and ev.score < floor:
        res.status = "fail"
        res.failures.append(f"judge score {ev.score} is below min_judge_score {floor}")
    compare_to_baseline(res, baseline, _label(provider, model), tolerance)
    return res


def select_cases(cases: list[Case], only: list[str] | None) -> list[Case]:
    """Filter by ``job_type`` or ``job_type/id`` selectors (none == all)."""
    if not only:
        return cases
    return [c for c in cases if c.job_type in only or c.key in only]


def run_live(provider: str, model: str, *, root: Path | None = None, only: list[str] | None = None,
             tolerance: float = DEFAULT_TOLERANCE,
             progress: Callable[[CaseResult], None] | None = None) -> dict:
    """Run the live tier and return the report dict (see :func:`render_markdown`)."""
    cases, problems = load_cases(root)
    if problems:
        raise ValueError("eval cases are malformed — run the offline tier first:\n  "
                         + "\n  ".join(problems))
    baseline = load_baseline(root)
    results: list[CaseResult] = []
    for case in select_cases(cases, only):
        result = run_case(case, provider, model, baseline, tolerance)
        results.append(result)
        if progress:
            progress(result)
    counts = {s: sum(r.status == s for r in results) for s in ("pass", "fail", "error", "unscored")}
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "run_model": _label(provider, model),
        "tolerance": tolerance,
        "baseline_recorded_at": baseline.get("recorded_at"),
        "counts": counts,
        "ok": bool(results) and counts["pass"] == len(results),
        "results": [asdict(r) for r in results],
    }


def baseline_from(report: dict, previous: dict | None = None) -> dict:
    """Baseline file content for ``report``. Only independently judged scores are
    recorded; entries for cases this run didn't touch are kept when the run
    model is unchanged (so ``--only`` can refresh a subset)."""
    previous = previous or {}
    keep = previous.get("scores", {}) if previous.get("run_model") == report["run_model"] else {}
    scores = {k: v for k, v in keep.items() if isinstance(v, dict)}
    for r in report["results"]:
        if r["score"] is not None:
            scores[r["key"]] = {"score": r["score"], "judge": r["judge"]}
    return {
        "version": 1,
        "recorded_at": report["generated_at"],
        "run_model": report["run_model"],
        "scores": dict(sorted(scores.items())),
    }


def render_markdown(report: dict) -> str:
    c = report["counts"]
    lines = [
        "# Eval regression report",
        "",
        f"- Run model: `{report['run_model']}`",
        f"- Generated: {report['generated_at']}",
        f"- Baseline recorded: {report['baseline_recorded_at'] or 'unset — nothing to compare against yet'}",
        f"- Tolerance: {report['tolerance']} judge points",
        f"- Result: **{'PASS' if report['ok'] else 'FAIL'}** — {c['pass']} pass, {c['fail']} fail, "
        f"{c['error']} error, {c['unscored']} unscored",
        "",
        "| Case | Status | Score | Baseline | Δ | Comparison | Judge |",
        "|---|---|---|---|---|---|---|",
    ]

    def cell(v: object) -> str:
        return "–" if v in (None, "") else str(v).replace("|", "\\|")

    for r in report["results"]:
        lines.append(f"| {r['key']} | {r['status']} | {cell(r['score'])} | {cell(r['baseline'])} | "
                     f"{cell(r['delta'])} | {cell(r['comparison'])} | {cell(r['judge'])} |")
    problems = [r for r in report["results"] if r["failures"]]
    if problems:
        lines += ["", "## Failures"]
        for r in problems:
            lines += ["", f"### {r['key']} ({r['status']})"]
            lines += [f"- {f}" for f in r["failures"]]
            if r["judge_notes"]:
                lines.append(f"- judge notes: {r['judge_notes']}")
    return "\n".join(lines) + "\n"
