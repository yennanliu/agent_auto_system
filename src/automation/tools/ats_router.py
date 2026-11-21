"""
Which applicant-tracking system a pasted board / job reference belongs to.

``parse_board_ref`` turns whatever the user typed into a ``BoardRef``:

  * a board or job URL on any supported ATS host
    (``jobs.lever.co/palantir``, ``jobs.ashbyhq.com/openai/<uuid>``,
    ``apply.workable.com/huggingface/j/<SHORTCODE>``, …), or
  * ``platform:company`` shorthand (``lever:palantir``), or
  * a bare token, which is ambiguous and so needs ``default_platform``.

``detect_ats(url)`` is the URL-only half, used to route job links found by
discovery sources (job boards, aggregators) to the right apply adapter.

Workday boards are identified by ``<tenant>.<wdN>/<site>`` (the API needs all
three): ``workday:ngc.wd1/Northrop_Grumman_External_Site``. Its jobs are
discovered but applied to by hand (a separate account per company).
"""
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

SUPPORTED = ("greenhouse", "lever", "ashby", "workable", "smartrecruiters", "workday", "recruitee",
             "personio", "bamboohr", "teamtailor")
KNOWN = SUPPORTED

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_WORKABLE_SHORTCODE_RE = re.compile(r"^[A-Z0-9]{6,12}$")
_WORKABLE_RESERVED = {"www", "apply", "jobs", "help", "resources", "app", "api", "blog",
                      "careers", "status", "developers", "marketplace", "id"}
# Recruitee's own subdomains, not employer careers sites.
_RECRUITEE_RESERVED = {"www", "app", "api", "help", "support", "blog", "careers", "status",
                       "developers", "marketplace", "partners", "integrations", "docs"}
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True)
class BoardRef:
    """One employer board on one ATS, optionally narrowed to a single job.

    ``company`` is the platform's board token (Lever site name, Ashby jobs-page
    name, Workable account subdomain, SmartRecruiters company identifier);
    ``job_id`` is "" for a whole board. ``region`` is "eu" for Lever's EU
    instance (jobs.eu.lever.co), "" otherwise."""
    platform: str
    company: str
    job_id: str = ""
    region: str = ""

    @property
    def supported(self) -> bool:
        return self.platform in SUPPORTED


def _host(u) -> str:
    return (u.netloc or "").lower().split(":")[0]


def _greenhouse(ref: str) -> BoardRef | None:
    from src.automation.tools.greenhouse_apply_tool import parse_board_ref as gh_parse
    parsed = gh_parse(ref)
    return BoardRef("greenhouse", parsed[0], parsed[1]) if parsed else None


def detect_ats(url: str) -> BoardRef | None:
    """The ATS board / job a URL points at, or None for any other site."""
    url = (url or "").strip()
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url
    u = urlparse(url)
    host = _host(u)
    parts = [p for p in u.path.split("/") if p]
    q = parse_qs(u.query)

    if host == "greenhouse.io" or host.endswith(".greenhouse.io"):  # not evilgreenhouse.io
        return _greenhouse(url)

    if host in ("jobs.lever.co", "jobs.eu.lever.co"):
        if not parts or not _TOKEN_RE.match(parts[0]):
            return None
        job = parts[1] if len(parts) > 1 and _UUID_RE.match(parts[1]) else ""
        return BoardRef("lever", parts[0].lower(), job.lower(), "eu" if ".eu." in host else "")

    if host == "jobs.ashbyhq.com":
        if not parts or not _TOKEN_RE.match(parts[0]):
            return None
        job = parts[1] if len(parts) > 1 and _UUID_RE.match(parts[1]) else ""
        # Kept as written (Ashby's API matches the page name case-insensitively).
        return BoardRef("ashby", parts[0], job.lower())

    if host.endswith(".recruitee.com") and host.count(".") == 2:
        # <company>.recruitee.com[/o/<offer slug>[/c/new]]
        company = host.split(".")[0]
        if company in _RECRUITEE_RESERVED or not _TOKEN_RE.match(company):
            return None
        if len(parts) > 1 and parts[0] == "o":
            # A job link with a bad slug is refused, never widened to the
            # whole board (PR review).
            if not _SLUG_RE.match(parts[1].lower()):
                return None
            return BoardRef("recruitee", company, parts[1].lower())
        return BoardRef("recruitee", company)

    if re.fullmatch(r"[a-z0-9-]+\.jobs\.personio\.(de|com)", host):
        # <company>.jobs.personio.de[/job/<id>[/apply]]
        company = host.split(".")[0]
        if len(parts) > 1 and parts[0] == "job":
            return BoardRef("personio", company, parts[1]) if parts[1].isdigit() else None
        return BoardRef("personio", company)

    if host.endswith(".teamtailor.com") and host.count(".") == 2:
        # <company>.teamtailor.com[/jobs/<id>-<slug>]
        company = host.split(".")[0]
        if company in ("www", "app", "api", "help", "support", "career", "status", "partners") \
                or not _TOKEN_RE.match(company):
            return None
        if len(parts) > 1 and parts[0] == "jobs":
            m = re.match(r"(\d+)(?:-|$)", parts[1])
            return BoardRef("teamtailor", company, m.group(1)) if m else None
        return BoardRef("teamtailor", company)

    if host.endswith(".bamboohr.com") and host.count(".") == 2:
        # <company>.bamboohr.com/careers[/<id>]
        company = host.split(".")[0]
        if company in ("www", "app", "api", "help", "partners", "marketplace") or not parts or parts[0] != "careers":
            return None
        if len(parts) > 1:
            return BoardRef("bamboohr", company, parts[1]) if parts[1].isdigit() else None
        return BoardRef("bamboohr", company)

    if host == "apply.workable.com":
        if not parts or not _TOKEN_RE.match(parts[0]) or parts[0] in ("api", "j"):
            return None
        job = parts[2] if len(parts) > 2 and parts[1] == "j" else ""
        return BoardRef("workable", parts[0].lower(), job.upper() if _WORKABLE_SHORTCODE_RE.match(job.upper()) else "")
    if host.endswith(".workable.com") and host.count(".") == 2:
        # Legacy <account>.workable.com/j/<SHORTCODE>
        account = host.split(".")[0]
        # Workable's own subdomains, not employer accounts (jobs.workable.com is
        # its job-seeker search site).
        if account in _WORKABLE_RESERVED:
            return None
        job = parts[1] if len(parts) > 1 and parts[0] in ("j", "jobs") else ""
        return BoardRef("workable", account, job.upper() if _WORKABLE_SHORTCODE_RE.match(job.upper()) else "")

    if host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com"):
        if parts and parts[0] == "oneclick-ui":
            # Decoded query value: validate like every other token, so "%2F" /
            # "%3F" can't add path or query to the API request built from it.
            company = (q.get("dcr_ci") or [""])[0]
            return BoardRef("smartrecruiters", company) if _TOKEN_RE.match(company) else None
        if not parts or not _TOKEN_RE.match(parts[0]):
            return None
        m = re.match(r"^(\d{6,})", parts[1]) if len(parts) > 1 else None
        return BoardRef("smartrecruiters", parts[0], m.group(1) if m else "")

    wd = _workday(host, parts)
    if wd:
        return wd

    return None


