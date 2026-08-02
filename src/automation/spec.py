"""Single source of truth (SSOT) for automation job types.

Every automation ("job type") is declared once here as an :class:`AutomationSpec`.
The rest of the system *derives* its per-job-type tables from :data:`REGISTRY`
instead of hand-maintaining parallel copies:

    executor._FLOW_MAP          ← flow_map()        (dispatch: module/class/log)
    settings_store.ALL_AUTOMATIONS ← job_types()    (the allowlist)
    validator._CHECKS           ← checks()          (pass/fail rule)
    evaluator._RUBRICS          ← rubrics()          (LLM-judge rubric)
    flow_steps.FLOW_STEPS       ← step_map()         (step graph, server + client)

This module is intentionally **pure data**: it imports nothing heavy (no crewai,
no flows, no tools), so it is safe to import from low-level modules like
``settings_store`` without risking circular imports. Flow classes are referenced
lazily by ``(flow_module, flow_class)`` strings and imported on demand by the
executor — exactly as the old ``_FLOW_MAP`` did.

Adding a new automation: add one :func:`register` call below, create the flow +
crew, and (until Phase 2 wires the UI from a manifest) the UI form. See
``doc/automation-extensibility-design.md``.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Quality-assurance + terminal steps run centrally in the executor after every
# job, so every flow's step graph ends with these. Mirrors flow_steps._QA/_DONE.
_QA: tuple[tuple[str, str], ...] = (("Verify", "Validating result"), ("Evaluate", "Evaluation complete"))
_DONE: tuple[str, str] = ("Done", "completed successfully")


@dataclass(frozen=True)
class Field:
    """A single user input for an automation's run form.

    Consumed by the manifest-driven UI (Phase 2): the browser renders one input
    per field and collects the payload generically from ``data-field`` inputs.
    Automations with a bespoke form (file upload, pipeline builder, …) set
    ``AutomationSpec.custom_ui=True`` instead and keep their hand-written HTML.
    """

    name: str
    type: str  # text | number | url | select | textarea | checkbox
    label: str
    required: bool = False
    default: object = None
    min: float | None = None
    max: float | None = None
    placeholder: str = ""
    help: str = ""  # small hint rendered under the input
    options: tuple[tuple[str, str], ...] = ()  # (value, label) for select


@dataclass(frozen=True)
class AutomationSpec:
    """Everything the platform needs to know about one job type."""

    job_type: str
    name: str  # UI display name
    icon: str  # emoji shown on the picker tile
    rubric: str  # what a good result looks like (LLM-judge grounding)
    validate: Callable[[dict], tuple[bool, str]]  # (passed, reason) — reason used on failure
    steps: tuple[tuple[str, str], ...] = ()  # (label, log-trigger); () when handled specially
    # Dispatch. flow_module is None for job types the executor handles directly
    # (e.g. "pipeline"), which therefore contribute no _FLOW_MAP entry.
    flow_module: str | None = None
    flow_class: str | None = None
    start_log: str = ""
    temperature: float = 0.7
    browser: bool = False  # needs Playwright / a saved browser session
    # Manifest-driven UI (Phase 2). When custom_ui is False, the browser renders
    # the run form from `fields` and derives the run name from `name_template`
    # (a Python str.format template over the payload, falling back to `name`).
    # custom_ui=True keeps a bespoke hand-written form (file upload, pipeline,
    # multi-field flows) — the escape hatch called for in the extensibility RFC.
    fields: tuple[Field, ...] = field(default=())
    name_template: str = ""
    custom_ui: bool = False
    help_note: str = ""  # optional HTML note rendered under a generic form
    desc: str = ""  # one-line description shown on the picker tile


REGISTRY: dict[str, AutomationSpec] = {}


def register(spec: AutomationSpec) -> AutomationSpec:
    """Add a spec to the registry, rejecting duplicate job types (fail loud)."""
    if spec.job_type in REGISTRY:
        raise ValueError(f"duplicate automation registered: {spec.job_type!r}")
    REGISTRY[spec.job_type] = spec
    return spec


PLUGIN_GROUP = "agent_auto_system.automations"


def load_plugins() -> list[str]:
    """Register automations from external packages via importlib entry points.

    A plugin package exposes an entry point in group ``agent_auto_system.automations``
    pointing at a callable ``def setup(register): register(AutomationSpec(...))``.

    OFF unless ``AUTOMATION_PLUGINS_ENABLED=1``: third-party code executes at
    import, so enabling plugins is an explicit trust decision (see Option F in
    doc/automation-extensibility-design.md). Returns the names loaded.
    """
    if os.getenv("AUTOMATION_PLUGINS_ENABLED", "0") != "1":
        return []
    try:
        from importlib.metadata import entry_points
        eps = list(entry_points(group=PLUGIN_GROUP))
    except Exception as exc:  # noqa: BLE001
        logger.warning("automation plugin discovery failed: %s", exc)
        return []
    loaded: list[str] = []
    for ep in eps:
        try:
            ep.load()(register)  # the plugin calls register() for each spec
            loaded.append(ep.name)
            logger.info("loaded automation plugin %r", ep.name)
        except Exception as exc:  # noqa: BLE001 — a bad plugin must not crash boot
            logger.warning("automation plugin %r failed to load: %s", ep.name, exc)
    return loaded


_plugins_loaded = False


def ensure_plugins_loaded() -> None:
    """Load plugins on first use (idempotent), not at import.

    Keeps this module's import a pure, side-effect-free data load (so low-level
    importers like settings_store stay safe), and defers any third-party code to
    the first time a derived table is built. A no-op when plugins are disabled.
    """
    global _plugins_loaded
    if not _plugins_loaded:
        _plugins_loaded = True
        load_plugins()


# ── Derivation helpers ──────────────────────────────────────────────────────
# Each returns a plain dict/list with the exact shape its consumer expects, so
# the consumer keeps its public symbol name and behavior unchanged. Each triggers
# a one-time plugin load first, so plugin-registered specs are always included.

def job_types() -> list[str]:
    """Every registered job type — the allowlist (ALL_AUTOMATIONS)."""
    ensure_plugins_loaded()
    return list(REGISTRY)


def flow_map() -> dict[str, tuple[str, str, str]]:
    """job_type → (flow_module, flow_class, start_log) for flow-backed types."""
    ensure_plugins_loaded()
    return {
        jt: (s.flow_module, s.flow_class, s.start_log)
        for jt, s in REGISTRY.items()
        if s.flow_module and s.flow_class
    }


def checks() -> dict[str, Callable[[dict], tuple[bool, str]]]:
    """job_type → validation predicate (validator._CHECKS)."""
    ensure_plugins_loaded()
    return {jt: s.validate for jt, s in REGISTRY.items()}


def rubrics() -> dict[str, str]:
    """job_type → LLM-judge rubric (evaluator._RUBRICS)."""
    ensure_plugins_loaded()
    return {jt: s.rubric for jt, s in REGISTRY.items()}


def step_map() -> dict[str, list[tuple[str, str]]]:
    """job_type → ordered [(label, trigger)] for types that define steps."""
    ensure_plugins_loaded()
    return {jt: list(s.steps) for jt, s in REGISTRY.items() if s.steps}


def manifest() -> list[dict]:
    """Serialize the registry for the browser (GET /api/automations/manifest).

    Drives the picker tiles, the generic run form + payload collection, and the
    live step graph — so a new automation with standard fields needs no UI edits.
    ``custom_ui`` types still ship a hand-written form; the UI shows that instead.
    """
    ensure_plugins_loaded()
    out = []
    for jt, s in REGISTRY.items():
        out.append({
            "job_type": jt,
            "name": s.name,
            "icon": s.icon,
            "desc": s.desc,
            "browser": s.browser,
            "custom_ui": s.custom_ui,
            "name_template": s.name_template,
            "help_note": s.help_note,
            "steps": [[label, trigger] for label, trigger in s.steps],
            "fields": [
                {
                    "name": f.name, "type": f.type, "label": f.label,
                    "required": f.required, "default": f.default,
                    "min": f.min, "max": f.max, "placeholder": f.placeholder,
                    "help": f.help, "options": [list(o) for o in f.options],
                }
                for f in s.fields
            ],
        })
    return out


def validate_registry() -> list[str]:
    """Return a list of consistency problems (empty == healthy).

    Used by a startup check / test so a misregistered automation fails loud
    instead of silently vanishing from a dropdown.
    """
    ensure_plugins_loaded()
    problems: list[str] = []
    for jt, s in REGISTRY.items():
        if s.job_type != jt:
            problems.append(f"{jt}: job_type mismatch ({s.job_type!r})")
        if not s.name or not s.icon:
            problems.append(f"{jt}: missing name/icon")
        if not s.rubric:
            problems.append(f"{jt}: missing rubric")
        if not callable(s.validate):
            problems.append(f"{jt}: validate is not callable")
        # A flow-backed type needs both module and class.
        if bool(s.flow_module) != bool(s.flow_class):
            problems.append(f"{jt}: flow_module/flow_class must be set together")
    return problems


# ══════════════════════════════════════════════════════════════════════════════
# Registrations. One entry per automation — the single place these facts live.
# ══════════════════════════════════════════════════════════════════════════════

register(AutomationSpec(
    job_type="google_form_fill", desc='Auto-fill any Google Form', custom_ui=True, name="Form Fill", icon="📋",
    flow_module="src.automation.flows.form_fill_flow", flow_class="FormFillFlow",
    start_log="Launching form fill agent...", temperature=0.0, browser=True,
    steps=(("Start", "Starting"), ("Validate", "Payload validated"),
           ("Inspect Form", "Inspecting Google Form"), ("Submit", "Form submission attempted"),
           *_QA, _DONE),
    validate=lambda r: (r.get("submitted") is True, "form not submitted"),
    rubric="The form was actually submitted (submitted=true) with sensible field values.",
))

register(AutomationSpec(
    job_type="web_scraper", desc='Scrape full page content & summary', name="Web Scraper", icon="🌐",
    flow_module="src.automation.flows.web_scraper_flow", flow_class="WebScraperFlow",
    start_log="Launching web scraper agent...", temperature=0.1,
    steps=(("Start", "Starting"), ("Validate", "Payload validated"),
           ("Scrape", "scraper agent reading"), ("Analyze", "generated summary"),
           *_QA, _DONE),
    validate=lambda r: (
        bool(r.get("content") or r.get("title") or r.get("summary") or r.get("data")),
        "no content scraped",
    ),
    rubric="Substantive page content/title/summary was extracted for the target URL.",
    name_template="Scrape: {url}",
    fields=(
        Field("url", "url", "URL to Scrape", required=True, placeholder="https://example.com"),
    ),
))

register(AutomationSpec(
    job_type="hacker_news_digest", desc='Top HN stories with AI summary', name="HN Digest", icon="🔶",
    flow_module="src.automation.flows.hn_digest_flow", flow_class="HNDigestFlow",
    start_log="Contacting Hacker News API...", temperature=0.4,
    steps=(("Start", "Starting"), ("Validate", "Fetching top"),
           ("Digest", "Digest generated"), *_QA, _DONE),
    validate=lambda r: (
        bool(r.get("stories") or r.get("digest") or r.get("items") or r.get("answer")),
        "no stories in result",
    ),
    rubric="Several real HN stories are present with titles and a useful digest.",
    name_template="HN Digest (top {limit})",
    fields=(
        Field("limit", "number", "Number of Stories (1–10)", default=5, min=1, max=10),
    ),
))

register(AutomationSpec(
    job_type="x_scraper", desc='Recent posts from any X profile', custom_ui=True, name="X Scraper", icon="✕",
    flow_module="src.automation.flows.x_scraper_flow", flow_class="XScraperFlow",
    start_log="Connecting to X profile scraper...", temperature=0.3, browser=True,
    steps=(("Start", "Starting"), ("Validate", "Validated payload"),
           ("Fetch", "Fetching posts"), ("Analyze", "Analysis complete"), *_QA, _DONE),
    validate=lambda r: (
        bool(r.get("posts") or r.get("profile") or r.get("summary") or r.get("data")),
        "no profile data found",
    ),
    rubric="Real profile/post data was captured, not empty or an error page.",
))

register(AutomationSpec(
    job_type="email_sender", desc='Send email to multiple recipients', custom_ui=True, name="Email Sender", icon="✉️",
    flow_module="src.automation.flows.email_sender_flow", flow_class="EmailSenderFlow",
    start_log="Preparing email delivery...", temperature=0.7,
    steps=(("Start", "Starting"), ("Validate", "Sending to"),
           ("Send", "Connecting to Gmail"), *_QA, _DONE),
    validate=lambda r: (r.get("sent") is True, "email not sent"),
    rubric="The email was sent (sent=true) with a coherent subject and body.",
))

register(AutomationSpec(
    job_type="google_sheet_reader", desc='Fetch & analyze Google Sheets', name="Sheet Reader", icon="📊",
    flow_module="src.automation.flows.google_sheet_flow", flow_class="GoogleSheetFlow",
    start_log="Connecting to Google Sheets...", temperature=0.1,
    steps=(("Start", "Starting"), ("Validate", "Validated sheet URL"),
           ("Fetch", "Fetching Google Sheet"), ("Analyze", "Analyzing sheet data"), *_QA, _DONE),
    validate=lambda r: (
        bool(r.get("columns") or r.get("data") or r.get("summary")),
        "no sheet data returned",
    ),
    rubric="Real sheet data was returned (columns/rows/summary), not empty or placeholder.",
    name_template="Sheet Reader",
    fields=(
        Field("url", "text", "Google Sheet URL", required=True,
              placeholder="https://docs.google.com/spreadsheets/d/…",
              help="Paste any Google Sheets URL — share link, edit link, or CSV export URL"),
        Field("limit", "number", "Max Rows (1–500)", default=200, min=1, max=500),
    ),
))

register(AutomationSpec(
    job_type="shopee_seller_scraper", desc='Collect sellers from top products', name="Shopee Sellers", icon="🛒",
    flow_module="src.automation.flows.shopee_seller_flow", flow_class="ShopeeSellerFlow",
    start_log="Loading Shopee session...", temperature=0.2, browser=True,
    steps=(("Start", "Starting"), ("Validate", "Validated payload for keyword"),
           ("Search", "Loading Shopee session"), ("Collect", "Seller collection complete"),
           *_QA, _DONE),
    validate=lambda r: (bool(r.get("sellers")), "no sellers found"),
    rubric="A non-empty list of sellers with plausible fields was returned.",
    name_template="Shopee: {keyword}",
    fields=(
        Field("keyword", "text", "Search Keyword", required=True,
              placeholder="e.g. 無線耳機 / wireless earbuds"),
        Field("limit", "number", "Number of Products (1–100)", default=5, min=1, max=100),
    ),
    help_note=(
        "Requires a saved Shopee login session. Run "
        "<code>uv run python scripts/shopee_login.py</code> once, then set "
        "<code>SHOPEE_STORAGE_STATE</code> in <code>.env</code>."
    ),
))

register(AutomationSpec(
    job_type="profit_health_check", desc='Upload Shopee CSVs → profit report', custom_ui=True, name="利潤健檢", icon="🧾",
    flow_module="src.automation.flows.profit_health_flow", flow_class="ProfitHealthFlow",
    start_log="解析 CSV，計算利潤健檢...", temperature=0.2,
    steps=(("Start", "Starting"), ("Load CSV", "Loaded CSVs"),
           ("驗證", "蝦皮資料驗證員"), ("修正", "蝦皮資料修正員"),
           ("分析", "蝦皮利潤分析師"), ("建議", "蝦皮營運行動建議員"),
           ("PDF", "PDF 報告"), *_QA, _DONE),
    validate=lambda r: (
        bool(r.get("skus") or r.get("action_items") or r.get("recommendations")),
        "no profit analysis in result",
    ),
    rubric="Concrete SKU-level analysis with actionable recommendations is present.",
))

register(AutomationSpec(
    job_type="tasker_apply", desc='Auto-apply to tasker.com.tw cases', custom_ui=True, name="Tasker 自動提案", icon="🧰",
    flow_module="src.automation.flows.tasker_apply_flow", flow_class="TaskerApplyFlow",
    start_log="Loading tasker.com.tw session...", temperature=0.5, browser=True,
    steps=(("Start", "Starting"), ("Validate", "Payload validated"),
           ("Login", "Loading tasker.com.tw session"), ("Apply", "run complete"), *_QA, _DONE),
    validate=lambda r: (
        isinstance(r.get("applied"), list) and r.get("cases_found") is not None,
        "no cases processed",
    ),
    rubric=("Cases were found and an accurate applied[] list reflects real submissions; "
            "when a task_filter was used, cases skipped as 'filtered out' with a reason are "
            "correct behavior, not failures."),
))

register(AutomationSpec(
    job_type="tw104_apply", desc='Auto-apply to 104.com.tw job openings', custom_ui=True, name="104 自動應徵", icon="💼",
    flow_module="src.automation.flows.tw104_apply_flow", flow_class="TW104ApplyFlow",
    start_log="Loading 104.com.tw session...", temperature=0.2, browser=True,
    steps=(("Start", "Starting"), ("Validate", "Payload validated"),
           ("Login", "Loading 104.com.tw session"), ("Apply", "run complete"), *_QA, _DONE),
    validate=lambda r: (
        isinstance(r.get("applied"), list) and r.get("jobs_found") is not None,
        "no jobs processed",
    ),
    rubric=("Jobs were found and an accurate applied[] list reflects real applications "
            "(a submission counts only when the site confirmed /job/apply/done/); jobs "
            "skipped as already-applied or 'filtered out' with a reason are correct "
            "behavior, not failures."),
))

register(AutomationSpec(
    job_type="linkedin_apply", desc='Auto-apply to LinkedIn Easy Apply jobs', custom_ui=True, name="LinkedIn Easy Apply", icon="🔗",
    flow_module="src.automation.flows.linkedin_apply_flow", flow_class="LinkedInApplyFlow",
    start_log="Loading LinkedIn session...", temperature=0.2, browser=True,
    steps=(("Start", "Starting"), ("Validate", "Payload validated"),
           ("Login", "Loading LinkedIn session"), ("Apply", "run complete"), *_QA, _DONE),
    validate=lambda r: (
        isinstance(r.get("applied"), list) and r.get("jobs_found") is not None,
        "no jobs processed",
    ),
    rubric=("Jobs were found and an accurate applied[] list reflects real applications "
            "(a submission counts only when LinkedIn confirmed the 'application was sent' "
            "banner); jobs skipped as already-applied or 'filtered out' with a reason are "
            "correct behavior, not failures."),
))

register(AutomationSpec(
    job_type="email_collect", desc='Find businesses & collect their emails', custom_ui=True, name="Email Collector", icon="📧",
    flow_module="src.automation.flows.email_collect_flow", flow_class="EmailCollectFlow",
    start_log="Starting lead-collection funnel...", temperature=0.4, browser=True,
    steps=(("Start", "Starting"), ("Validate", "Payload validated"),
           ("Discover", "Discovering businesses"), ("Extract", "Extracting email"),
           ("Collect", "Collected"), ("Enrich", "Enriching"),
           ("Qualify", "Qualifying"), *_QA, _DONE),
    validate=lambda r: (r.get("discovered_count", 0) > 0, "no businesses discovered"),
    rubric="Real businesses were discovered with verified contact emails and useful personalization hooks.",
))

# Pipeline is dispatched directly by the executor (no flow class) and defines no
# step graph of its own (the UI renders per-step sub-graphs), so it contributes
# to the allowlist / checks / rubrics but not to _FLOW_MAP or FLOW_STEPS.
register(AutomationSpec(
    job_type="pipeline", desc='Chain automations in sequence', custom_ui=True, name="Pipeline", icon="🔗",
    rubric="Each declared step ran and produced non-empty, on-topic output.",
    validate=lambda r: (bool(r.get("steps")), "pipeline completed no steps"),
))


# Third-party automations (opt-in via AUTOMATION_PLUGINS_ENABLED=1) are loaded
# lazily by ensure_plugins_loaded() on first table derivation — NOT at import —
# so importing this module stays a pure, side-effect-free data load.
