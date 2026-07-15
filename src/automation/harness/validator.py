from dataclasses import dataclass

from src.automation import spec


@dataclass
class ValidationResult:
    valid: bool
    reason: str = ""


_MIN_CHARS = 20

# Per-job-type pass/fail rule, derived from the automation registry (SSOT).
# Each entry is `lambda result -> (passed: bool, reason: str)`; the reason is
# surfaced only on failure. See src/automation/spec.py.
_CHECKS: dict = spec.checks()


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
