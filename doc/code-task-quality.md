# Improving code-task quality: lessons from `junyi-sw-agent`

> Date: 2026-11-26 · Reference: [`junyiacademy/junyi-sw-agent`](https://github.com/junyiacademy/junyi-sw-agent) (read at `main`, shallow clone)
> Scope: **how a coding task gets done, checked and merged in this repo** when an AI agent (Claude Code) does most of the work. This doc is not about the automations' runtime quality, except where the same gap shows up there (LLM-prompt changes).

---

## TL;DR

`junyi-sw-agent` is a Claude Code plugin plus its own dev harness. Its whole point is that **an agent's PR can be trusted without someone re-doing the work**. To get there it treats every claim in a PR as something a tool must back up. We ship fast and our CI is broader, but our quality checks are **advisory and done by hand**. Nothing stops an unverified change from merging, and the same root cause gets patched several times in one day.

Top 5 things to adopt, in priority order:

| # | Do this | Effort | Fixes |
|---|---|---|---|
| 1 | **A "Verification" contract in every PR**: *expected change → how it was tested → result*. The test has to reach the code path that changed. Say plainly what was not tested | S | Fix chains, "verified" claims that never ran the changed branch |
| 2 | **Turn every live failure into a regression case** before or with the fix (unit fixture, golden HTML/JSON, or eval case) | S–M | Same bug class coming back (tw104 ×4, workday ×10 in a day) |
| 3 | **Prompt changes get an eval, not a one-off script**: per-crew cases under `evals/`, ≥3 runs, report *c/n* against a baseline | M | Prompt tweaks (e.g. #139) that nothing guards after merge |
| 4 | **Enforce the local and merge gates**: committed git hooks (secrets scan + ruff + fast tests), a branch-protection rule that requires CI | S | Hooks that are configured but never installed; merging on red/unknown CI |
| 5 | **An independent reviewer with a fixed rubric** that sees only the PR number, keeps "report" separate from "block", and labels each finding *confirmed* or *speculative* | M | Self-review bias; bots' comments are noise and never block |

---

## 1. The two repos at a glance

| | `junyi-sw-agent` (reference) | `agent_auto_system` (us) |
|---|---|---|
| What it builds | A Claude Code plugin (`junyi-dev`) that takes a ticket in another repo, changes code, verifies it locally and opens a draft PR with evidence | A FastAPI + CrewAI automation platform (scrapers, ATS auto-apply, digests) |
| Who writes the code | Claude Code sessions, run as orchestrator / worker / reviewer subagents | Claude Code sessions, mostly single-session per PR |
| Process size | Heavy: `scripts/jswr` CLI, ~20 check items, evidence ledger, merge-gate hook, 9 hook scripts, 9 agent roles, ~40 skills | Light: CLAUDE.md + CI + bot reviewers |
| Last 60 days (us) | — | 47 merged PRs; **74 `fix` vs 41 `feat`** commits; up to 22 commits/day |

The products differ, but both repos are built the same way: an agent does a ticket and opens a PR. So their *process* lessons carry over directly.

### Signals from our own history

- **Fix chains.** On 2026-11-26 four `fix(tw104)` commits landed in one day (#136 → #139 and follow-ups). On 2026-11-24 ten `fix(workday)` commits landed in one day. Each fix was right but incomplete. The next live run found the next hole.
- **Hotspots without a matching test focus.** Size × 60-day churn: `tools/ats_common.py` (1323 lines × 36 changes), `ats_workday.py` (1172 × 19), `greenhouse_apply_tool.py` (907 × 17), `spec.py` (803 × 18). These files are where regressions cost the most.
- **Prompt changes guarded by one-off scripts.** #139 checked the tw104 relevance prompt on 45 live titles (30/45 → 45/45) with a new `scripts/tw104_relevance_check.py`. That was a good measurement. But it is not a regression case: `tw104_apply` stays in `evals/skip.yaml`, so the next prompt edit cannot tell if it broke those 45.
- **Hooks that never run.** `.pre-commit-config.yaml` exists (ruff, mypy), but no hook is installed in the main checkout (`.git/hooks` has only samples), and nothing scans staged lines for secrets. Our sensitive files (`data/workday_accounts.json`, `data/default_answers.txt`) are protected only by `.gitignore`.

---

## 2. Side-by-side: who does better

| Dimension | `junyi-sw-agent` | Us | Better |
|---|---|---|---|
| **Task definition** | A plan is a ticket. Before testing, write "what changed" and "what should look different". If you can't write it, think more before coding (`repo-verify` §1) | Problem section in PR body (often excellent, with run IDs). No required "expected change" | **Ref** (ours is good but optional) |
| **Workspace isolation** | Worktree per PR/plan, created and cleaned by tools; `.agent/` state survives sessions | Worktree per PR (memory rule), by hand | Ref (small gap) |
| **Verification evidence** | Evidence ledger: each claim is a command run by a tool, with id `E<n>`, commit, expected vs actual, full output kept. The tool tracks staleness. `evidence check-pr` blocks PRs whose cited evidence is missing, stale, or untested-but-unmarked | Free-text "Verification" / "Tests" sections | **Ref** (big gap) |
| **"Did the test reach the changed path?"** | Explicit rule plus reviewer row, learned from real misses: a test ran on a clone with real secrets and never hit the fake-secrets path; before/after were the same commit | Not asked | **Ref** |
| **Picking the right test size** | `S0` unit/static → `S1` real env, no LLM → `S2` one LLM judgement → `S3` full rehearsal. A table maps each file area to the cheapest level that can answer the question | Implicit; author's call | **Ref** |
| **LLM / prompt changes** | ≥3 runs, report *c/n* with Wilson 95% interval; cases come from **real failures**; run a baseline (old prompt) first and drop cases both versions pass; validate the judge against human labels before trusting it | Golden eval cases + nightly evals for 7 types; browser-apply types skipped (reason documented); prompt fixes checked with one-off scripts | Ref on method; **us** on having a product eval set at all |
| **Local gates** | Committed `.githooks/` (`core.hooksPath`): pre-commit runs secrets on staged lines + second-level checks; pre-push stays cheap; fingerprint cache so re-runs take seconds | `.pre-commit-config.yaml` not installed; no secrets scan | **Ref** |
| **Merge gate** | `merge_gate.py` hook blocks `gh pr merge` unless (a) the *PR + latest base* merge result has a passing check record and (b) the reviewer passed *this head*. The gate version comes from the base branch, so a PR can't weaken its own gate | CI runs on PRs, but `main` has no branch protection (verified 2026-11-26: `protected: false`); merging is a human/agent choice | **Ref** |
| **Code review** | `repo-reviewer` subagent gets **only the PR number**, never the author's framing. It reads raw evidence, not the PR description. Fixed rubric (correctness, placement, tests, evidence reaches change, every reader of a renamed thing, docs, boundaries). Only *confirmed* CRITICAL/HIGH block; *speculative* is listed, not blocking. Second blocked round → stop and ask a human. Verdict tied to the commit | CodeRabbit (assertive) + pr-lens bots; comments never block; no repo-specific rubric | **Ref** |
| **Boundaries** | Each rule lists *who blocks it* (hook, check, or "judgement only"). `boundary_guard` hook blocks writes to read-only clones and external pushes, with the fix in the message. Rules load when needed, not all in CLAUDE.md | Rules live in memory notes / CLAUDE.md prose ("never commit credentials") | **Ref** |
| **Learning loops** | `/reflect` (why an instruction was ignored → fix the layer it lives in), `/retro` (repeated friction over a period, prune checks that don't pay), "same problem twice → change the tool, not the prompt" | None formal; lessons go into memory | **Ref** |
| **Hotspot visibility** | Generated `docs/architecture/12-hotspots.md` (lines × churn, ⚠ when no test) | None | **Ref** |
| **CI breadth** | Local check script; e2e needs docker, not in CI | Unit + integration + UI (jsdom) + Docker in-container + smoke + nightly evals + canaries + live ATS dry-run smoke | **Us** |
| **Registry consistency** | Check items for layers, names, paths | `AutomationSpec` SSOT + `test_spec_registry.py`, `test_evalset.py`, `test_ui_modules.py` fail loudly | **Us** |
| **PR narrative** | Written for reviewer + audit trail; terse | Problem → Change → Verification with real run numbers; very readable | **Us** |
| **Process cost** | Its own harness is now its top hotspot (`check.py` 1350 lines × 29, `workspace.py` 2151 lines); many decisions exist only to tune the process | Near zero | **Us** |

---

## 3. Gaps in detail

### G1. "Verified" is a claim, not a record
**Today:** The PR says "`tests/unit/test_tw104_apply.py` passes (44)" or "verified live". A reviewer can't tell which commit it ran on, or whether it ran the changed branch.
**Reference:** Each claim is `E<n>` = command + commit + expected/actual + full log. Evidence goes stale when the files it depends on change. Unverified items must say **[untested]** with a reason, and the gate checks this.
**Why it matters:** Most of our fix chains come from a fix verified on the *happy* path while the next live run hit the *other* path.

### G2. Live failures don't become tests
**Today:** A live run finds a bug, we patch it, and the proof is a re-run. The failing page/DOM/answer is not kept.
**Reference:** "Errors caught by a rehearsal or a human should first become an S0/S1 check, so next time no LLM is needed to catch them." Eval cases come only from real failures. Each case has a control run.
**Why it matters:** Workday's 10 fixes in a day were 10 different DOM/flow shapes. Each could have been a saved HTML fixture tested in seconds.

### G3. Prompt changes have no regression net
**Today:** Prompts for the relevance gate, answer drafting and judges change often. The browser-apply types are in `evals/skip.yaml` because the *whole run* can't be replayed. But the crews inside it are pure LLM calls.
**Reference:** Test the smallest unit that holds the judgement. Assert the deterministic parts in unit tests. Use LLM evals only for the judgement, with *c/n* over ≥3 runs, a baseline, and only cases that split good from bad.
**Why it matters:** #139's 45 titles are exactly a per-crew eval set. Right now they live in a script nobody runs.

### G4. Gates exist on paper
**Today:** Pre-commit config isn't installed. No staged-secrets scan. Merging doesn't need green CI or a review verdict.
**Reference:** Committed hooks enabled with one `git config`. The merge gate reads the check record for *PR + latest base* (merge-queue semantics). The gate's rules come from the base branch.

### G5. Review is generic and non-blocking
**Today:** CodeRabbit "assertive" + pr-lens leave many comments. Nothing ties a verdict to a commit or stops a merge. The authoring session effectively reviews itself.
**Reference:** A repo-specific rubric that grew from real misses (e.g. "renamed a path? list every reader, including files already on disk"; "merge commits that resolved conflicts: run `git show --remerge-diff`"). Clean context: only the PR number. Report everything, block only on confirmed HIGH+.

### G6. Boundaries rely on memory
**Today:** "Workday credentials local-only", "default answers never committed", "check active runs before a server restart": these are memory notes. Forget them and nothing stops you.
**Reference:** Every boundary names its enforcer. Where a tool can block it, a hook or check does, and the message says what to do instead. What a tool can't block lives in the skill that uses it.

### G7. No loop that turns repeated friction into tools
**Today:** Lessons pile up in memory and CLAUDE.md (164 lines, always loaded).
**Reference:** "If the same prompt section has been edited several times, switch to a tool." `/retro` reviews a period, follows up on the last retro's action items, and **prunes** checks that never fire. Always-loaded context is kept to what you'd get wrong without it.

---

## 4. Suggestions (roadmap)

### Quick wins (≤ 1 day each)

1. **PR template** `.github/pull_request_template.md`:
   ```markdown
   ## Problem          <!-- what broke / what's needed; run ids -->
   ## Change           <!-- one line per file that matters -->
   ## Expected change  <!-- what should look different after this PR -->
   ## Verification
   - Expected: … / How: <unit|fixture|live run #id|eval c/n> on <commit> / Result: …
   - Changed path exercised: <which branch/condition, and how you know>
   - [untested] <what> — <why>
   ```
   Add one line to CLAUDE.md: *a PR without "Expected change" and "Verification" is not ready*.
2. **Install hooks for real:** move to a committed `.githooks/pre-commit` (or `pre-commit install` in the setup docs and a CI check). Run `ruff` and a **staged-lines secrets scan** (gitleaks, or a regex for tokens/passwords and our sensitive filenames under `data/`). Bump ruff from v0.4.4.
3. **Branch protection on `main`:** require `test`, `ui-test` and `docker-test`. Merging on red then means deliberately overriding a rule, not an oversight.
4. **Hotspot report:** a ~40-line `scripts/hotspots.py` (lines × 60-day churn, test-file count) written to `doc/hotspots.md` monthly. It feeds G2.

### Medium (a few days)

5. **Live-failure → fixture rule.** For the browser tools (`ats_*`, `tw104`, `linkedin`), save the failing page HTML/JSON under `tests/fixtures/<tool>/` and add a unit test that replays the parser/planner on it. Make it a CLAUDE.md invariant next to "skip the job rather than guess": *a `fix(<tool>)` PR adds the fixture that reproduces it, or says why it can't*.
6. **Per-crew eval cases.** Move #139's 45 titles into `evals/tw104_relevance/` (input: title + filter, expected: keep/reject). Do the same for the ATS answer planner (`plan_questions`). Narrow the `skip.yaml` reason from "whole job type" to "browser part only". In `scripts/run_evals.py`, report *c/n* over 3 runs and compare against `baseline.json`.
7. **`pr-reviewer` subagent** (`.claude/agents/pr-reviewer.md`): input is only `PR #n`. The rubric seeds from our own incidents: SSOT drift (`spec.py` vs derived tables), a renamed field with readers on disk (`data/*.json` ledgers), concurrency/ledger races, credentials in diffs, "did verification hit the changed path". Output: confirmed/speculative × severity. Only confirmed HIGH+ blocks. A second blocked round → ask the human.

### Larger (only if the above sticks)

8. **Lightweight evidence ledger:** `scripts/evidence.py run --claim "…" -- <cmd>` appends `{id, claim, commit, cmd, exit, expected, log_path}` to `.agent/evidence.jsonl`, plus a `check-pr` that confirms every `E<n>` in the PR body exists and was recorded on the PR's head. Take only this core. Skip the staleness/fingerprint machinery.
9. **Merge gate hook** (PreToolUse on `gh pr merge`): allow only if CI is green on head **and** the `pr-reviewer` verdict for that head is pass.
10. **Monthly `/retro`:** list fix chains (≥3 `fix(<scope>)` within 48 h), what tool or check would have caught each one, and which checks never fired (prune them).

### What *not* to copy

- **The full harness.** Their process code has become their biggest hotspot and a cost center. Adopt the *contracts* (expected change, evidence reaches the path, independent reviewer, failures → tests), not the machinery (fingerprint caches, parent/child PR stacks, decision audits, workbench mod).
- **Rehearsal-scale runs** (1–3 h, $10–15 each). Our nightly evals + canaries + `ats_live_smoke.py` already cover that layer more cheaply.
- **Process for a single maintainer's habits.** Many of their rules exist to keep a human out of child-PR details. Ours has one reviewer, so keep it lean.

---

## 5. How we'd know it worked

| Metric | Now (60 d) | Target |
|---|---|---|
| `fix` : `feat` commits | 74 : 41 (1.8×) | < 1× |
| Fix chains (≥3 `fix(<same scope>)` in 48 h) | several (tw104, workday) | ≤ 1 / month |
| `fix(<tool>)` PRs that add a reproducing fixture/test | not tracked | ≥ 80 % |
| Prompt-change PRs with an eval *c/n* vs baseline | ~0 (one-off scripts) | 100 % |
| PRs merged with CI not green | not tracked | 0 |

Commands to recompute: `git log --since=60.days --no-merges --format=%s origin/main | sed -E 's/^([a-z]+).*/\1/' | sort | uniq -c`.

---

## Appendix: where to read in the reference repo

| Topic | Path in `junyiacademy/junyi-sw-agent` |
|---|---|
| Overall dev workflow | `docs/repo-workflow.md` |
| Work contract (ask vs decide, decision rules) | `.claude/skills/repo-work/SKILL.md`, `references/step.md`, `references/pr-description.md` |
| Which test for which change (S0–S3) | `.claude/skills/repo-verify/SKILL.md`, `docs/test-pyramid.md` |
| How to write LLM evals (c/n, Wilson, baseline) | `.claude/skills/testing/SKILL.md` |
| Independent reviewer rubric | `.claude/agents/repo-reviewer.md`, `.claude/skills/repo-work/references/review.md` |
| Merge gate | `.claude/hooks/merge_gate.py`, `docs/repo-workflow.md` § merge gate |
| Boundaries + who enforces them | `docs/boundaries.md`, `.claude/hooks/boundary_guard.py` |
| Learning loops | `.claude/skills/reflect/SKILL.md`, `.claude/skills/retro/SKILL.md` |
| Hotspots | `docs/architecture/12-hotspots.md` |
