"""A job-tailored, plain-text résumé for forms that accept a pasted one.

Greenhouse forms offer "Enter manually" beside the résumé upload (the
``resume_text`` field). With ``tailor_resume`` on, the planner asks the run's
``tailor(job_data) -> str`` for the candidate's résumé rewritten toward that
job's description and pastes it instead of uploading the file.

Every failure falls back to the original behaviour — upload the file: no
readable résumé text, no job description, an LLM error, or an answer that fails
``accept()`` (too short, or states a number the original résumé doesn't).
"""
import html
import os
import re
from collections.abc import Callable

from src.automation.tools.ats_common import off_loop

MAX_JD_CHARS = 6000
MAX_RESUME_CHARS = 12000


def _normalize(text: str) -> str:
    lines = (re.sub(r"[ \t ]+", " ", ln).strip() for ln in (text or "").splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def load_resume_text(path: str) -> str:
    """Plain text of a .txt / .md / .pdf résumé; "" when it can't be read."""
    ext = os.path.splitext(path or "")[1].lower()
    try:
        if ext in (".txt", ".md"):
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        elif ext == ".pdf":
            from pypdf import PdfReader
            text = "\n".join(p.extract_text() or "" for p in PdfReader(path).pages)
        else:
            return ""
    except Exception:  # noqa: BLE001 — unreadable → caller uploads the file instead
        return ""
    return _normalize(text)[:MAX_RESUME_CHARS]


def job_description(job_data: dict) -> str:
    """The posting's description as plain text (the Greenhouse API returns it
    as entity-escaped HTML)."""
    raw = html.unescape(job_data.get("content") or "")
    raw = re.sub(r"<\s*(br|/p|/ul|/ol|/h\d|/div)[^>]*>", "\n", raw, flags=re.I)
    raw = re.sub(r"<\s*li[^>]*>", "\n- ", raw, flags=re.I)
    return _normalize(html.unescape(re.sub(r"<[^>]+>", " ", raw)))[:MAX_JD_CHARS]


_FENCE = re.compile(r"^```[a-z]*\n|\n?```$", re.I)
_NUMBER = re.compile(r"\d+")


def accept(tailored: str, original: str) -> tuple[str, str]:
    """``(text, "")`` when the tailored résumé is safe to send, else ``("", why)``.

    The LLM may reorder, trim and reword — never add facts. Numbers are the
    facts that are cheap to check: a year, a percentage or a "5+ years" the
    original doesn't contain means something was invented."""
    text = _normalize(_FENCE.sub("", (tailored or "").strip()))
    if not text:
        return "", "empty answer"
    if len(text) < 0.4 * len(original):
        return "", f"too short ({len(text)} of {len(original)} chars)"
    invented = sorted(set(_NUMBER.findall(text)) - set(_NUMBER.findall(original)))
    if invented:
        return "", "numbers not in the original résumé: " + ", ".join(invented[:5])
    return text, ""


def bind_tailor(tailor_fn: Callable[[str, dict], str] | None, resume_path: str,
                log: Callable[[str], None], warnings: list[str]) -> Callable[[dict], str] | None:
    """``tailor_fn(resume_text, job_data)`` bound to the résumé's text, ready
    for ``profile["tailor_resume"]``; None (file upload only) when tailoring is
    off or the résumé has no readable text."""
    if tailor_fn is None:
        return None
    resume_text = load_resume_text(resume_path)
    if not resume_text:
        msg = (f"tailor_resume: no text could be read from '{os.path.basename(resume_path or '')}' "
               "(use a text-based .pdf, .txt or .md) — uploading the file as is")
        warnings.append(msg)
        log(f"⚠ {msg}")
        return None
    log(f"tailor_resume on: forms with a paste-résumé option get a JD-tailored version "
        f"({len(resume_text)} chars of résumé text)")
    return lambda job_data: tailor_fn(resume_text, job_data)


def tailored_text(profile: dict, job_data: dict) -> str:
    """The tailored résumé for this job, or "" to keep the file upload. Never raises."""
    tailor = profile.get("tailor_resume")
    if tailor is None:
        return ""
    try:
        return (off_loop(tailor, job_data) or "").strip()
    except Exception:  # noqa: BLE001 — tailoring must never cost the application
        return ""
