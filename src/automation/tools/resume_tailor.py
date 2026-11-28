"""A job-tailored, plain-text résumé for forms that accept a pasted one.

Greenhouse forms offer "Enter manually" beside the résumé upload (the
``resume_text`` field). With ``tailor_resume`` on, the planner asks the run's
``tailor(job_data) -> str`` for the candidate's résumé rewritten toward that
job's description and pastes it instead of uploading the file.

Résumé text comes from pypdf (or .txt / .md); a PDF without a text layer is
transcribed once per run by the run's LLM when it can read PDFs.

Every failure falls back to the original behaviour — upload the file: no
readable résumé text, no job description, an LLM error, or an answer that fails
``accept()`` (too short, or states a number the original résumé doesn't).
"""
import html
import os
import re
from collections.abc import Callable
from dataclasses import dataclass

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


@dataclass(frozen=True)
class Tailor:
    """A run's LLM steps for tailoring, built by the flow.

    ``rewrite(resume_text, job_data)`` returns the tailored résumé ("" to keep
    the file). ``read_pdf(path)`` transcribes a PDF pypdf gets no text from (a
    scanned / image-only résumé); None when the run's model can't read PDFs."""
    rewrite: Callable[[str, dict], str]
    read_pdf: Callable[[str], str] | None = None


def _is_candidates(text: str, profile: dict) -> bool:
    """The transcript names the candidate (e-mail, or first + last name). A
    model that never received the PDF (some clients drop attachments silently)
    can only invent a résumé — and it won't contain these."""
    t = text.lower()
    email = (profile.get("email") or "").strip().lower()
    first, last = ((profile.get(k) or "").strip().lower() for k in ("first_name", "last_name"))
    return bool(email and email in t) or bool(first and last and first in t and last in t)


def _read_pdf_with_llm(tailor: Tailor, path: str, profile: dict,
                       log: Callable[[str], None]) -> str:
    """The résumé PDF transcribed by the run's LLM, once per run; "" on any
    failure or when the transcript isn't recognisably the candidate's."""
    if tailor.read_pdf is None or os.path.splitext(path or "")[1].lower() != ".pdf":
        return ""
    log("Résumé PDF has no text layer — asking the LLM to read it (once per run)...")
    try:
        text = _normalize(off_loop(tailor.read_pdf, path) or "")[:MAX_RESUME_CHARS]
    except Exception as exc:  # noqa: BLE001
        log(f"⚠ LLM could not read the résumé PDF ({type(exc).__name__}: {exc})")
        return ""
    if not _is_candidates(text, profile):
        log("⚠ LLM transcript of the résumé doesn't name the candidate — discarded")
        return ""
    return text


def bind_tailor(tailor: Tailor | None, resume_path: str, profile: dict,
                log: Callable[[str], None], warnings: list[str]) -> Callable[[dict], str] | None:
    """``tailor.rewrite`` bound to the résumé's text, ready for
    ``profile["tailor_resume"]``; None (file upload only) when tailoring is off
    or no text can be had from the résumé — by pypdf, or else the LLM."""
    if tailor is None:
        return None
    resume_text = (load_resume_text(resume_path)
                   or _read_pdf_with_llm(tailor, resume_path, profile, log))
    if not resume_text:
        msg = (f"tailor_resume: no text could be read from '{os.path.basename(resume_path or '')}' "
               "(use a .pdf, .txt or .md; a scanned PDF needs a model that reads PDFs) "
               "— uploading the file as is")
        warnings.append(msg)
        log(f"⚠ {msg}")
        return None
    log(f"tailor_resume on: forms with a paste-résumé option get a JD-tailored version "
        f"({len(resume_text)} chars of résumé text)")
    return lambda job_data: tailor.rewrite(resume_text, job_data)


def tailored_text(profile: dict, job_data: dict) -> str:
    """The tailored résumé for this job, or "" to keep the file upload. Never raises."""
    tailor = profile.get("tailor_resume")
    if tailor is None:
        return ""
    try:
        return (off_loop(tailor, job_data) or "").strip()
    except Exception:  # noqa: BLE001 — tailoring must never cost the application
        return ""
