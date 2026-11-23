"""
Platform-neutral core shared by the ATS auto-apply tools (Phase 0 of
``doc/job-platforms-automation-plan.md``).

Moved out of ``greenhouse_apply_tool.py`` unchanged in behaviour, so every ATS
adapter (Greenhouse today; Lever / Ashby / Workable / SmartRecruiters next)
answers forms by the same rules. An adapter only has to:

  1. discover jobs                 → ``[{..., "title", "location", "url"}]``
  2. turn its form into questions  → ``[Question(key, label, kind, required, options)]``
  3. fill the planned answers into the page and submit.

Everything between 2 and 3 — profile / rule answers, option matching, the
sponsorship-vs-authorization split, spoken / programming languages, the
optional LLM ``answer_fn`` for required questions, and the "skip the job
rather than guess a required answer" policy — lives here, along with the
title / location matcher, the local "already applied" ledger, profile-field
parsing and the stealth browser helpers.

The answer-planning half is pure (no I/O) and unit-tested.
"""
import asyncio
import contextvars
import json
import os
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal


def noop(_msg: str) -> None:
    pass


def off_loop(fn: Callable, *args):
    """Call ``fn(*args)`` on a loop-free worker thread when an asyncio loop is
    running here. Playwright's sync API keeps one alive, and crewai's sync
    kickoff refuses to run inside a running loop — so the LLM callbacks
    (relevance gate / screening answers) must not run on this thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return fn(*args)
    ctx = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(ctx.run, fn, *args).result()



# ── title / location matching ────────────────────────────────────────────────

def split_list(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"[,\n;]", text or "") if s.strip()]


# Role shorthand people type vs. what job titles actually say. A keyword equal
# to a key is replaced by its phrases (any of which may match).
ROLE_ALIASES = {
    "swe": ("software engineer", "software developer", "software development engineer"),
    "sde": ("software development engineer", "software engineer", "software developer"),
    "be": ("backend", "back end", "back-end"),
    "fe": ("frontend", "front end", "front-end"),
    "fullstack": ("full stack", "fullstack", "full-stack"),
    "sre": ("site reliability", "reliability engineer"),
    "devops": ("devops", "dev ops", "platform engineer", "infrastructure engineer"),
    "ml": ("machine learning", "ml engineer", "ai engineer"),
    "mle": ("machine learning engineer", "ml engineer"),
    "de": ("data engineer",),
    "ds": ("data scientist",),
    "em": ("engineering manager",),
    "pm": ("product manager",),
}


def expand_keywords(keywords: list[str]) -> list[str]:
    out: list[str] = []
    for kw in keywords:
        for phrase in ROLE_ALIASES.get(kw.strip().lower(), (kw,)):
            if phrase not in out:
                out.append(phrase)
    return out


def phrase_in(phrase: str, title: str) -> bool:
    # Every word must START a word in the title: "engineer" hits "engineering",
    # but "ai" no longer hits "maintain".
    return all(re.search(r"\b" + re.escape(w), title) for w in phrase.lower().split())


def match_title(title: str, location: str, keywords: list[str], locations: list[str]) -> bool:
    """Title matches ANY keyword phrase (every word of that phrase starts a word
    in the title, case-insensitive; role shorthand like "SWE" is expanded via
    ROLE_ALIASES) and location matches ANY location substring. Empty lists match
    everything."""
    title = (title or "").lower()
    loc = (location or "").lower()
    if keywords and not any(phrase_in(kw, title) for kw in expand_keywords(keywords)):
        return False
    if locations and not any(lc.lower() in loc for lc in locations):
        return False
    return True


def interleave(groups: list[list[dict]]) -> list[dict]:
    out, i = [], 0
    while any(i < len(g) for g in groups):
        out.extend(g[i] for g in groups if i < len(g))
        i += 1
    return out


# ── answer planning (pure; unit-tested) ──────────────────────────────────────

# Every wording of "I'd rather not self-identify" seen on real forms
# (3M's Workday uses "Not declared").
_DECLINE_RE = re.compile(r"decline|don.?t wish|do not wish|prefer not|preferred not|not to (say|answer|disclose)|"
                         r"not declared|undeclared|not specified|choose not|rather not", re.I)
_AGREE_RE = re.compile(r"^(yes|i agree|i acknowledge|i consent|i understand|i confirm|i accept|agree|acknowledge|confirm|accept)", re.I)
# Most-truthful first: adapters find jobs on the employer's own ATS board.
_SOURCE_PREFS = (r"company (web)?site|careers? (page|site|website)|company careers|website",
                 r"job board|online|internet|search engine|third.party", r"\bother\b")


def match_option(options: list[str], want: str) -> str | None:
    """The option label best matching ``want``: exact, then prefix, then
    substring (all case-insensitive). None if nothing matches."""
    w = (want or "").strip().lower()
    if not w:
        return None
    # Prefix must end on a word boundary: "no" is a prefix of "No, I am not",
    # not of "Not now".
    tests = [lambda o: o == w, lambda o: re.match(re.escape(w) + r"(?!\w)", o) is not None]
    if len(w) > 3:  # "no" must not match inside "now" / "know"
        tests.append(lambda o: w in o)
    for test in tests:
        hits = [opt for opt in options if test(opt.strip().lower())]
        if hits:
            # Shortest wins: "National Taiwan University" → "… (NTU)", not
            # "… of Science And Technology".
            return min(hits, key=len)
    return None


def years_option(options: list[str], years: int) -> str | None:
    """Pick a range option ("0-2 years", "3-5", "10+") that contains ``years``."""
    for opt in options:
        nums = [int(n) for n in re.findall(r"\d+", opt)]
        if not nums:
            continue
        if "+" in opt or re.search(r"more|over|above", opt, re.I):
            if years >= nums[0]:
                return opt
        elif len(nums) >= 2 and nums[0] <= years <= nums[1]:
            return opt
        elif len(nums) == 1 and nums[0] == years:
            return opt
    return None


def yes_no(flag: bool) -> str:
    return "Yes" if flag else "No"


# Countries a work-authorization question may name. Keys are canonical names;
# values match the label (lower-cased) or an authorized_countries entry.
_COUNTRIES = {
    # Bare "us" only after "the"/"in": "eligible to work for us?" is not the US.
    "united states": r"united states|\bu\.s\.(a\.)?|\busa\b|\b(the|in) us\b|america",
    "united kingdom": r"united kingdom|\buk\b|britain|england",
    "canada": r"canada", "ireland": r"ireland", "germany": r"germany",
    "netherlands": r"netherlands", "france": r"france", "spain": r"spain",
    "poland": r"poland", "israel": r"israel", "india": r"india",
    "japan": r"japan", "singapore": r"singapore", "australia": r"australia",
    "taiwan": r"taiwan", "hong kong": r"hong kong", "brazil": r"brazil",
    "mexico": r"mexico", "south korea": r"korea", "thailand": r"thailand",
    "slovenia": r"slovenia", "switzerland": r"switzerland", "sweden": r"sweden",
}
# Job locations often give only a city ("Toronto", "San Francisco, CA").
_CITIES = {
    "united states": r"san francisco|new york|seattle|los angeles|mountain view|palo alto|"
                     r"menlo park|sunnyvale|san jose|san mateo|redwood city|bay area|chicago|"
                     r"boston|cambridge, ma|denver|washington,? d\.?c|atlanta|miami|"
                     r"austin|dallas|houston",
    "canada": r"toronto|vancouver|montreal", "ireland": r"dublin",
    "united kingdom": r"london|manchester|edinburgh", "thailand": r"bangkok",
    "india": r"gurugram|gurgaon|bengaluru|bangalore|hyderabad|pune|mumbai",
    "slovenia": r"ljubljana", "japan": r"tokyo|osaka", "australia": r"sydney|melbourne",
    "germany": r"berlin|munich", "netherlands": r"amsterdam", "france": r"paris",
    "israel": r"tel aviv", "brazil": r"s[aã]o paulo", "taiwan": r"taipei|hsinchu|taichung",
    "south korea": r"seoul", "spain": r"madrid|barcelona", "poland": r"warsaw|krak[oó]w",
}
_US_STATE = re.compile(r",\s*(A[KLRZ]|C[AOT]|D[CE]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]|"
                       r"O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY])\b")
# Regions where a remote role can be done from anywhere inside them.
_ANYWHERE = re.compile(r"worldwide|anywhere|global|apac|asia", re.I)


def countries_in(text: str) -> set[str]:
    t = (text or "").lower()
    return {name for name, pat in _COUNTRIES.items() if re.search(pat, t)}


def job_countries(location: str) -> set[str] | None:
    """Countries a job's location names (cities and US state codes included).
    ``{"*"}`` for worldwide / APAC remote; ``set()`` when unknown."""
    if _ANYWHERE.search(location or ""):
        return {"*"}
    found = countries_in(location)
    t = (location or "").lower()
    found |= {name for name, pat in _CITIES.items() if re.search(pat, t)}
    if _US_STATE.search(location or ""):
        found.add("united states")
    return found


_UNKNOWN = object()


def _authorized_for(lbl: str, profile: dict):
    """Authorization the question is about, from authorized_countries.

    A question naming a country is judged against that country; a generic one
    ("…in the country for which you applied?") against the JOB's location.
    Returns True/False, None when no list was given (blanket work_authorized
    flag applies), or _UNKNOWN when the job's country can't be determined —
    then the question must stay unanswered rather than be guessed."""
    allowed = profile.get("authorized_countries") or set()
    if not allowed:
        return None
    named = countries_in(lbl)
    if named:
        return named <= allowed
    # "…in the country where you currently reside / are based?" is about where
    # you live, not where the job is.
    if re.search(r"where you (currently )?(reside|live|are (currently )?based)", lbl):
        loc = (profile.get("location") or "").lower()
        here = countries_in(loc) | {n for n, pat in _CITIES.items() if re.search(pat, loc)}
        return (here <= allowed) if here else _UNKNOWN
    jc = profile.get("job_countries")
    if jc is None:  # no job context (e.g. planning without a location)
        return None
    if "*" in jc:
        return True
    return (jc <= allowed) if jc else _UNKNOWN


# Spoken languages a question may name ("Can you speak Chinese fluently?").
# Values are the words an option or the user's list may use for that language.
_SPOKEN = {
    "english": ("english",), "chinese": ("chinese", "mandarin"), "cantonese": ("cantonese",),
    "japanese": ("japanese",), "korean": ("korean",), "spanish": ("spanish",),
    "french": ("french",), "german": ("german",), "malay": ("malay", "bahasa melayu"),
    "indonesian": ("indonesian", "bahasa indonesia"), "tamil": ("tamil",), "hindi": ("hindi",),
    "vietnamese": ("vietnamese",), "thai": ("thai",), "arabic": ("arabic",),
    "portuguese": ("portuguese",), "italian": ("italian",), "dutch": ("dutch",),
}
_LEVEL_RE = re.compile(r"\b(beginner|basic|elementary|intermediate|advanced|expert|proficient|fluent|native)\b", re.I)
_PROGRAMMING_Q = re.compile(r"programming language|coding language|languages? (are you|you are|you're) "
                            r"(familiar|proficient|comfortable)|(which|what) languages do you (code|program|write)", re.I)


def _spoken_key(word: str) -> str | None:
    """Canonical key of a spoken-language name ("Mandarin" → "chinese")."""
    w = word.strip().lower()
    for key, aliases in _SPOKEN.items():
        if any(re.search(rf"\b{re.escape(a)}\b", w) for a in aliases):
            return key
    return None


def _language_options(wanted: list[str], options: list[str]) -> list[str]:
    """Options naming each wanted spoken language ("Chinese" also matches
    "Mandarin Chinese" / "Chinese (Mandarin)", never "Cantonese")."""
    picks: list[str] = []
    for want in wanted:
        key = _spoken_key(want)
        aliases = _SPOKEN.get(key or "", (want.strip().lower(),))
        hits = [o for o in options
                if any(re.search(rf"\b{re.escape(a)}\b", o.lower()) for a in aliases)
                and (key == "cantonese" or "cantonese" not in o.lower())]
        if hits:
            pick = min(hits, key=len)
            if pick not in picks:
                picks.append(pick)
    return picks


def _programming_options(wanted: list[str], options: list[str]) -> list[str]:
    """Options for each wanted programming language. An entry may carry a level
    ("Python: Advanced"); when the options pair each language with a level
    ("Java Beginner / Java Advanced") a language WITHOUT a stated level is left
    out rather than given a guessed proficiency."""
    leveled = any(_LEVEL_RE.search(o) for o in options)
    picks: list[str] = []
    for want in wanted:
        name, _, level = want.partition(":")
        name, level = name.strip().lower(), level.strip().lower()
        if not name:
            continue
        # Word-bounded so "Java" never matches "JavaScript".
        pat = re.compile(rf"(?<![\w+#]){re.escape(name)}(?![\w+#])", re.I)
        hits = [o for o in options if pat.search(o)]
        if leveled:
            if not level:
                continue
            hits = [o for o in hits if level in o.lower()]
        if hits:
            pick = min(hits, key=len)
            if pick not in picks:
                picks.append(pick)
    return picks


def location_parts(location: str) -> list[str]:
    return [p.strip() for p in (location or "").split(",") if p.strip()]


def _profile_answer(label: str, profile: dict):
    """Answers from the configurable defaults (notice period, languages,
    reason for leaving, …) and from the profile's location / authorization.
    Returns a str, a list (multi-value: languages), or None."""
    lbl = label.lower()
    plain = re.sub(r"[^a-z/ ]", "", lbl).strip()
    loc_parts = location_parts(profile.get("location", ""))

    # Bare "City" / "State" / "Country" text questions about where you are.
    if loc_parts and plain in ("city", "current city", "city of residence"):
        return loc_parts[0]
    # "San Francisco, CA": the second part is a US state code, not a country.
    us_state = len(loc_parts) == 2 and _US_STATE.search(", " + loc_parts[1]) is not None
    if loc_parts and plain in ("state", "state/province", "province", "region", "state / province"):
        if len(loc_parts) >= 3 or us_state:
            return loc_parts[1]
        # "Taipei, Taiwan" has no state: the city is the first-level division.
        return loc_parts[0]
    if loc_parts and plain in ("country", "current country", "country of residence"):
        return "United States" if us_state else loc_parts[-1]

    # Spoken languages — a question naming one asks Yes/No; otherwise it wants the list.
    langs = split_list(profile.get("languages", ""))
    if not _PROGRAMMING_Q.search(lbl) and not re.search(r"language models?|\bllms?\b", lbl) \
            and re.search(r"language|speak|fluen", lbl):
        named = {k for k in _SPOKEN if any(re.search(rf"\b{re.escape(a)}\b", lbl) for a in _SPOKEN[k])}
        if named and langs:
            # Only a yes/no phrasing gets Yes/No: "Describe your English
            # language skills" is not answered "Yes".
            if re.match(r"\s*(can|do|are|is|will|would|could)\b", lbl):
                have = {_spoken_key(x) for x in langs}
                return yes_no(named <= have)
            return None
        if not named and langs and re.search(r"language", lbl):
            return langs

    if _PROGRAMMING_Q.search(lbl):
        progs = split_list(profile.get("programming_languages", ""))
        return progs or None

    if re.search(r"notice period|how soon can you (start|join)|when can you (start|join)|"
                 r"earliest (possible )?(start|joining)|availability to (start|join)|"
                 r"when (would|will|could|can) you be available", lbl):
        return profile.get("notice_period") or None
    if re.search(r"reasons? for (leaving|wanting to leave|looking|changing)|"
                 r"why (are you|did you|do you want to) (leave|leaving|looking|change)", lbl):
        return profile.get("reason_for_leaving") or None
    if re.search(r"convicted|criminal (offen[cs]e|record|conviction|charge)", lbl):
        return yes_no(bool(profile.get("has_criminal_record")))
    if re.search(r"experience (with|in|using|of) (ai|a\.i\.|artificial intelligence|llms?|large language|"
                 r"generative|gen ?ai|machine learning)|\b(ai|llm|genai)\b.{0,40}\bexperience\b", lbl):
        return profile.get("ai_experience") or None

    # "Are you a Singapore Citizen or PR?" / "Residency status in Singapore" /
    # "Work eligibility in Singapore": a country you are not authorized to work
    # in is one you are not a citizen or resident of.
    if re.search(r"citizen|permanent resident|residency status|resident status|\bpr\b|work eligibility", lbl):
        named = countries_in(lbl)
        allowed = profile.get("authorized_countries") or set()
        if named and allowed and not (named <= allowed):
            return "No"
        return None
    # "Are you located in Singapore?" — compared with where you are based.
    m = re.search(r"\b(are you|do you) (currently )?(located|based|living|live|residing|reside) in\b", lbl)
    if m and countries_in(lbl) and loc_parts:
        here = countries_in(profile.get("location", "")) | {
            n for n, pat in _CITIES.items() if re.search(pat, profile.get("location", "").lower())}
        return yes_no(countries_in(lbl) <= here)
    if re.search(r"(currently|last) employed|current (employer|company)|where (do|did) you (currently )?work", lbl):
        return (profile.get("employment") or {}).get("company") or None
    return None


# Profile-URL fields ("LinkedIn", "What is your GitHub URL?", "Please share your
# LinkedIn profile") vs questions that only mention a site ("Why GitHub?",
# "Share 2-3 contributions (GitHub PRs)"). One test for rule_answer and
# standard_for_label, so the two can never disagree.
_URL_SITES = (("linkedin", r"linkedin"), ("github", r"github"),
              ("website", r"website|portfolio|personal site|blog"))
_CONTENT_Q = re.compile(r"\b(why|describe|explain|tell us|walk us|examples?|contributions?|"
                        r"pull requests?|prs|projects?)\b")
_URL_WORDS = re.compile(r"\b(url|urls|profile|link|links|handle|username|page|account)\b")


def profile_url_field(label: str) -> str | None:
    """"linkedin" / "github" / "website" when the label names that profile-URL
    field, None for anything else — including questions that mention the site."""
    t = re.sub(r"[*:✱?]", "", (label or "").lower()).strip()
    t = re.sub(r"\s*\((optional|required)\)$", "", t)
    # Workday in CJK locales: "Given Name - Western Script" is the romanised
    # name (the profile's); the "Local Script" one isn't, so it stays unmapped.
    t = re.sub(r"\s*[-–(]\s*western script\)?$", "", t)
    site = next((name for name, pat in _URL_SITES if re.search(pat, t)), None)
    if site is None or _CONTENT_Q.search(t):
        return None
    return site if len(t.split()) <= 3 or _URL_WORDS.search(t) else None


_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
              "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20}
_YEARS_Q = re.compile(r"\b(do|does|have|has) you (have|possess|got)\b.{0,25}?\b(?:at least |over |more than |a minimum of )?"
                      r"(\d{1,2}|" + "|".join(_NUM_WORDS) + r")\b\s*(?:\(\s*\d{1,2}\s*\)\s*)?\+?\s*(?:or more\s*)?years?\b")


# Words of a "how many years…?" question that don't name a skill.
_YEARS_GENERIC = set("""how many years year yrs have has had you do does did of experience experienced
    with in using used the a an and or more at least over than minimum total overall professional
    relevant work working industry software engineering engineer development developer developing
    product programming full stack fullstack full-stack backend back-end your plus number possess got
    what is are career commercial hands-on hands on paid post-graduate post graduation
    do you have""".split()) | set(_NUM_WORDS)


def _skill_specific(lbl: str) -> bool:
    """True when a years question names a skill, tool or domain ("…with C#",
    ".NET development", "leading teams") rather than general software work."""
    words = re.findall(r"[a-z0-9#+.]+(?:-[a-z]+)?", lbl.lower())
    return any(w.strip(".") not in _YEARS_GENERIC and not w.strip(".").isdigit()
               for w in words if w.strip("."))


def _years_required(lbl: str) -> int | None:
    """N in a yes/no "Do you have N (or more) years of …?" question, else None."""
    m = _YEARS_Q.search(lbl)
    if not m:
        return None
    n = m.group(3)
    return int(n) if n.isdigit() else _NUM_WORDS[n]


_PRIOR_AT = re.compile(r"\b(?:worked|employed|employee|contractor|intern)\s+(?:for|at|by|with)\s+(.{1,60})")
_US_TARGET = re.compile(r"(us|our|this|the company|the organi[sz]ation|here)\b")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _prior_employment(lbl: str, profile: dict):
    """ "Have you worked for <X> before?" is "No" when X is the hiring company,
    which is the question's usual meaning. When the job's company is known
    (``job_company``) and X is someone else ("…employed by Epic Systems",
    "…by a State government") it is a fact about the person: never guessed."""
    names = company_names(profile)
    m = _PRIOR_AT.search(lbl)
    if not names or m is None:
        return "No"
    target = m.group(1).strip()
    if _US_TARGET.match(target):
        return "No"
    # The whole name, as its first one to three words ("3M or a subsidiary",
    # "Metropolitan Hospital"): never a prefix, or "a state government" would
    # pass for "acme".
    # "…employed by any Micron company": a leading determiner isn't the name.
    target = re.sub(r"^(?:any|a|an|the|one of)\s+(?:of\s+)?", "", target, flags=re.I)
    words = [_norm(re.sub(r"['’]s$", "", w)) for w in target.split()]
    for name in names:
        # "…employed by WD?" at Western Digital: the company's initials.
        parts = re.findall(r"[a-z0-9]+", name.lower())
        if len(parts) > 1 and words and words[0] == "".join(w[0] for w in parts):
            return "No"
        if any("".join(words[:n]) == _norm(name) for n in (1, 2, 3)):
            return "No"
    return None


_LEGAL_SUFFIX = re.compile(r"[,.]?\s+(ltd|limited|inc|incorporated|llc|corp|corporation|co|company|gmbh|"
                           r"plc|s\.?a|b\.?v|pte|pty|ag|k\.?k|sdn bhd|bhd)\.?$", re.I)


def clean_company_name(name: str) -> str:
    """A legal entity name as people write the company: "0027 Applied
    Materials Taiwan, Ltd" → "Applied Materials" (no leading entity code,
    legal suffix or trailing country)."""
    t = re.sub(r"^\s*[\d-]+\s+", "", name or "").strip()
    for _ in range(3):
        t = _LEGAL_SUFFIX.sub("", t).strip().rstrip(",")
        words = t.split()
        if len(words) > 1 and countries_in(words[-1].lower()):
            t = " ".join(words[:-1])
    return t.strip()


def company_names(profile: dict) -> list[str]:
    """The hiring company's names: ``job_company`` and its aliases (on Workday
    the tenant "amat" and the employer "Applied Materials")."""
    names = [profile.get("job_company") or "", *(profile.get("job_company_aliases") or ())]
    return [n for n in dict.fromkeys(n.strip() for n in names) if n]


def rule_answer(label: str, profile: dict):
    """Heuristic answer for a custom question label, or None when unsure. The
    caller maps the answer onto a select's options when the field is a select.
    A list means several values (spoken / programming languages)."""
    lbl = label.lower()
    for kw, ans in profile.get("extra_answers") or []:
        if kw.lower() in lbl:
            return ans
    prof = _profile_answer(label, profile)
    if prof:
        return prof
    site = profile_url_field(label)
    if site == "linkedin":
        return profile.get("linkedin_url") or None
    if site == "github":
        return profile.get("github_url") or None
    if site == "website":
        return profile.get("website_url") or profile.get("github_url") or None
    # "Are you a citizen / lawful permanent resident of the U.S.…? (if you only
    # hold a temporary visa, check No)" is about citizenship, not sponsorship:
    # _profile_answer already said all it can (Western Digital, live).
    if re.search(r"\bcitizen\b|permanent resident|green card|asylee|refugee", lbl):
        return None
    # "Will you require a work permit…" is a sponsorship question, not "do you
    # have the right to work" — opposite answers.
    sponsor = re.search(r"sponsor|visa|(require|need)\b.*\bwork permit", lbl)
    auth_q = re.search(r"authori[sz]ed|eligible to work|legally (able|permitted|allowed)|"
                       r"right to work|work permit", lbl)
    # "Are you party to an agreement that restricts your right to work for us?"
    # is a non-compete question, not work authorization (Western Digital, live).
    if re.search(r"restrict|non-?compete|non-?solicit", lbl):
        sponsor = auth_q = None
    if sponsor or auth_q:
        ok = _authorized_for(lbl, profile)
        if ok is _UNKNOWN:
            return "__skip__"
        # Not authorized in the country at hand means "not authorized there" and
        # "would need sponsorship there" — never the blanket flags.
        needs = bool(profile.get("needs_sponsorship")) if ok is not False else True
        authorized = bool(profile.get("work_authorized", True)) if ok is None else ok
        # "Will you require sponsorship…" asks about sponsorship; "Are you
        # authorized to work … without sponsorship?" about authorization.
        if sponsor and "without" not in lbl and re.search(r"\b(require|need|will you|do you)\b", lbl):
            return yes_no(needs)
        if auth_q:
            return yes_no(authorized and not (sponsor and needs))
        return yes_no(needs)
    if re.search(r"(worked|employed) (for|at|by)\b.*\b(before|previously|in the past)|"
                 r"previously (worked|been employed)|former employee|ever (worked|been employed)", lbl):
        return _prior_employment(lbl, profile)
    # "…are you an existing HP employee?" (HP, Workday): asked about the hiring
    # company, it's "No" when the profile's current employer is someone else.
    m = re.search(r"\bare you (?:an? )?(?:existing|current) (.{1,30}?) ?employee", lbl)
    if m:
        names = {_norm(n) for n in company_names(profile)}
        current = _norm((profile.get("employment") or {}).get("company") or "")
        named = _norm(m.group(1))
        if names and (named in names or named == "") and current and current not in names:
            return "No"
        return None
    # "Are you referred to this job by a Micron employee?" too (Workday, Micron).
    if re.search(r"were you referred|are you referred|referred (to [\w ]{1,30} )?by|referral", lbl):
        return "No"
    if re.search(r"how (did )?you (first )?(hear|heard|find|found|learn|learned|came across)|"
                 r"where did you (hear|find|learn|see)", lbl):
        return "__source__"  # we found it on the employer's own job board
    # "Which university are you attending / did you last attend?" — the
    # highest-degree school from the profile (a select maps it onto its list).
    if re.search(r"(which|what) (university|college|school)|name of (the |your )?(university|college|school)|"
                 r"(university|college|school) (did|do) you (attend|graduate)", lbl):
        return (profile.get("education") or {}).get("school") or None
    if re.search(r"(highest|what) (level of )?(degree|education)", lbl):
        return (profile.get("education") or {}).get("degree") or None
    # "Do you have seven (7) / 5+ / at least 3 years of …?" is a yes/no about
    # the candidate's experience: compare with the profile's years, truthfully.
    # The profile knows total years only: for a skill ("…with C#", "…used
    # SQL") it can truthfully say "No" to "N years?" when the total is less,
    # and nothing more (Experian, live: "8-12 years" of C# was a guess).
    years = int(profile.get("years", 0) or 0)
    need = _years_required(lbl)
    if need is not None:
        if _skill_specific(lbl):
            return "No" if years < need else None
        return yes_no(years >= need)
    if re.search(r"how many years|years of (professional |relevant |work )?experience", lbl):
        return None if _skill_specific(lbl) else str(profile.get("years", 3))
    # "Last drawn / current salary" is a fact about the past, not the expectation.
    if re.search(r"(current|last|previous|present|most recent)( drawn)?( base)? (salary|compensation|pay)|"
                 r"last drawn", lbl) and not re.search(r"expect", lbl):
        return profile.get("current_salary") or None
    if re.search(r"salary|compensation|pay expectation|expected pay", lbl):
        return profile.get("salary") or None
    if re.search(r"(where|which (city|country)).*(located|based|live|reside|intend to work|work from)|"
                 r"current (location|city)|city of residence|country of residence|residential area", lbl):
        return profile.get("location") or None
    if re.search(r"preferred (first )?name", lbl):
        return profile.get("first_name") or None
    if re.search(r"full (legal )?name|legal name", lbl):
        return f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip() or None
    # A factual question about the candidate's past ("Have you signed an
    # agreement with a previous employer containing a non-compete…?") is NOT
    # a consent box — answering it "Yes / I agree" states a falsehood.
    factual = re.search(r"\b(have|has|had|did|were|was|are|is) you\b.{0,60}\b(sign(ed)?|enter(ed)?( into)?|"
                        r"executed|bound by|party to|subject to|agreed to)\b", lbl) \
        and not re.search(r"\b(terms|conditions|privacy|policy|notice|statement)\b", lbl)
    # ↑ "Have you read and agreed to the terms?" is a consent box, not a fact.
    if not factual and re.search(r"consent|acknowledge|agree|privacy|certify|attest|confirm that|terms|"
                                 r"i understand", lbl):
        return "__agree__"
    return None


def to_options(answer, options: list[str], label: str = "") -> list[str]:
    """Every option a list answer (spoken / programming languages) maps onto."""
    if _PROGRAMMING_Q.search(label or ""):
        return _programming_options(answer, options)
    return _language_options(answer, options)


def to_option(answer, options: list[str], label: str = "") -> str | None:
    """Map a heuristic/LLM answer onto one of a select's option labels."""
    if isinstance(answer, list):
        picks = to_options(answer, options, label)
        return picks[0] if picks else None
    if answer == "__agree__":
        return next((o for o in options if _AGREE_RE.match(o.strip())), None)
    hit = match_option(options, answer)
    if hit:
        return hit
    if answer.isdigit():
        return years_option(options, int(answer))
    lead = re.match(r"\s*(\d+)\s*(\+|years?|yrs?)?\s*$", answer)
    if lead:  # "5 years" against "3-5 years" / "5+ years"
        return years_option(options, int(lead.group(1)))
    if answer == "No":
        # Sentence-style options: "I have never worked at Robinhood", "I do not…",
        # and a residency list's "Others" ("Citizen / PR / Others").
        return next((o for o in options if re.search(r"\b(never|not|don.?t|no)\b", o, re.I)), None) \
            or next((o for o in options if re.fullmatch(r"\s*(others?|none( of the above)?)\s*", o, re.I)), None)
    if "," in answer:  # "Taipei, Taiwan" against a country list → "Taiwan"
        for part in reversed(location_parts(answer)):
            hit = match_option(options, part)
            if hit:
                return hit
    if answer == "__source__":
        for pat in _SOURCE_PREFS:
            hit = next((o for o in options if re.search(pat, o, re.I)), None)
            if hit:
                return hit
        return None
    return None


