"""Unit tests for EmailSenderFlow — validate inputs and direct tool invocation."""
import json
from unittest.mock import MagicMock

import pytest

# ── Validation ────────────────────────────────────────────────────────────────

def test_raises_on_missing_to():
    from src.automation.flows.email_sender_flow import EmailSenderFlow
    with pytest.raises(Exception):
        EmailSenderFlow().kickoff(inputs={"to": "", "subject": "Hi", "body": "Hello"})


def test_raises_on_missing_subject():
    from src.automation.flows.email_sender_flow import EmailSenderFlow
    with pytest.raises(Exception):
        EmailSenderFlow().kickoff(inputs={"to": "a@b.com", "subject": "", "body": "Hello"})


def test_raises_on_missing_body():
    from src.automation.flows.email_sender_flow import EmailSenderFlow
    with pytest.raises(Exception):
        EmailSenderFlow().kickoff(inputs={"to": "a@b.com", "subject": "Hi", "body": ""})


# ── Direct tool call ──────────────────────────────────────────────────────────

def _make_send_mock(sent: bool = True):
    mock = MagicMock()
    mock._run.return_value = {
        "sent": sent,
        "from": "sender@gmail.com",
        "to": "r@x.com",
        "cc": "",
        "subject": "Hello",
        "confirmation": "Email sent successfully to r@x.com",
    }
    return mock


def test_flow_calls_gmail_tool_directly(mocker):
    mock_tool = _make_send_mock()
    mocker.patch("src.automation.flows.email_sender_flow.GmailSendTool", return_value=mock_tool)

    from src.automation.flows.email_sender_flow import EmailSenderFlow
    EmailSenderFlow().kickoff(inputs={
        "to": "r@x.com",
        "subject": "Hello",
        "body": "World",
    })

    mock_tool._run.assert_called_once()


def test_flow_passes_correct_to_and_subject(mocker):
    mock_tool = _make_send_mock()
    mocker.patch("src.automation.flows.email_sender_flow.GmailSendTool", return_value=mock_tool)

    from src.automation.flows.email_sender_flow import EmailSenderFlow
    EmailSenderFlow().kickoff(inputs={
        "to": "alice@x.com",
        "subject": "My Subject",
        "body": "Body text",
    })

    call_kwargs = mock_tool._run.call_args
    kwargs = call_kwargs.kwargs if call_kwargs.kwargs else {}
    args = call_kwargs.args if call_kwargs.args else ()
    # accept both positional and keyword call styles
    called_to = kwargs.get("to") or (args[0] if args else None)
    called_subject = kwargs.get("subject") or (args[1] if len(args) > 1 else None)
    assert called_to == "alice@x.com"
    assert called_subject == "My Subject"


def test_flow_passes_cc_when_provided(mocker):
    mock_tool = _make_send_mock()
    mocker.patch("src.automation.flows.email_sender_flow.GmailSendTool", return_value=mock_tool)

    from src.automation.flows.email_sender_flow import EmailSenderFlow
    EmailSenderFlow().kickoff(inputs={
        "to": "r@x.com",
        "subject": "S",
        "body": "B",
        "cc": "cc@x.com",
    })

    call_kwargs = mock_tool._run.call_args
    # cc should be passed as keyword argument
    assert call_kwargs.kwargs.get("cc") == "cc@x.com"


def test_flow_passes_bcc_when_provided(mocker):
    mock_tool = _make_send_mock()
    mocker.patch("src.automation.flows.email_sender_flow.GmailSendTool", return_value=mock_tool)

    from src.automation.flows.email_sender_flow import EmailSenderFlow
    EmailSenderFlow().kickoff(inputs={
        "to": "r@x.com", "subject": "S", "body": "B",
        "bcc": "a@x.com,b@x.com",
    })

    assert mock_tool._run.call_args.kwargs.get("bcc") == "a@x.com,b@x.com"


def test_tool_bcc_in_envelope_not_headers(mocker):
    """BCC must reach the SMTP envelope but never appear in the message headers."""
    from src.automation.tools.gmail_send_tool import GmailSendTool
    mocker.patch.dict("os.environ",
                      {"GMAIL_ADDRESS": "me@gmail.com", "GMAIL_APP_PASSWORD": "x"})
    smtp = MagicMock()
    ctx = mocker.patch("smtplib.SMTP")
    ctx.return_value.__enter__.return_value = smtp

    out = GmailSendTool()._run(to="me@gmail.com", subject="S", body="B",
                               bcc="hidden1@x.com,hidden2@x.com")

    assert out["sent"] is True and out["bcc_count"] == 2
    envelope_recipients = smtp.sendmail.call_args.args[1]
    assert "hidden1@x.com" in envelope_recipients and "hidden2@x.com" in envelope_recipients
    raw_message = smtp.sendmail.call_args.args[2]
    assert "hidden1@x.com" not in raw_message  # blind: not in headers/body


def test_flow_result_is_json_string(mocker):
    mock_tool = _make_send_mock(sent=True)
    mocker.patch("src.automation.flows.email_sender_flow.GmailSendTool", return_value=mock_tool)

    from src.automation.flows.email_sender_flow import EmailSenderFlow
    result = EmailSenderFlow().kickoff(inputs={"to": "r@x.com", "subject": "S", "body": "B"})

    raw = result.raw if hasattr(result, "raw") else str(result)
    parsed = json.loads(raw)
    assert parsed["sent"] is True


def test_flow_failed_send_returns_sent_false(mocker):
    mock_tool = _make_send_mock(sent=False)
    mock_tool._run.return_value = {
        "sent": False,
        "error": "GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set in .env",
    }
    mocker.patch("src.automation.flows.email_sender_flow.GmailSendTool", return_value=mock_tool)

    from src.automation.flows.email_sender_flow import EmailSenderFlow
    result = EmailSenderFlow().kickoff(inputs={"to": "r@x.com", "subject": "S", "body": "B"})

    raw = result.raw if hasattr(result, "raw") else str(result)
    parsed = json.loads(raw)
    assert parsed["sent"] is False
