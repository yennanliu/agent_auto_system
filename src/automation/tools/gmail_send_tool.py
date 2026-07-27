"""
Gmail send tool — ported from agent_office_system/tools/gmail_tool.py.
Sends email via Gmail SMTP using an app password (no OAuth required).

Setup:
  1. Enable 2-Step Verification on your Google account.
  2. Generate an App Password at https://myaccount.google.com/apppasswords
  3. Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env
"""
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 587


def _parse_emails(s: str) -> list[str]:
    return [r.strip() for r in s.split(",") if r.strip()]


class SendEmailInput(BaseModel):
    to: str = Field(description="Recipient email address(es), comma-separated")
    subject: str = Field(description="Email subject line")
    body: str = Field(description="HTML or plain-text email body")
    cc: str | None = Field(default=None, description="CC recipients, comma-separated")
    bcc: str | None = Field(default=None, description="BCC recipients, comma-separated")


class GmailSendTool(BaseTool):
    name: str = "gmail_send_email"
    description: str = (
        "Send an email via Gmail SMTP. "
        "Provide recipient(s), subject, and body (HTML or plain text). "
        "Optionally include CC recipients."
    )
    args_schema: type[BaseModel] = SendEmailInput

    def _run(
        self,
        to: str,
        subject: str,
        body: str,
        cc: str | None = None,
        bcc: str | None = None,
    ) -> dict:
        gmail_address = os.environ.get("GMAIL_ADDRESS", "").strip()
        app_password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()

        if not gmail_address or not app_password:
            return {
                "sent": False,
                "error": "GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set in .env",
            }

        msg = MIMEMultipart("alternative")
        msg["From"] = gmail_address
        msg["To"] = to
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = cc
        # NB: Bcc is deliberately NOT written to the headers — that's what makes
        # it blind. The addresses only go into the SMTP envelope below, so no
        # recipient sees the others (the point of a BCC blast).

        # Attach as HTML if it looks like HTML, otherwise plain text
        mime_type = "html" if "<" in body and ">" in body else "plain"
        msg.attach(MIMEText(body, mime_type))

        recipients = (_parse_emails(to) + (_parse_emails(cc) if cc else [])
                      + (_parse_emails(bcc) if bcc else []))

        try:
            with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT) as server:
                server.ehlo()
                server.starttls()
                server.login(gmail_address, app_password)
                server.sendmail(gmail_address, recipients, msg.as_string())
        except Exception as exc:
            return {"sent": False, "error": f"SMTP error: {exc}"}

        return {
            "sent": True,
            "from": gmail_address,
            "to": to,
            "cc": cc or "",
            "bcc_count": len(_parse_emails(bcc)) if bcc else 0,
            "subject": subject,
            "confirmation": f"Email sent to {len(recipients)} recipient(s)",
        }