def call_answer_fn(answer_fn: Callable, label: str, kind: str, options: list[str]) -> str:
    try:
        return (off_loop(answer_fn, label, kind, options) or "").strip()
    except Exception:  # noqa: BLE001 — an LLM failure just leaves it unanswered
        return ""


# ── normalized form question → planned answer ────────────────────────────────

QuestionKind = Literal["text", "select", "multiselect", "file"]


@dataclass
class Question:
    """One answerable form field, in a shape every ATS adapter can produce.

    ``key`` is whatever the adapter fills by (DOM id / API field name);
    ``kind`` is ``text`` (input or textarea), ``select``, ``multiselect`` or
    ``file``; ``options`` are the choice labels for select kinds."""
    key: str
    label: str
    kind: QuestionKind
    required: bool = False
    options: list[str] = field(default_factory=list)


def _from_extra_answers(label: str, profile: dict, ans) -> bool:
    """Did ``ans`` come from the user's Extra Answers (not a rule)? The same
    keyword match ``rule_answer`` uses first: the first Extra Answer whose
    keyword is in the label, and its value is ``ans``."""
    lbl = label.lower()
    for kw, value in profile.get("extra_answers") or []:
        if kw.lower() in lbl:
            return value == ans
    return False


_YES_NO_Q = re.compile(r"\s*(are|am|do|does|did|have|has|had|is|was|were|will|would|can|could|"
                       r"should|shall|may)\b", re.I)


