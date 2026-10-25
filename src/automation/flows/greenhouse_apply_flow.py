import json

from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel

from src.automation.crews.greenhouse_answer_crew.crew import GreenhouseAnswerCrew
from src.automation.crews.greenhouse_relevance_crew.crew import GreenhouseRelevanceCrew
from src.automation.flows.base import FlowMixin
from src.automation.flows.utils import extract_usage
from src.automation.progress import append_log
from src.automation.tools.greenhouse_apply_tool import run_greenhouse_apply


def _parse_json_obj(text: str, key: str) -> dict | None:
    """Best-effort extract a JSON object containing ``key`` from LLM text.
    Tolerates ```json fences and surrounding prose; None if nothing usable."""
    if not text:
        return None
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start:end + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and key in obj:
            return obj
    return None


def _truthy(val) -> bool:
    if isinstance(val, str):
        return val.strip().lower() in ("true", "yes", "y", "1", "relevant", "match")
    return bool(val)


def _parse_verdict(text: str) -> dict | None:
    """{"relevant": bool, "reason": str} from LLM text (mirrors the 104 /
    LinkedIn apply flows' parser); None → caller fails open."""
    obj = _parse_json_obj(text, "relevant")
    if obj is not None:
        obj["relevant"] = _truthy(obj["relevant"])
    return obj


def _parse_answer(text: str) -> str:
    """The answer from {"answer": str, "confident": bool}, or "" when the model
    abstained, wasn't confident, or the output is unparseable."""
    obj = _parse_json_obj(text, "answer")
    if obj is None or not _truthy(obj.get("confident", False)):
        return ""
    return str(obj.get("answer") or "").strip()


def _names_unmentioned_employer(question: str, job: dict, background: str) -> bool:
    """True when a question is about this employer ("Why do you want to join
    Figma?") but the candidate's background never mentions it. The answerer
    would otherwise borrow motivation written about a different company, so such
    questions stay unanswered (the job is skipped) without asking the LLM."""
    q, bg = question.lower(), background.lower()
    names = {n.strip().lower() for n in (job.get("company", ""), job.get("token", "")) if len(n.strip()) >= 3}
    return any(n in q for n in names) and not any(n in bg for n in names)


class GreenhouseApplyState(BaseModel):
    boards: str = ""                # board tokens / URLs, comma-separated (required)
    keywords: str = ""              # title filter, comma-separated phrases (blank = all)
    location: str = ""              # job-location filter, comma-separated (blank = any)
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    resume_path: str = ""           # PDF/DOCX on this server (default GREENHOUSE_RESUME_PATH)
    current_location: str = ""      # answers "where are you based?" / location autocomplete
    linkedin_url: str = ""
    github_url: str = ""
    website_url: str = ""
    cover_letter: str = ""          # uploaded as a .txt when the form has a cover-letter field
    years_experience: int = 3
    salary_expectation: str = ""
    work_authorized: bool = True
    needs_sponsorship: bool = False
    authorized_countries: str = ""  # e.g. "Taiwan" — country-named questions answer from this
    school: str = ""                # Education block (boards that require it)
    degree: str = ""                # e.g. "Master's Degree"
    discipline: str = ""
    current_company: str = ""       # Employment block (boards that require it)
    current_title: str = ""
    current_start: str = ""         # e.g. "2025-06"
    extra_answers: str = ""         # "keyword: answer" lines for recurring custom questions
    candidate_background: str = ""  # enables LLM answers to required screening questions
    task_filter: str = ""           # optional 2nd gate: LLM relevance filter (nat-lang)
    max_applications: int = 5
    max_per_board: int = 0          # cap per company (0 = no cap)
    max_scans_per_board: int = 0    # jobs examined per company (0 = no cap)
    dry_run: bool = True            # safety: don't click Submit unless explicitly false
    run_id: int = 0
    usage: dict = {}
    llm_provider: str = ""
    llm_model: str = ""
    previous_error: str = ""


