from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    is_admin: bool = False
    is_active: bool = True
    allowed_automations: str = "[]"  # JSON list of job_type; "*" = all
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_login_at: datetime | None = None


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)  # e.g. "llm_key:openai", "enabled_automations"
    value: str  # JSON string; API-key values are Fernet-encrypted
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Job(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    job_type: str = "google_form_fill"
    payload: str  # JSON string
    schedule: str | None = None  # cron expression, e.g. "0 8 * * *"; None = manual only
    created_by: int | None = Field(default=None, foreign_key="user.id")  # owner (scheduled-run attribution)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Run(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id")
    user_id: int | None = Field(default=None, foreign_key="user.id")  # who triggered it
    status: str = "pending"  # pending | running | success | failed
    result: str | None = None  # JSON string
    log: str | None = None  # JSON array of {ts, msg} progress entries
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    # Harness fields
    llm_provider: str | None = None
    llm_model: str | None = None            # model requested (before fallback)
    served_model: str | None = None         # model that actually produced the result
    fallback_used: bool = False             # a cross-model fallback fired
    models_attempted: int = 1               # distinct models tried before success
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    retry_count: int = 0
    duration_secs: float | None = None      # wall-clock; stored to enable p50/p95
    # Quality gate (validator.py) outcome
    validation_passed: bool | None = None   # None = validation never ran (hard error)
    validation_reason: str | None = None    # why it failed the gate
    # Evaluation fields (LLM-as-judge quality score; informational only)
    eval_score: float | None = None        # 0-100 quality score
    eval_confidence: float | None = None    # 0-1 confidence in the score
    eval_notes: str | None = None           # short rationale
    eval_method: str | None = None          # "llm" | "heuristic"