def answer_question(q: Question, profile: dict,
                    answer_fn: Callable[[str, str, list[str]], str] | None = None,
                    ) -> dict | None:
    """Plan the answer for one custom question, or None when we have no
    confident answer (the caller decides whether that blocks the job).

    Returns ``{field, kind, value, label}``: ``value`` is a str, or a list for
    ``multiselect``. ``answer_fn(label, kind, options) -> str`` is consulted
    only for REQUIRED questions the rules miss; return "" to abstain. A
    question whose answer must not be guessed (work authorization for a job in
    an unknown country) is never sent to ``answer_fn``."""
    label, kind, options, required = q.label, q.kind, q.options, q.required

    def ans_dict(value) -> dict:
        return {"field": q.key, "kind": kind, "value": value, "label": label}

    ans = rule_answer(label, profile)
    if ans == "__skip__":
        return None  # must not be guessed — neither by rules nor the LLM
    if kind == "multiselect" and isinstance(ans, list):
        picks = to_options(ans, options, label)
        return ans_dict(picks) if picks else None
    if kind in ("select", "multiselect"):
        pick = to_option(ans, options, label) if ans else None
        if pick is None and ans and profile.get("extra_answers"):
            # An extra answer is a keyword match, so one written for
            # another question ("currently reside: Taiwan") can catch
            # this one ("…sponsorship in the country where you currently
            # reside?"). If it fits none of the options, use the rules.
            fallback = rule_answer(label, {**profile, "extra_answers": []})
            if fallback and fallback != "__skip__":
                pick = to_option(fallback, options, label)
        if pick is None and required and len(options) == 1:
            # A required single-option select ("I agree", "Aceito os
            # termos…") is an acknowledgement — the only way to apply.
            pick = options[0]
        if pick is None and required and answer_fn is not None:
            pick = match_option(options, call_answer_fn(answer_fn, label, "select", options))
        if pick is None and required:
            pick = next((o for o in options if _DECLINE_RE.search(o)), None) \
                if DEMOGRAPHIC_RE.search(label) else None
        if pick is None:
            return None
        return ans_dict([pick] if kind == "multiselect" else pick)
    if kind == "text":
        if isinstance(ans, list):
            ans = ", ".join(ans)
        if ans == "__source__":
            ans = "Company careers page"
        elif ans == "__agree__":
            ans = None
        elif ans in ("Yes", "No") and not _YES_NO_Q.match(label) \
                and not _from_extra_answers(label, profile, ans):
            # An INFERRED Yes/No only answers a yes/no question: "If you
            # selected a valid work permit, please specify which type" is not
            # one. The user's own Extra Answers are always kept (PR review).
            ans = None
        if not ans and required and answer_fn is not None:
            ans = call_answer_fn(answer_fn, label, "text", [])
        return ans_dict(ans) if ans else None
    return None  # file uploads etc. are the adapter's job