_WD_HOST = re.compile(r"^([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com$")
_WD_SITE_HOST = re.compile(r"^(wd\d+)\.myworkdaysite\.com$")
_LANG = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
_WD_TOKEN = re.compile(r"^([a-z0-9-]+)\.(wd\d+)/([A-Za-z0-9_.-]+)$")


def _workday(host: str, parts: list[str]) -> BoardRef | None:
    """``<tenant>.<wdN>.myworkdayjobs.com/[<lang>/]<site>[/job/<loc>/<slug>]``
    or ``<wdN>.myworkdaysite.com/recruiting/<tenant>/<site>/…`` (same API)."""
    if m := _WD_HOST.match(host):
        tenant, wd = m.groups()
        rest = parts[1:] if parts and _LANG.match(parts[0]) else parts
    elif (m := _WD_SITE_HOST.match(host)) and len(parts) >= 3 and parts[0] == "recruiting":
        tenant, wd = parts[1].lower(), m.group(1)
        rest = parts[2:]
    else:
        return None
    if not rest or not _TOKEN_RE.match(rest[0]) or rest[0] in ("wday", "job"):
        return None
    site = rest[0]
    job = rest[-1] if "job" in rest[1:] and rest[-1] != "job" else ""
    return BoardRef("workday", f"{tenant}.{wd}/{site}", job)


def parse_board_ref(ref: str, default_platform: str = "") -> BoardRef | None:
    """A ``BoardRef`` from a URL, ``platform:token`` shorthand, or a bare token
    (only with ``default_platform``). None when it can't be resolved."""
    ref = (ref or "").strip().strip("/")
    if not ref:
        return None
    if ref.startswith("workday:"):
        # workday:<tenant>.<wdN>/<site>[/<job slug>]
        token, _, job = ref[len("workday:"):].partition("/")
        site, _, job = job.partition("/")
        m = _WD_TOKEN.match(f"{token}/{site}")
        return BoardRef("workday", f"{m.group(1)}.{m.group(2)}/{m.group(3)}", job) if m else None
    m = re.match(r"^([a-z]+):([A-Za-z0-9_.-]+)(?:/([A-Za-z0-9-]+))?$", ref)
    if m and m.group(1) in KNOWN:
        platform, token, job = m.group(1), m.group(2), m.group(3) or ""
        if platform == "greenhouse":
            return BoardRef(platform, token.lower(), job if job.isdigit() else "")
        if platform == "lever":
            return BoardRef(platform, token.lower(), job.lower() if _UUID_RE.match(job) else "")
        if platform == "ashby":
            return BoardRef(platform, token, job.lower() if _UUID_RE.match(job) else "")
        if platform == "workable":
            return BoardRef(platform, token.lower(), job.upper() if job else "")
        if platform in ("personio", "bamboohr", "teamtailor"):
            if job and not job.isdigit():
                return None  # their job ids are numeric; a bad one is refused, not widened
            return BoardRef(platform, token.lower(), job)
        if platform == "recruitee":
            if job and not _SLUG_RE.match(job.lower()):
                return None  # a bad job slug is refused, not widened to the board
            return BoardRef(platform, token.lower(), job.lower())
        return BoardRef(platform, token, job)
    if "://" in ref or "/" in ref or "." in ref:
        return detect_ats(ref)
    if default_platform and _TOKEN_RE.match(ref):
        return parse_board_ref(f"{default_platform}:{ref}")
    return None