class GreenhouseApplyFlow(FlowMixin, Flow[GreenhouseApplyState]):

    @start()
    def validate_payload(self):
        self._check_required("boards", "first_name", "last_name", "email")
        append_log(
            self.state.run_id,
            f"Payload validated — boards '{self.state.boards}', "
            f"keywords '{self.state.keywords or 'any'}', "
            f"location '{self.state.location or 'any'}', "
            f"max {self.state.max_applications} application(s), "
            f"dry_run={self.state.dry_run}",
        )
        return self.state.model_dump()

    @listen(validate_payload)
    def execute_apply(self, _):
        usage_acc = {"prompt_tokens": 0, "completion_tokens": 0}

        # The LLM powers two optional steps — the relevance gate and screening
        # answers. It's resolved lazily, so a run using neither makes no LLM call.
        llm_box: dict = {}

        def _get_llm():
            if "llm" not in llm_box:
                llm = None
                try:
                    from src.automation.harness.provider import resolve as resolve_llm
                    llm, _p, _m = resolve_llm(
                        self.state.llm_provider or None,
                        self.state.llm_model or None,
                        temperature=0.2,
                    )
                except Exception as exc:  # noqa: BLE001
                    append_log(self.state.run_id, f"No LLM available ({exc}).")
                llm_box["llm"] = llm
            return llm_box["llm"]

        def _acc_usage(result) -> None:
            u = extract_usage(result)
            usage_acc["prompt_tokens"] += u.get("prompt_tokens", 0)
            usage_acc["completion_tokens"] += u.get("completion_tokens", 0)

        # ── Relevance gate (optional second filter before applying) ──
        task_filter = (self.state.task_filter or "").strip()
        relevance_fn = None
        if task_filter:
            llm = _get_llm()
            if llm is None:
                append_log(self.state.run_id,
                           "task_filter is set but no LLM is available; "
                           "applying to all matching jobs.")
            else:
                append_log(self.state.run_id,
                           "Second gate active: filtering jobs by task_filter before applying.")

                def relevance_fn(title: str, meta: str) -> tuple[bool, str]:  # noqa: F811
                    try:
                        result = GreenhouseRelevanceCrew(llm=llm).crew().kickoff(inputs={
                            "task_filter": task_filter,
                            "job_title": title,
                            "job_meta": meta,
                        })
                        _acc_usage(result)
                        text = (result.raw if hasattr(result, "raw") else str(result)) or ""
                        verdict = _parse_verdict(text)
                        if verdict is None:
                            append_log(self.state.run_id,
                                       f"Relevance verdict unparseable ({text[:80]!r}); "
                                       "keeping job (fail-open).")
                            return True, ""
                        return bool(verdict.get("relevant")), str(verdict.get("reason") or "")
                    except Exception as exc:  # noqa: BLE001 — never fail a job on the gate
                        append_log(self.state.run_id,
                                   f"Relevance judge failed ({exc}); keeping job (fail-open).")
                        return True, ""

        # ── Screening answers (only with the candidate's own background) ──
        # Without background text the model could only invent facts, so the
        # answerer stays off and unanswerable required questions skip the job.
        background = (self.state.candidate_background or "").strip()
        answer_fn = None
        if background:
            llm = _get_llm()
            if llm is not None:
                append_log(self.state.run_id,
                           "Screening answerer active: required questions the rules "
                           "miss are answered from candidate_background.")
                profile_txt = "\n".join(f"{k}: {v}" for k, v in {
                    "name": f"{self.state.first_name} {self.state.last_name}".strip(),
                    "based in": self.state.current_location,
                    "years of experience": self.state.years_experience,
                    "authorized to work": self.state.work_authorized,
                    "needs visa sponsorship": self.state.needs_sponsorship,
                    "authorized to work only in": self.state.authorized_countries,
                    "salary expectation": self.state.salary_expectation,
                }.items() if v not in ("", None))

                def answer_fn(label: str, kind: str, options: list[str], job: dict) -> str:  # noqa: F811
                    if _names_unmentioned_employer(label, job, background):
                        return ""
                    try:
                        result = GreenhouseAnswerCrew(llm=llm).crew().kickoff(inputs={
                            "job_title": job.get("title", ""),
                            "company": job.get("company", ""),
                            "job_location": job.get("location", "") or "unspecified",
                            "question": label,
                            "field_kind": "select" if kind == "select" else "free text",
                            "options": "\n".join(f"- {o}" for o in options) or "(free text)",
                            "candidate_profile": profile_txt,
                            "candidate_background": background,
                        })
                        _acc_usage(result)
                        return _parse_answer(
                            (result.raw if hasattr(result, "raw") else str(result)) or "")
                    except Exception as exc:  # noqa: BLE001
                        append_log(self.state.run_id,
                                   f"Screening answerer failed ({exc}); leaving unanswered.")
                        return ""

        result = run_greenhouse_apply(
            boards=self.state.boards,
            keywords=self.state.keywords,
            location=self.state.location,
            first_name=self.state.first_name,
            last_name=self.state.last_name,
            email=self.state.email,
            phone=self.state.phone,
            resume_path=self.state.resume_path,
            current_location=self.state.current_location,
            linkedin_url=self.state.linkedin_url,
            github_url=self.state.github_url,
            website_url=self.state.website_url,
            cover_letter=self.state.cover_letter,
            years_experience=self.state.years_experience,
            salary_expectation=self.state.salary_expectation,
            work_authorized=self.state.work_authorized,
            needs_sponsorship=self.state.needs_sponsorship,
            authorized_countries=self.state.authorized_countries,
            school=self.state.school, degree=self.state.degree, discipline=self.state.discipline,
            current_company=self.state.current_company, current_title=self.state.current_title,
            current_start=self.state.current_start,
            extra_answers=self.state.extra_answers,
            max_applications=self.state.max_applications,
            max_per_board=self.state.max_per_board,
            max_scans_per_board=self.state.max_scans_per_board,
            dry_run=self.state.dry_run,
            relevance_fn=relevance_fn,
            answer_fn=answer_fn,
            log=lambda m: append_log(self.state.run_id, m),
        )

        self.state.usage = usage_acc
        append_log(self.state.run_id, "Greenhouse apply run complete, formatting result...")
        return json.dumps(result, ensure_ascii=False)