# Voluntary self-identification (EEO / diversity surveys): the only answer we
# ever give is the decline option. "18 years of age or older?" is NOT one —
# only a question asking for the age itself.
DEMOGRAPHIC_RE = re.compile(
    r"gender|\bsex\b|race|ethnic|veteran|disabilit|neurodivers|pronoun|sexual orientation|"
    r"hispanic|latin[ao]?\b|first generation|what.?s your age|\byour age\b|age (range|group|bracket)",
    re.I)


def decline_option(options: list[str]) -> str | None:
    """The "decline to self-identify" option of an EEOC / demographic question —
    the only answer we ever give one: never a guessed identity."""
    return next((o for o in options if _DECLINE_RE.search(o)), None)


def plan_questions(questions: list[Question], profile: dict,
                   answer_fn: Callable[[str, str, list[str]], str] | None = None,
                   ) -> tuple[list[dict], list[str]]:
    """``answer_question`` over a flat list. Returns ``(answers, unanswered)``
    where ``unanswered`` holds the labels of REQUIRED questions with no answer —
    the caller should skip such a job rather than submit a guess."""
    answers: list[dict] = []
    unanswered: list[str] = []
    for q in questions:
        a = answer_question(q, profile, answer_fn)
        if a is not None:
            answers.append(a)
        elif q.required:
            unanswered.append(q.label or "(unlabelled question)")
    return answers, unanswered


