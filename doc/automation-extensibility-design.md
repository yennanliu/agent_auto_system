# Automation Extensibility & Maintainability — Design Options

> Status: **Phase 1 implemented** (Options A + B + C shipped) · Audience: maintainers ·
> Scope: how new automations are defined, registered, surfaced, and (eventually) authored
> by end users.
>
> This document maps how adding an automation works **today**, names the concrete
> pain points, then lays out a menu of design options with **effort, pros, and cons**
> for each — and a recommended phased roadmap. See [§8 Implementation status](#8-implementation-status)
> for what has landed.

---

## 1. Where we are today

Adding one `job_type` is described in `CLAUDE.md` as *"touch exactly these 6 files."*
In practice a job type's identity is spread across **~11 locations**, and several of
them are **hand-maintained copies of the same facts**. Missing one doesn't error — it
fails *silently* (the task becomes invisible in the UI and/or un-runnable server-side).

### 1.1 The real registration surface

| # | Location | What it holds | Duplication? |
|---|----------|---------------|--------------|
| 1 | `src/automation/executor.py` → `_FLOW_MAP` | job_type → (module, class, log line) | job_type list |
| 2 | `src/automation/flows/<name>_flow.py` | `Flow[State]` + `@start`/`@listen` boilerplate | — |
| 3 | `src/automation/crews/<name>_crew/` | `crew.py` + `config/agents.yaml` + `tasks.yaml` | — |
| 4 | `src/automation/tools/*.py` | tool classes | — |
| 5 | `src/routers/system.py` → `_CATALOG` | agents/tools/crews/workflows for the System tab | **copies YAML role/goal/backstory/tools by hand** |
| 6 | `src/settings_store.py` → `ALL_AUTOMATIONS` | the allowlist (gates UI + server) | job_type list |
| 7 | `src/automation/harness/validator.py` → `_CHECKS` | pass/fail rule per job_type | — |
| 8 | `src/automation/harness/evaluator.py` → `_RUBRICS` | LLM-judge rubric per job_type | — |
| 9 | `src/automation/flow_steps.py` → `FLOW_STEPS` | canonical step labels + log triggers (server) | **duplicated in JS ↓** |
| 10 | `ui/app.js` | `FLOW_STEPS` (dup of #9), `TYPE_META`, `AUTO_CATALOG`, `LLM_MODELS`, the `runForm` submit switch, `renderPipelineStepFields` switch | step defs, fields, job_type list, model list |
| 11 | `ui/index.html` | the `.type-card` tile + `#fields-<type>` form block | fields |

### 1.2 The four duplication hazards

These are where the design actively fights the maintainer:

1. **Step definitions live twice** — `flow_steps.py` (Python, drives `/overview`) and
   `ui/app.js` `FLOW_STEPS` (drives the live step graph). The Python file's own
   docstring says *"This mirrors the client-side FLOW_STEPS … in ui/app.js."* Two
   hand-synced copies of the same ordered list.
2. **Agent config lives twice** — the real `role`/`goal`/`backstory`/`tools` are in each
   crew's `config/agents.yaml`, and are **re-typed** into `system.py` `_CATALOG` for the
   System tab. They drift the moment someone edits one and not the other.
3. **Form fields live three times** — the HTML form (`#fields-<type>` in `index.html`),
   the per-type payload assembly (the giant `if/else` in `runForm`'s submit handler), and
   the pipeline step fields (`renderPipelineStepFields`). Three descriptions of the same
   inputs.
4. **The job_type list lives 4+ times** — `_FLOW_MAP`, `ALL_AUTOMATIONS`, `_CATALOG`
   `job_type` tags, the UI `.type-card`s, and `TYPE_META`/`AUTO_CATALOG`.

### 1.3 Consequences

- **Silent failure is the default.** Forget `ALL_AUTOMATIONS` → invisible + blocked.
  Forget the UI form → not runnable from the browser. Forget `_CHECKS`/`_RUBRICS` →
  degrades quietly to a generic check/rubric. Nothing shouts.
- **Copy-paste onboarding.** A new automation is written by cloning an existing flow +
  crew and find-replacing names. Boilerplate (`validate → resolve → kickoff → extract
  usage → parse`) is repeated in all 11 flows.
- **"Customize your own" is code-only.** Today an end user can *chain* existing types via
  `pipeline` and vary payload params — but authoring a *new* automation means editing
  Python + YAML + JS and redeploying. There is no runtime path.

---

## 2. Goals & principles

- **Single Source of Truth (SSOT).** One declaration per automation; everything else
  (dispatch, allowlist, catalog, checks, rubrics, steps, UI form) is *derived*.
- **Fail loud, at startup.** A misregistered automation should raise on boot or in a
  test, not disappear from a dropdown.
- **Keep the good invariants.** No `@CrewBase` (stale-LLM bug), constructor LLM injection,
  Pydantic flow state, graceful eval/trace degradation, single-pass stats. Any refactor
  must preserve these (see `CLAUDE.md` → *Key Invariants*).
- **Incremental & backwards-compatible.** Migrate one automation at a time; old and new
  registration coexist during transition.
- **Don't over-abstract deterministic funnels.** `email_collect` / `tasker_apply` drive
  tools directly for speed/reliability; the abstraction must not force everything through
  a single "one crew" mold.

---

## 3. Options

Each option is independent unless noted. Effort is rough dev-days for a maintainer who
knows the codebase. Risk is the chance of regressing a working automation.

### Option A — Declarative `AutomationSpec` registry (SSOT)

**Idea.** Define each automation once as a data object and register it via a decorator.
Derive `_FLOW_MAP`, `ALL_AUTOMATIONS`, `_CHECKS`, `_RUBRICS`, step defs, and (later) the
UI manifest from the registry.

```python
# src/automation/registry_spec.py  (new)
@dataclass(frozen=True)
class Field:
    name: str; type: str; label: str
    required: bool = False; default: Any = None
    min: float | None = None; max: float | None = None

@dataclass(frozen=True)
class AutomationSpec:
    job_type: str
    name: str                     # UI display name
    icon: str                     # emoji for the tile
    flow: type                    # the Flow subclass
    fields: list[Field]           # inputs → drives form + payload + pipeline
    steps: list[tuple[str, str]]  # (label, log-trigger) → drives both step graphs
    validate: Callable            # replaces _CHECKS entry
    rubric: str                   # replaces _RUBRICS entry
    default_temperature: float = 0.4
    browser: bool = False         # needs Playwright / a saved session
    start_log: str = ""

REGISTRY: dict[str, AutomationSpec] = {}

def automation(spec: AutomationSpec):
    if spec.job_type in REGISTRY:
        raise ValueError(f"duplicate automation {spec.job_type}")
    REGISTRY[spec.job_type] = spec
    return spec
```

Registration sits next to each flow:

```python
# src/automation/flows/hn_digest_flow.py
automation(AutomationSpec(
    job_type="hacker_news_digest", name="HN Digest", icon="🔶",
    flow=HNDigestFlow,
    fields=[Field("limit", "int", "Number of Stories (1–10)", default=5, min=1, max=10)],
    steps=[("Start","Starting"),("Validate","Fetching top"),("Digest","Digest generated")],
    validate=lambda r: (bool(r.get("stories")), "no stories in digest"),
    rubric="Several real HN stories are present with titles and a useful digest.",
))
```

Then the old dicts become one-liners:

```python
_FLOW_MAP        = {s.job_type: (s.flow, s.start_log) for s in REGISTRY.values()}
ALL_AUTOMATIONS  = list(REGISTRY)
_CHECKS          = {jt: s.validate for jt, s in REGISTRY.items()}
_RUBRICS         = {jt: s.rubric   for jt, s in REGISTRY.items()}
FLOW_STEPS       = {jt: s.steps    for jt, s in REGISTRY.items()}
```

- **Effort:** M (≈3–4 days). Add registry + specs, rewire 5 consumers, migrate 11
  automations, keep a startup assertion that every registered flow imports cleanly.
- **Pros:** Collapses 5–6 of the 11 touch points into 1. Duplication hazards #1, #4 gone;
  #2 shrinks (see C). "Forgot to register" becomes a boot-time error. Enables the manifest
  endpoint (D) for free.
- **Cons:** A one-time churn across many files. Specs must import their flow class, so mind
  import order / cycles (lazy-import the flow inside the spec if needed). Doesn't by itself
  touch the UI.
- **Risk:** Low–medium — pure re-plumbing, well covered by existing 380 tests + a new
  "every spec resolves" test.

### Option B — `BaseAutomationFlow` to kill flow boilerplate

**Idea.** Most flows do the same dance: validate fields → `resolve()` LLM at a temperature
→ build crew → `kickoff(inputs)` → `extract_usage` → return `.raw`. Fold that into a base
class; a new flow declares only its crew, inputs, and temperature.

```python
class BaseAutomationFlow(FlowMixin, Flow[S]):
    crew_cls: type; temperature: float = 0.4
    def build_inputs(self) -> dict: ...        # override
    # base implements validate → resolve → kickoff → usage → parse
```

- **Effort:** S–M (≈2 days). The base + migrating the ~8 "single-crew" flows. Leave the
  deterministic funnels (`email_collect`, `tasker_apply`, `tw104_apply`) as-is or give them
  a thinner base.
- **Pros:** New single-crew automation ≈ 15 lines. Fewer places to get the fallback/usage
  wiring subtly wrong. Pairs naturally with A.
- **Cons:** CrewAI `Flow` + Pydantic generics can be fiddly to subclass cleanly; must keep
  the "fresh crew per run / no `@CrewBase`" invariant. Not every flow fits one mold.
- **Risk:** Medium — touches the hot path (kickoff/fallback). Mitigate by migrating one
  flow, diffing behavior, then the rest.

### Option C — Derive the System catalog from YAML + specs

**Idea.** Stop hand-copying agent metadata into `system.py` `_CATALOG`. Build it by reading
each crew's `config/*.yaml` (already the source of truth) plus the spec's job_type/name.

- **Effort:** S (≈1–2 days).
- **Pros:** Kills duplication hazard #2 outright; the System tab can never drift from the
  real agent config again. Removes a large hand-maintained literal from `system.py`.
- **Cons:** Needs a small convention (which YAML → which job_type) — trivial once A exists
  (the spec names the crew). Tool/workflow descriptions may still need a short annotation.
- **Risk:** Low.

### Option D — Automation manifest endpoint + schema-driven UI

**Idea.** Expose `GET /api/automations/manifest` that serializes the registry (name, icon,
fields, steps, browser flag). The UI renders the picker tiles, the per-type form, the
pipeline step fields, and the payload assembly **from the manifest** instead of hardcoded
HTML/JS. Model dropdowns come from the existing provider catalog, not a JS copy.

- **Effort:** M–L (≈4–6 days). Build the manifest, a generic form renderer, generic payload
  collector, and migrate the picker/pipeline UI off the `if/else` switches.
- **Pros:** Removes duplication hazards #1 (steps), #3 (fields ×3), and the JS `LLM_MODELS`
  copy. Adding an automation becomes **zero UI edits**. Front-end and back-end can never
  disagree about fields/steps again.
- **Cons:** Biggest single lift; a generic form renderer needs to cover the field types in
  use (text, number+min/max, url, select, textarea, checkbox, file upload for
  `profit_health_check`). File-upload and bespoke widgets need escape hatches.
- **Risk:** Medium — user-facing; stage behind the existing pages and switch per-type.
  Depends on A (ideally C too).

### Option E — Unify step definitions (subset of A/D)

**Idea.** If A/D are too big to do now, at least make `flow_steps.py` the sole source and
have the client fetch steps (via a tiny endpoint) instead of keeping `ui/app.js`
`FLOW_STEPS`.

- **Effort:** S (≈1 day).
- **Pros:** Kills the single most fragile duplication (server/client step drift) cheaply.
- **Cons:** Point fix; leaves the other hazards. Largely subsumed by A+D later.
- **Risk:** Low.

### Option F — Entry-point / plugin packages

**Idea.** Let automations register via Python entry points (`importlib.metadata`), so a
separate package can add automations to a deployment **without forking** the repo. Builds
directly on A's `REGISTRY`.

- **Effort:** M (≈2–3 days on top of A).
- **Pros:** Clean third-party / private-automation story; core stays lean. Good for
  multi-team or SaaS-tenant separation.
- **Cons:** Only meaningful after A. Adds packaging/versioning surface and a trust boundary
  (arbitrary code loads at import). Overkill if all automations live in this repo.
- **Risk:** Medium (supply-chain / import-time code execution) — gate by allowlist/config.

### Option G — Runtime, user-defined automations (no-code)

**Idea.** The ambitious read of *"users customize their own automation."* Store automation
definitions in the DB and offer a **generic single-crew template**: the user picks a tool
(or a safe subset), writes the agent goal/backstory and an expected-output JSON shape, sets
a temperature, and names inputs — all from the Admin UI, no deploy. A `DynamicCrewFlow`
reads the definition and runs it through the same harness (validate/evaluate/fallback).

- **Effort:** L–XL (≈2–4 weeks for a safe MVP).
- **Pros:** Real self-service; non-devs create automations. Huge product differentiator.
  Everything still flows through the trusted harness (scoring, cost, retries).
- **Cons:** Big surface: a safe **tool allowlist** (no arbitrary code/SSRF/secret access),
  prompt-injection and abuse limits, per-user cost caps, validation of user output schemas,
  versioning/migration of definitions, and a real permissions model. Browser-driven and
  file-upload automations don't fit a generic template. Security review required.
- **Risk:** High — this is a product, not a refactor. Only sensible after A–D make the
  "one crew + fields + schema" shape first-class.

---

## 4. Comparison matrix

| Option | Effort | Risk | Kills duplication | UI edits to add an automation | User-facing win | Prereq |
|--------|:-----:|:----:|-------------------|:-----------------------------:|-----------------|--------|
| **A** Spec registry (SSOT) | M | Low–Med | #1 steps, #4 job_type list | still needed | none (dev velocity) | — |
| **B** BaseFlow | S–M | Med | flow boilerplate | — | none | pairs w/ A |
| **C** Catalog from YAML | S | Low | #2 agent config | — | accurate System tab | A (nice) |
| **D** Manifest + dynamic UI | M–L | Med | #1, #3 fields×3, model list | **none** | consistent forms | A (+C) |
| **E** Unify steps only | S | Low | #1 steps | — | none | — |
| **F** Plugin entry points | M | Med | — | depends | 3rd-party automations | A |
| **G** No-code user automations | L–XL | High | — | — | **self-service authoring** | A–D |

Add-an-automation cost, before vs. after:

- **Today:** ~11 edit points across Py/YAML/JS/HTML; 4 silent-failure traps.
- **After A+B+C:** 1 spec + 1 flow (~15 lines) + 1 crew dir; catalog/allowlist/checks/
  rubrics/steps auto-derived. UI still hand-edited.
- **After A+B+C+D:** 1 spec + 1 flow + 1 crew dir; **zero** UI edits.

---

## 5. Recommended roadmap

**Phase 1 — Consolidate (highest ROI, low risk): A + C + B. ✅ DONE.**
Introduce `AutomationSpec` + registry, derive the five server dicts, build the System
catalog from YAML, and add `BaseAutomationFlow`. Migrate automations one at a time behind a
"registry is authoritative" test. Net effect: the "6 files" really becomes ~2, and
forgetting a step becomes a boot/test error instead of a silent gap. Update `CLAUDE.md`
"Adding a New Job Type" to the new flow. **~1 week.** — *Shipped; see [§8](#8-implementation-status).*

**Phase 2 — Derive the UI: D (+E folds in).**
Ship `GET /api/automations/manifest` and render picker/form/pipeline/steps from it. After
this, adding an automation touches **no** front-end code and the client can't drift from the
server. **~1 week.**

**Phase 3 — Open it up: F and/or G, product-driven.**
If third parties/tenants need private automations → **F**. If the goal is non-devs authoring
automations in the browser → **G**, scoped to a safe single-crew template with a tool
allowlist, cost caps, and a security review. **Weeks, with review.**

Phases 1–2 are pure maintainability wins with no user-visible behavior change. Phase 3 is a
genuine product bet — do it only once the internal shape (Phase 1–2) makes it cheap and safe.

---

## 6. Migration & safety notes

- **Coexistence:** keep `_FLOW_MAP` et al. as *derived* values during migration; a job_type
  not yet ported keeps its literal entry. Registry entries win.
- **Loud on boot:** add a startup/test assertion that every `AutomationSpec` imports its
  flow, that `ALL_AUTOMATIONS == set(REGISTRY)`, and that every registered type has steps +
  validate + rubric. This directly removes the "silent invisibility" class of bug.
- **Invariants to preserve:** no `@CrewBase`; constructor LLM injection; Pydantic
  `llm_provider`/`llm_model` fields on state; eval/trace never raise into the executor;
  `get_stats()` single pass. Call these out in the PR checklist.
- **Tests:** the existing 380 tests are the safety net. Add: registry-completeness test,
  a manifest-shape test (Phase 2), and a per-migrated-automation golden-output diff.
- **Non-goals (for Phase 1–2):** changing crew logic, changing the harness contract, or
  changing run/DB schemas. This is re-plumbing, not re-architecting.

---

## 7. TL;DR

The automation layer is solid at runtime but **expensive to extend** because one
automation's identity is copied across ~11 places with 4 hand-synced duplications, and
mistakes fail silently. The fix is a **single declarative `AutomationSpec` registry** (A)
that everything derives from — do that first (with B + C), then **derive the UI from a
manifest** (D). Only after that foundation is in place should we consider **plugins** (F) or
true **no-code, user-authored automations** (G), the latter gated by a real security review.

---

## 8. Implementation status

### Phase 1 — shipped (Options A + B + C)

**A — `AutomationSpec` registry (SSOT).** New pure-data module `src/automation/spec.py`
declares every job type once (`register(AutomationSpec(...))`) with name, icon, flow
module/class, `start_log`, `temperature`, `steps`, `validate`, and `rubric`. Five consumers
now *derive* their tables instead of hand-maintaining them:

| Consumer (symbol) | Now derived from |
|---|---|
| `executor._FLOW_MAP` | `spec.flow_map()` |
| `settings_store.ALL_AUTOMATIONS` | `spec.job_types()` |
| `validator._CHECKS` | `spec.checks()` |
| `evaluator._RUBRICS` | `spec.rubrics()` |
| `flow_steps.FLOW_STEPS` | `spec.step_map()` |

Symbol names and shapes are unchanged, so nothing downstream (or in tests that
`patch.dict(_FLOW_MAP, …)`) had to change. `spec.py` imports nothing heavy (flows are
referenced by `module:class` strings and imported lazily by the executor), so it is safe to
import from low-level modules like `settings_store` with no import cycle. Kills duplication
hazards **#1 (steps, was duplicated Python↔JS server-side)** and **#4 (job_type list)**.

**Fail-loud guard.** `tests/unit/test_spec_registry.py` (19 tests) asserts the registry is
internally consistent, matches every derived table, and that **every flow-backed spec's
`(module, class)` actually imports** — so a misregistered automation is a red test, not a
silent gap.

**B — `FlowMixin._run_crew`.** The identical `resolve → kickoff → extract_usage →
raw-extract` boilerplate in the six single-crew flows (`hn_digest`, `web_scraper`,
`x_scraper`, `google_sheet`, `shopee_seller`, `form_fill`) collapsed to one call; each
`execute_crew` is now ~6 lines. Implemented as a **mixin helper, not a Flow base class**:
a probe confirmed the doc's warning — CrewAI's Flow metaclass does not route `kickoff`
results through inherited `@start`/`@listen` methods (returns `None`). Deterministic /
non-single-crew flows (`email_sender`, `email_collect`, `tasker_apply`, `tw104_apply`,
`profit_health`) were intentionally left as-is.

**C — System catalog agents from YAML.** `system.py` no longer hand-copies agent
`role`/`goal`/`backstory`; a compact `_AGENT_DEFS` table (structural facts only) is merged
with those fields read live from each crew's `agents.yaml` by `_build_agents()`. Kills
duplication hazard **#2** — the System tab can no longer drift from the real agent config.

**Invariants preserved:** no `@CrewBase`; constructor LLM injection; Pydantic
`llm_provider`/`llm_model` on state; eval/trace still degrade gracefully; `get_stats()`
single pass. Full suite green (**562 passed**, was 543 + 19 new registry tests); lint clean.

**Docs:** `CLAUDE.md` "Adding a New Job Type" rewritten to the registry flow.

### Not yet done

- **Phase 2 (D + E):** manifest endpoint + schema-driven UI. The `AutomationSpec.fields`
  slot exists but is intentionally unpopulated — form fields stay authoritative in
  `ui/index.html` + `ui/app.js` until the UI is wired from a manifest (avoids introducing a
  *new* unsynced copy in the meantime). Duplication hazard #3 (fields ×3) remains until then.
- **Phase 3 (F / G):** plugin entry points and no-code user-authored automations — unchanged
  from the proposal; the Phase 1 registry is the foundation they build on.
