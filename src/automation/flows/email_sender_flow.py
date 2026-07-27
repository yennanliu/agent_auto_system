import json

from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel

from src.automation.flows.base import FlowMixin
from src.automation.progress import append_log
from src.automation.tools.gmail_send_tool import GmailSendTool


class EmailSenderState(BaseModel):
    to: str = ""
    subject: str = ""
    body: str = ""
    cc: str = ""
    bcc: str = ""
    run_id: int = 0
    usage: dict = {}
    llm_provider: str = ""
    llm_model: str = ""
    previous_error: str = ""


class EmailSenderFlow(FlowMixin, Flow[EmailSenderState]):

    @start()
    def validate_payload(self):
        self._check_required("to", "subject", "body")
        to_n = len([e for e in self.state.to.split(",") if e.strip()])
        bcc_n = len([e for e in self.state.bcc.split(",") if e.strip()])
        append_log(self.state.run_id,
                   f"Sending to {to_n} To + {bcc_n} Bcc recipient(s)")
        return self.state.model_dump()

    @listen(validate_payload)
    def send_email(self, _):
        append_log(self.state.run_id, "Connecting to Gmail SMTP...")
        tool = GmailSendTool()
        result = tool._run(
            to=self.state.to,
            subject=self.state.subject,
            body=self.state.body,
            cc=self.state.cc or None,
            bcc=self.state.bcc or None,
        )
        if isinstance(result, dict) and result.get("sent"):
            append_log(self.state.run_id, f"Email sent successfully to {self.state.to}")
        else:
            err = result.get("error", "Unknown error") if isinstance(result, dict) else str(result)
            append_log(self.state.run_id, f"Send failed: {err}")
        return json.dumps(result) if isinstance(result, dict) else str(result)