def with_job_location(profile: dict, location: str | None, company: str | None = None,
                      aliases: tuple[str, ...] = ()) -> dict:
    """The profile with the job's countries attached, so a generic "are you
    authorized to work in the country you applied for?" is judged against the
    job. ``None`` (no location known) leaves the profile as it is. ``company``
    (the hiring company) tells "have you worked for us before?" apart from a
    question about another employer."""
    if company:
        profile = {**profile, "job_company": company}
    if aliases:
        profile = {**profile, "job_company_aliases": tuple(a for a in aliases if a)}
    if location is None:
        return profile
    return {**profile, "job_countries": job_countries(location)}


# ── profile-field parsing (shared run-form inputs) ───────────────────────────

_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


_DEGREE_LEVELS = (("doctor", r"doctor|ph\.?\s?d|d\.?phil"),
                  ("master", r"master|\bm\.?s\.?c?\b|\bm\.?a\.?\b|\bmba\b|\bm\.?eng\b"),
                  ("bachelor", r"bachelor|\bb\.?s\.?c?\b|\bb\.?a\.?\b|\bb\.?eng\b"),
                  ("associate", r"associate"),
                  ("high school", r"high school|\bged\b|secondary"))


def degree_level(text: str) -> str | None:
    t = (text or "").lower()
    return next((lvl for lvl, pat in _DEGREE_LEVELS if re.search(pat, t)), None)


