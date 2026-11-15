"""
Verify email addresses without any paid service.

Stage 3 of the lead-collection funnel (see doc/email_collect):
    discover  →  extract email  →  VERIFY  →  dedupe

Three free layers, cheapest first:
  1. Syntax   — regex shape check.
  2. MX       — does the domain publish mail servers (dnspython)? Falls back to
                an A record (implicit MX) per RFC 5321.
  3. SMTP     — open a handshake to the MX and issue RCPT TO *without sending*.
                250 → mailbox accepted, 550 → rejected. Best-effort only: many
                hosts greylist or block port 25 from cloud IPs, so an
                inconclusive probe is reported as "unknown", never a failure.

Output feeds lead ranking: role address + MX + SMTP-accept → high confidence.
"""
import re
import smtplib
import socket

from crewai.tools import BaseTool
from pydantic import BaseModel

from src.automation import net_guard

_SYNTAX_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")
_ROLE_LOCALPARTS = (
    "info", "contact", "hello", "sales", "office", "enquiries", "enquiry",
    "inquiries", "admin", "support", "hi", "team", "mail", "business",
)
# Neutral identities for the SMTP probe (we never actually send).
_PROBE_HELO = "verifier.local"
_PROBE_FROM = "verify@example.com"
_SMTP_TIMEOUT = 8


class EmailVerifyInput(BaseModel):
    emails: list[str]
    smtp_check: bool = True


class EmailVerifyTool(BaseTool):
    name: str = "email_verify"
    description: str = (
        "Verify email addresses for free: syntax, MX-record lookup, and a "
        "best-effort SMTP RCPT probe (no mail sent). Returns per-email "
        "deliverability signals and a confidence label. Args: emails "
        "(list[str]), smtp_check (bool, default true)."
    )
    args_schema: type[BaseModel] = EmailVerifyInput

    def _run(self, emails: list[str], smtp_check: bool = True) -> dict:
        results = [verify_email(e, smtp_check=smtp_check) for e in emails]
        return {"results": results}


def verify_email(email: str, smtp_check: bool = True) -> dict:
    """Return deliverability signals for one address."""
    email = (email or "").strip().lower()
    out = {
        "email": email,
        "syntax_valid": False,
        "mx_found": False,
        "mx_host": "",
        "smtp_status": "skipped",   # accepted | rejected | unknown | skipped
        "is_role": _is_role(email),
        "confidence": "invalid",    # high | medium | low | invalid
    }
    if not _SYNTAX_RE.match(email):
        return out
    out["syntax_valid"] = True

    domain = email.split("@", 1)[1]
    mx_host = _lookup_mx(domain)
    out["mx_found"] = bool(mx_host)
    out["mx_host"] = mx_host or ""

    if out["mx_found"] and smtp_check:
        out["smtp_status"] = _smtp_probe(mx_host, email)

    out["confidence"] = _confidence(out)
    return out


def _is_role(email: str) -> bool:
    local = email.split("@", 1)[0] if "@" in email else email
    return any(local == r or local.startswith(r) for r in _ROLE_LOCALPARTS)


def _lookup_mx(domain: str) -> str | None:
    """Highest-priority MX host, or the domain itself if only an A record exists."""
    try:
        import dns.resolver

        answers = dns.resolver.resolve(domain, "MX", lifetime=6)
        records = sorted(answers, key=lambda r: r.preference)
        if records:
            return str(records[0].exchange).rstrip(".")
    except Exception:  # noqa: BLE001 — no MX / NXDOMAIN / timeout → try A record
        pass
    try:
        socket.gethostbyname(domain)
        return domain  # implicit MX (RFC 5321 §5.1)
    except Exception:  # noqa: BLE001
        return None


def _smtp_probe(mx_host: str, email: str) -> str:
    """Handshake to the MX and RCPT the address without sending. Best-effort."""
    try:
        # The MX comes from DNS for a domain scraped off a web page; a hostile
        # zone can point it at 127.0.0.1 or an intranet mail relay. Probe only a
        # public MX, and connect to the address that was checked (SSRF guard).
        mx_addr = net_guard.resolve_public(mx_host, 25)[0]
        with smtplib.SMTP(mx_addr, 25, timeout=_SMTP_TIMEOUT) as smtp:
            smtp.helo(_PROBE_HELO)
            smtp.mail(_PROBE_FROM)
            code, _ = smtp.rcpt(email)
            if code in (250, 251):
                return "accepted"
            if code in (550, 551, 553, 554):
                return "rejected"
            return "unknown"
    except Exception:  # noqa: BLE001 — port blocked / greylist / timeout
        return "unknown"


def _confidence(out: dict) -> str:
    if not out["syntax_valid"]:
        return "invalid"
    if out["smtp_status"] == "rejected":
        return "low"
    if not out["mx_found"]:
        return "low"
    if out["smtp_status"] == "accepted":
        return "high"
    # MX present, SMTP inconclusive: role addresses are safer bets.
    return "medium" if out["is_role"] else "low"
