from dataclasses import dataclass


@dataclass
class ValidationResult:
    valid: bool
    reason: str = ""


_MIN_CHARS = 20

_CHECKS: dict = {
    "google_form_fill":   lambda r: (r.get("submitted") is True, "form not submitted"),
    "email_sender":       lambda r: (r.get("sent") is True, "email not sent"),
    "pipeline":           lambda r: (bool(r.get("steps")), "pipeline completed no steps"),
    "google_sheet_reader": lambda r: (
        bool(r.get("columns") or r.get("data") or r.get("summary")),
        "no sheet data returned",
    ),
    "web_scraper":        lambda r: (
        bool(r.get("content") or r.get("title") or r.get("summary") or r.get("data")),
        "no content scraped",
    ),
    "hacker_news_digest": lambda r: (
        bool(r.get("stories") or r.get("digest") or r.get("items") or r.get("answer")),
        "no stories in result",
    ),
    "x_scraper":          lambda r: (
        bool(r.get("posts") or r.get("profile") or r.get("summary") or r.get("data")),
        "no profile data found",
    ),
    "shopee_seller_scraper": lambda r: (
        bool(r.get("sellers")),
        "no sellers found",
    ),
    "profit_health_check": lambda r: (
        bool(r.get("skus") or r.get("action_items") or r.get("recommendations")),
        "no profit analysis in result",
    ),
    "tasker_apply": lambda r: (
        isinstance(r.get("applied"), list) and r.get("cases_found") is not None,
        "no cases processed",
    ),
    "tw104_apply": lambda r: (
        isinstance(r.get("applied"), list) and r.get("jobs_found") is not None,
        "no jobs processed",
    ),
    "email_collect": lambda r: (
        r.get("discovered_count", 0) > 0,
        "no businesses discovered",
    ),
}


def validate(job_type: str, result: dict) -> ValidationResult:
    if not isinstance(result, dict):
        return ValidationResult(False, "result is not a dict")
    if "error" in result:
        return ValidationResult(False, f"result contains error: {result['error']}")

    check = _CHECKS.get(job_type)
    if check:
        passed, reason = check(result)
        if not passed:
            return ValidationResult(False, reason)

    content = " ".join(str(v) for v in result.values() if v)
    if len(content) < _MIN_CHARS:
        return ValidationResult(False, "result content too short")

    return ValidationResult(True)