def degree_option(options: list[str], degree: str) -> str | None:
    """The one option at the profile degree's level ("Master's Degree" →
    "Masters (±18 years of education)"). None when no option, or several
    ("Master of Science" / "Master of Arts"), are at that level: never a guess."""
    level = degree_level(degree)
    hits = [o for o in options if level and degree_level(o) == level]
    return hits[0] if len(hits) == 1 else None


def parse_start(text: str) -> dict:
    """"2025-06" / "06/2025" / "June 2025" → {start_month: "June", start_year: "2025"}."""
    t = (text or "").strip()
    year = re.search(r"\b(19|20)\d{2}\b", t)
    month = None
    m = re.search(r"\b(1[0-2]|0?[1-9])\b", re.sub(r"\b(19|20)\d{2}\b", "", t))
    if m:
        month = _MONTHS[int(m.group(1)) - 1]
    else:
        month = next((mo for mo in _MONTHS if mo[:3].lower() in t.lower()), None)
    if not (year and month):
        return {}
    return {"start_month": month, "start_year": year.group(0)}


def parse_extra_answers(text: str) -> list[tuple[str, str]]:
    """``keyword: answer`` lines → [(keyword, answer)]."""
    out = []
    for line in (text or "").splitlines():
        if ":" in line:
            kw, ans = line.split(":", 1)
            if kw.strip() and ans.strip():
                out.append((kw.strip(), ans.strip()))
    return out


# The candidate's standing answers to personal questions ("bonded: No",
# "academic ranking: 11%-20%"), in the same ``keyword: answer`` lines as a run's
# extra_answers, applied to every run after that run's own. Personal data:
# under data/, which is git-ignored. Lines starting with "#" are comments.
DEFAULT_ANSWERS_PATH = "data/default_answers.txt"


def default_answers(path: str | None = None) -> list[tuple[str, str]]:
    """The saved default answers; [] when there is no file."""
    path = path or os.getenv("ATS_DEFAULT_ANSWERS_FILE", DEFAULT_ANSWERS_PATH)
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return []
    return parse_extra_answers("\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#")))


def build_profile(*, first_name: str, last_name: str, email: str, phone: str = "",
                  current_location: str = "", linkedin_url: str = "", github_url: str = "",
                  website_url: str = "", years_experience=3, salary_expectation: str = "",
                  work_authorized: bool = True, needs_sponsorship: bool = False,
                  authorized_countries: str = "", resume_path: str = "",
                  cover_letter_path: str | None = None, cover_letter_text: str = "",
                  extra_answers: str = "", current_salary: str = "", notice_period: str = "",
                  languages: str = "", programming_languages: str = "", ai_experience: str = "",
                  reason_for_leaving: str = "", has_criminal_record: bool = False,
                  school: str = "", degree: str = "", discipline: str = "",
                  current_company: str = "", current_title: str = "",
                  current_start: str = "") -> dict:
    """The candidate profile every planner reads, from the run-form inputs."""
    try:
        years = int(years_experience)  # preserve an explicit 0 (entry-level)
    except (TypeError, ValueError):
        years = 3
    return {
        "first_name": first_name.strip(), "last_name": last_name.strip(),
        "email": email.strip(), "phone": (phone or "").strip(),
        "location": (current_location or "").strip(),
        "linkedin_url": (linkedin_url or "").strip(), "github_url": (github_url or "").strip(),
        "website_url": (website_url or "").strip(), "years": years,
        "salary": (salary_expectation or "").strip(),
        "work_authorized": bool(work_authorized),
        "needs_sponsorship": bool(needs_sponsorship),
        "authorized_countries": set().union(*(countries_in(c) for c in split_list(authorized_countries))),
        "resume_path": resume_path, "cover_letter_path": cover_letter_path,
        "cover_letter_text": (cover_letter_text or "").strip(),
        # The run's own answers first (the first matching keyword wins), then
        # the candidate's saved defaults.
        "extra_answers": parse_extra_answers(extra_answers) + default_answers(),
        "current_salary": (current_salary or "").strip(),
        "notice_period": (notice_period or "").strip(),
        "languages": languages or "",
        "programming_languages": programming_languages or "",
        "ai_experience": (ai_experience or "").strip(),
        "reason_for_leaving": (reason_for_leaving or "").strip(),
        "has_criminal_record": bool(has_criminal_record),
        "education": {"school": (school or "").strip(), "degree": (degree or "").strip(),
                      "discipline": (discipline or "").strip()},
        "employment": {"company": (current_company or "").strip(),
                       "title": (current_title or "").strip(), **parse_start(current_start)},
    }


def standard_value(name: str, profile: dict) -> str:
    """A system field every ATS asks for, by its canonical name: first_name,
    last_name, full_name, email, phone, location, city, linkedin, github,
    website, current_company, current_title, resume, cover_letter_file,
    cover_letter_text. "" when the profile has none."""
    first, last = profile.get("first_name", ""), profile.get("last_name", "")
    emp = profile.get("employment") or {}
    values = {
        "first_name": first, "last_name": last, "full_name": f"{first} {last}".strip(),
        "email": profile.get("email", ""), "phone": profile.get("phone", ""),
        "location": profile.get("location", ""),
        "city": (location_parts(profile.get("location", "")) or [""])[0],
        "linkedin": profile.get("linkedin_url", ""), "github": profile.get("github_url", ""),
        "website": profile.get("website_url", "") or profile.get("github_url", ""),
        "current_company": emp.get("company", ""), "current_title": emp.get("title", ""),
        "resume": profile.get("resume_path", ""),
        "cover_letter_file": profile.get("cover_letter_path") or "",
        "cover_letter_text": profile.get("cover_letter_text", ""),
    }
    return str(values.get(name) or "")


# Contact-field labels → standard_value() names, for ATSs that don't mark their
# built-in fields (Ashby / Workable custom "Phone Number", "Linkedin", …).
_STANDARD_LABELS = (
    ("first_name", r"^(legal )?first name$|^given name$"),
    ("last_name", r"^(legal )?(last|family) name$|^surname$"),
    ("full_name", r"^(full |legal |your )?name$|^full legal name$"),
    ("email", r"^(e-?mail|email address)$"),
    ("phone", r"^(phone|mobile|cell|telephone)( (number|no\.?))?$"),
)


def standard_for_label(label: str) -> str | None:
    """The standard_value() name a plain contact-field label asks for, or None."""
    site = profile_url_field(label)
    if site:
        return site
    t = re.sub(r"[*:✱]", "", (label or "").lower()).strip()
    t = re.sub(r"\s*\((optional|required)\)$", "", t)
    # Workday in CJK locales: "Given Name - Western Script" is the romanised
    # name (the profile's); the "Local Script" one isn't, so it stays unmapped.
    t = re.sub(r"\s*[-–(]\s*western script\)?$", "", t)
    if len(t.split()) > 5:  # contact-field labels are a few words
        return None
    for name, pat in _STANDARD_LABELS:
        if re.search(pat, t):
            return name
    return None


# Whole numbers only: "1.5 months" must not read as "5 months".
_NOTICE_RE = re.compile(r"(?<![\d.])(\d+)(?!\.\d)\s*(day|week|month)", re.I)


def start_date_from_notice(notice: str, today=None):
    """The earliest start date a notice period implies ("2 weeks" → today + 14
    days, "immediately" → today), or None when it can't be read — a date
    question is then left to extra_answers / the LLM rather than guessed."""
    from datetime import date, timedelta
    today = today or date.today()
    t = (notice or "").strip().lower()
    if not t:
        return None
    if re.search(r"immediate|asap|right away|\bnow\b", t):
        return today
    m = _NOTICE_RE.search(t)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    return today + timedelta(days=n * {"day": 1, "week": 7, "month": 30}[unit])


# ── local ledger (no site-side "already applied" without a login) ────────────

def load_ledger(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record_applied(path: str, key: str, job: dict, status: str = "applied") -> None:
    """Add ``key`` (the adapter's canonical job id) to the ledger at ``path``.
    ``status`` is "applied" (confirmed) or "pending" (Submit was clicked but no
    confirmation was seen — the employer may have it, so it must not be sent
    again automatically)."""
    ledger = load_ledger(path)
    ledger[key] = {
        "title": job.get("title", ""), "company": job.get("company", ""),
        "applied_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": status,
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=1)


def url_path_has(url: str, segments: tuple[str, ...]) -> bool:
    """True when the URL's path contains one of ``segments`` as a whole path
    segment: "/acme/123/thanks" has "thanks"; "/thanksgiving-co/123" does not."""
    from urllib.parse import urlparse
    parts = [p for p in urlparse(url or "").path.split("/") if p]
    return any(seg in parts for seg in segments)


# ── browser helpers ──────────────────────────────────────────────────────────

# ATS forms carry invisible reCAPTCHA / hCaptcha, which scores headless or
# automation-flagged browsers low and silently rejects the submission. Real
# Chrome, headed, by default.
STEALTH_ARGS = ["--disable-blink-features=AutomationControlled"]
STEALTH_IGNORE = ["--enable-automation"]


def launch_browser(pw, headless: bool):
    try:
        return pw.chromium.launch(channel="chrome", headless=headless,
                                  args=STEALTH_ARGS, ignore_default_args=STEALTH_IGNORE)
    except Exception:  # noqa: BLE001 — Chrome not installed / not launchable
        return pw.chromium.launch(headless=headless, args=STEALTH_ARGS)


def first_visible(page, selectors: list[str]):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:  # noqa: BLE001
            continue
    return None


def visible_text(page, texts) -> str | None:
    for txt in texts:
        try:
            if page.get_by_text(txt, exact=False).first.is_visible():
                return txt
        except Exception:  # noqa: BLE001
            continue
    return None
