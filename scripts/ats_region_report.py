"""Region-wide report: which employers have software-engineering openings in Europe / Asia.

Reads the board cache written by `scripts/ats_country_boards.py scan` (no network of its
own) and renders one GitHub issue per region: a per-country summary table, then one table
per country listing every board with its `platform:token` ref for `ats_apply`.

    uv run python scripts/ats_region_report.py --list
    uv run python scripts/ats_region_report.py europe                    # data/ats_reports/europe_<date>.md
    uv run python scripts/ats_region_report.py europe --platform workday
    uv run python scripts/ats_region_report.py asia --create-issue --repo owner/name

A GitHub issue body is capped at 65,536 characters, so a long report is cut on country
boundaries: the first part is the issue body, the rest are posted as comments.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

DEFAULT_CACHE = "data/ats_boards.json"
DEFAULT_OUT_DIR = "data/ats_reports"
MAX_BODY = 60_000          # GitHub's limit is 65,536; leave headroom
SCAN_COMMAND = "uv run python scripts/ats_country_boards.py scan"

# ── title filter ─────────────────────────────────────────────────────────────
SWE = re.compile(
    r"software|back[- ]?end|front[- ]?end|full[- ]?stack|platform engineer|cloud engineer"
    r"|devops|dev ops|\bsre\b|site reliability|data engineer|machine learning|\bml\b"
    r"|\bmlops\b|\bai\b|\bdeveloper\b|\bsdet\b"
    r"|entwickler|d[ée]veloppeu|desarrollador|sviluppatore|ontwikkelaar|ing[ée]nieur logiciel",
    re.I)
NOT_SWE = re.compile(
    r"\bintern\b|internship|co-?op\b|student|werkstudent|praktik|stagiaire|\bstage\b"
    r"|alternan|apprenti|trainee|thesis|graduate|early careers?|\bsales\b|pre[- ]?sales|\bmanager\b|\bdirector\b"
    r"|\bhead of\b|\bvp\b|vice president|recruit|marketing|business develop|real estate"
    r"|land developer|property developer|account executive",
    re.I)


def is_swe(title: str) -> bool:
    return bool(SWE.search(title)) and not NOT_SWE.search(title)


# ── location matching ────────────────────────────────────────────────────────
# Per country:
#   words   country / city names; case-insensitive, must start a word
#   iso3    ISO-3166 alpha-3 codes; case-sensitive, anywhere ("POL - Wroclaw")
#   iso2    alpha-2 codes; case-sensitive and only as the first or last token
#           ("PL Warsaw Workspace", "Dublin, IE") - mid-text they are English words
#   lead    alpha-2 codes that are also US states: first token only ("DE - Glinde")
#   eu_dc   city names shared with North America (Cambridge, Birmingham ...): counted only
#           on a board in Workday's EU datacenter (`<tenant>.wd3`)
#   reject  vetoes the location (same-named places elsewhere)
_US = (r"\bUSA?\b|united states|, ?(?:A[LKZR]|C[AOT]|DE|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]"
       r"|N[CDEHJMVY]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY]|ON|NS|BC|QC|AB)\b")


def _c(name, words, iso3="", iso2="", lead="", eu_dc="", reject=""):
    return {"name": name, "words": words, "iso3": iso3, "iso2": iso2, "lead": lead,
            "eu_dc": eu_dc, "reject": reject}


EUROPE = [
    _c("United Kingdom",
       r"united kingdom|england|scotland|northern ireland|london|edinburgh|glasgow|belfast"
       r"|cardiff|aberdeen|dundee|stirling|leeds|sheffield|liverpool|nottingham|leicester"
       r"|coventry|southampton|milton keynes|knutsford|cheltenham|swindon|slough|uxbridge"
       r"|maidenhead|basingstoke|farnborough|stevenage|crawley|guildford|woking|staines"
       r"|egham|croydon|watford|canary wharf|holborn|farringdon|newcastle upon tyne|wales\b",
       iso3="GBR", iso2="GB|UK",
       eu_dc=r"cambridge|birmingham|manchester|bristol|oxford|reading|durham|bradford"
             r"|plymouth|brighton|newcastle|bath\b",
       reject=_US + r"|\bCAN?\b|\bcanada\b(?! square)|ontario|new south wales|australia"
                    r"|south africa|jamaica|\b(MA|AL|NC|NS|NY|NH)\b|minnesota|toronto"),
    _c("Ireland", r"ireland|dublin|cork\b|galway|limerick|letterkenny|athlone|waterford|leixlip",
       iso3="IRL", iso2="IE", reject=_US + r"|northern ireland|ohio|california|\bOH\b"),
    _c("Germany",
       r"germany|deutschland|berlin|munich|münchen|muenchen|hamburg|frankfurt|cologne|köln"
       r"|stuttgart|düsseldorf|dusseldorf|duesseldorf|karlsruhe|leipzig|dresden|nürnberg"
       r"|nuremberg|hannover|bremen|dortmund|essen\b|bonn\b|mannheim|heidelberg|walldorf"
       r"|darmstadt|wiesbaden|mainz|aachen|erlangen|regensburg|augsburg|ulm\b|freiburg"
       r"|kiel\b|jena\b|potsdam|wolfsburg|ingolstadt|böblingen|sindelfingen|ratingen"
       r"|eschborn|garching|bielefeld|münster|paderborn|kassel|rostock|magdeburg"
       r"|braunschweig|leverkusen|bochum|duisburg|wuppertal|mönchengladbach",
       iso3="DEU", lead="DE",
       reject=_US + r"|delaware|wilmington|newark|dover|work from home|south africa"),
    _c("France",
       r"france|paris|lyon\b|toulouse|marseille|nantes|bordeaux|lille\b|grenoble"
       r"|sophia antipolis|rennes|strasbourg|montpellier|nice\b|courbevoie|la d[ée]fense"
       r"|puteaux|nanterre|boulogne|issy|v[ée]lizy|massy|saclay|levallois|montrouge|rueil"
       r"|guyancourt|valbonne|antony\b|st\.? [ée]tienne",
       iso3="FRA", iso2="FR", reject=_US + r"|texas|tennessee|kentucky"),
    _c("Netherlands",
       r"netherlands|nederland|amsterdam|rotterdam|utrecht|eindhoven|the hague|den haag"
       r"|delft|haarlem|groningen|leiden|nijmegen|arnhem|hoofddorp|schiphol|veldhoven"
       r"|enschede|zwolle|breda\b|tilburg|maastricht|amersfoort|almere|den bosch"
       r"|hertogenbosch|zoetermeer|rijswijk|nieuwegein|apeldoorn|hilversum|venlo",
       iso3="NLD", iso2="NL", reject=_US + r"|canada|newfoundland|antilles"),
    _c("Belgium",
       r"belgium|belgië|belgique|brussels|bruxelles|antwerp|ghent|gent\b|leuven|li[eè]ge"
       r"|mechelen|diegem|zaventem|kortrijk|hasselt|charleroi|namur|bruges|brugge",
       iso3="BEL", iso2="BE", reject=_US),
    _c("Luxembourg", r"luxembourg|luxemburg", iso3="LUX", iso2="LU"),
    _c("Switzerland",
       r"switzerland|schweiz|suisse|svizzera|z[uü]rich|zuerich|geneva|gen[eè]ve|basel\b|bern\b"
       r"|lausanne|zug\b|lugano|winterthur|st\.? gallen|lucerne|luzern|neuch[aâ]tel"
       r"|schaffhausen|rotkreuz|vevey|nyon\b|fribourg",
       iso3="CHE", iso2="CH", reject=_US + r"|illinois|new bern"),
    _c("Austria", r"austria|österreich|vienna|wien\b|graz\b|linz\b|salzburg|innsbruck|klagenfurt",
       iso3="AUT", reject=_US + r"|virginia|juan de austria"),
    _c("Spain",
       r"spain|espa[ñn]a|madrid|barcelona|valencia|m[aá]laga|sevill[ae]|bilbao|zaragoza"
       r"|vigo\b|alicante|murcia|granada|valladolid|pamplona|san sebasti[aá]n|a coru[ñn]a"
       r"|las palmas|oviedo|gij[oó]n|girona|sant cugat|viladecans|las rozas|alcobendas"
       r"|pozuelo|tres cantos|boadilla del monte",
       iso3="ESP", iso2="ES",
       reject=_US + r"|california|venezuela|philippines|trinidad|port of spain|colombia"
                    r"|mexico|argentina|chile|costa rica|dominican|nicaragua"),
    _c("Portugal",
       r"portugal|lisbon|lisboa|porto\b|oporto|braga\b|coimbra|aveiro|oeiras|guimar[aã]es"
       r"|matosinhos|carnaxide|moscavide",
       iso3="PRT", reject=_US + r"|brazil|brasil|porto alegre|porto rico"),
    _c("Italy",
       r"italy|italia\b|milan\b|milano|rome\b|roma\b|turin\b|torino|bologna|florence"
       r"|firenze|naples\b|napoli|genoa|genova|padova|padua|verona|venice\b|venezia"
       r"|trieste|bari\b|catania|palermo|pisa\b|modena|parma\b|brescia|bergamo|assago"
       r"|segrate|agrate|vimercate",
       iso3="ITA", iso2="IT",
       reject=_US + r"|\bIT (hub|dep|serv|supp|oper|offic|center|centre)|texas|ohio|florida"),
    _c("Sweden",
       r"sweden|sverige|stockholm|gothenburg|g[oö]teborg|malm[oö]|lund\b|uppsala"
       r"|link[oö]ping|v[aä]ster[aå]s|solna|kista\b|ume[aå]|lule[aå]|[oö]rebro|helsingborg"
       r"|j[oö]nk[oö]ping|norrk[oö]ping|karlskrona|s[oö]dert[aä]lje|sundbyberg",
       iso3="SWE", iso2="SE", reject=_US + r"|\bSE (asia|region)"),
    _c("Denmark",
       r"denmark|danmark|copenhagen|k[oø]benhavn|aarhus|århus|aalborg|odense\b|ballerup"
       r"|lyngby|bagsv[aæ]rd|kalundborg|billund|s[oø]borg|herlev|hiller[oø]d|kolding|vejle"
       r"|esbjerg|hellerup|nordborg",
       iso3="DNK", iso2="DK", reject=_US + r"|western australia"),
    _c("Norway",
       r"norway|norge\b|oslo\b|bergen\b|trondheim|stavanger|kristiansand|troms[oø]|lysaker"
       r"|fornebu|sandvika|kongsberg|horten\b",
       iso3="NOR", iso2="NO", reject=_US + r"|bergen county|new jersey"),
    _c("Finland",
       r"finland|suomi|helsinki|espoo|tampere|oulu\b|turku|vantaa|jyv[aä]skyl[aä]|lahti\b"
       r"|kuopio|vaasa",
       iso3="FIN", iso2="FI", reject=_US),
    _c("Poland",
       r"poland|polska|warsaw|warszawa|krak[oó]?w|cracow|wroc[lł]aw|gda[nń]sk|gdynia"
       r"|pozna[nń]|[lł][oó]d[zź]|katowice|szczecin|lublin|rzesz[oó]w|bia[lł]ystok"
       r"|bydgoszcz|gliwice|toru[nń]|tricity",
       iso3="POL", iso2="PL", reject=_US + r"|indiana"),
    _c("Czechia",
       r"czech|česk|prague|praha|brno\b|ostrava|plze[nň]|pilsen|olomouc|liberec|pardubice",
       iso3="CZE", iso2="CZ", reject=_US),
    _c("Slovakia", r"slovakia|slovensk|bratislava|ko[sš]ice|[zž]ilina|nitra\b",
       iso3="SVK", iso2="SK", reject=r"canada|saskatchewan"),
    _c("Hungary", r"hungary|magyarorsz|budapest|debrecen|szeged|p[eé]cs\b|gy[oő]r\b|miskolc",
       iso3="HUN", iso2="HU", reject=_US),
    _c("Romania",
       r"romania|rom[aâ]nia|bucharest|bucure[sşș]ti|cluj|timi[sşș]oara|ia[sşș]i\b"
       r"|bra[sşș]ov|sibiu|craiova|oradea|constan[tţț]a|ploie[sşș]ti",
       iso3="ROU|ROM", iso2="RO", reject=_US),
    _c("Bulgaria", r"bulgaria|sofia\b|plovdiv|varna\b|burgas", iso3="BGR", iso2="BG",
       reject=_US),
    _c("Greece", r"greece|hellas|athens|thessaloniki|patras|heraklion|marousi|chalandri",
       iso3="GRC", iso2="GR", reject=_US + r"|georgia|ohio|\b(GA|OH|AL|TN)\b"),
    _c("Serbia", r"serbia|srbija|belgrade|beograd|novi sad|niš", iso3="SRB", reject=_US),
    _c("Croatia", r"croatia|hrvatska|zagreb|rijeka|osijek", iso3="HRV"),
    _c("Slovenia", r"slovenia|slovenija|ljubljana|maribor", iso3="SVN"),
    _c("Estonia", r"estonia|eesti\b|tallinn|tartu\b"),
    _c("Latvia", r"latvia|latvija|riga\b|rīga", iso3="LVA"),
    _c("Lithuania", r"lithuania|lietuva|vilnius|kaunas|klaip[eė]da", iso3="LTU"),
    _c("Ukraine", r"ukraine|kyiv|kiev\b|lviv|kharkiv|odesa\b|dnipro|vinnytsia", iso3="UKR",
       reject=_US),
    _c("Türkiye", r"turkey|t[uü]rkiye|istanbul|ankara|izmir|bursa\b|kocaeli|gebze",
       iso3="TUR", reject=_US),
    _c("Cyprus", r"cyprus|nicosia|limassol|larnaca", iso3="CYP"),
    _c("Malta", r"malta\b|valletta|sliema", iso3="MLT", reject=_US + r"|\b(NY|MT|OH|ID)\b"),
    _c("Iceland", r"iceland|reykjav[ií]k", iso3="ISL"),
]

ASIA = [
    _c("Taiwan", r"taiwan|taipei|hsinchu|taichung|kaohsiung|tainan|taoyuan|zhubei|台灣|臺灣|台北",
       iso3="TWN", iso2="TW"),
    _c("Japan", r"japan|tokyo|osaka|kyoto|yokohama|fukuoka|nagoya|日本|東京", iso3="JPN", iso2="JP"),
    _c("Singapore", r"singapore", iso3="SGP", iso2="SG"),
    _c("Hong Kong", r"hong\s?kong", iso3="HKG", iso2="HK"),
    _c("Korea", r"korea|seoul|pangyo|busan|incheon|suwon", iso3="KOR", iso2="KR"),
    _c("China",
       r"china|shanghai|beijing|shenzhen|guangzhou|hangzhou|chengdu|nanjing|suzhou|wuhan"
       r"|xi'?an\b|dalian|tianjin|chongqing|xiamen|qingdao",
       iso3="CHN", iso2="CN", reject=_US + r"|taiwan"),
    _c("India",
       r"india\b|bengaluru|bangalore|banglore|hyderabad|pune\b|chennai|mumbai|noida"
       r"|gurgaon|gurugram|new delhi|delhi|kolkata|ahmedabad|kochi|trivandrum"
       r"|thiruvananthapuram|coimbatore|indore|jaipur|chandigarh|nagpur|vadodara|baroda"
       r"|karnataka|telangana|maharashtra|tamil nadu|haryana",
       iso3="IND", lead="IN", reject=_US + r"|indianapolis|indiana\b"),
    _c("Malaysia", r"malaysia|kuala lumpur|penang|johor|cyberjaya|petaling jaya|selangor",
       iso3="MYS", iso2="MY"),
    _c("Vietnam", r"vietnam|viet nam|ho chi minh|hanoi|ha noi|da nang", iso3="VNM", iso2="VN"),
    _c("Philippines", r"philippines|manila|makati|quezon city|taguig|cebu|pasig",
       iso3="PHL", iso2="PH"),
    _c("Thailand", r"thailand|bangkok|chiang mai", iso3="THA", iso2="TH"),
    _c("Indonesia", r"indonesia|jakarta|surabaya|bandung", iso3="IDN"),
]

REGIONS = {"europe": ("Europe", EUROPE), "asia": ("Asia", ASIA)}

_WORD_START = r"(?<![^\W_])"       # start of a word; "_" counts as a break (PB_Cen_Katowice)
_SEP = r"[\s,\-–—>|:(/]"


def compile_country(c: dict) -> dict:
    def codes(pattern: str) -> str:
        return f"(?:{pattern})"
    parts = [f"(?i:{_WORD_START}(?:{c['words']}))"]
    if c["iso3"]:
        parts.append(rf"\b{codes(c['iso3'])}\b")
    first = "|".join(x for x in (c["iso2"], c["lead"]) if x)
    if first:
        parts.append(rf"^\s*{codes(first)}(?={_SEP}|$)")
    if c["iso2"]:
        parts.append(rf"(?<={_SEP}){codes(c['iso2'])}\)?\s*$")
    return {
        "name": c["name"],
        "match": re.compile("|".join(parts)),
        "eu_dc": re.compile(f"{_WORD_START}(?:{c['eu_dc']})", re.I) if c["eu_dc"] else None,
        "reject": re.compile(c["reject"], re.I) if c["reject"] else None,
    }


_N_LOCATIONS = re.compile(r"\s*\d+\s+locations?\s*", re.I)


def location_text(job: dict) -> str:
    """The job's location; Workday says "3 Locations" for multi-site jobs, so fall back to
    the location segment of its URL (`/job/<Location>/<Title>_<Req>`)."""
    loc = (job.get("location") or "").strip()
    if loc and not _N_LOCATIONS.fullmatch(loc):
        return loc
    m = re.search(r"/job/([^/]+)/", job.get("url") or "")
    return urllib.parse.unquote(m.group(1)).replace("-", " ") if m else ""


def is_eu_datacenter(platform: str, board: str) -> bool:
    return platform == "workday" and ".wd3/" in board


def countries_of(text: str, compiled: list[dict], eu_dc: bool = False) -> list[str]:
    out = []
    for c in compiled:
        if c["reject"] and c["reject"].search(text):
            continue
        if c["match"].search(text) or (eu_dc and c["eu_dc"] and c["eu_dc"].search(text)):
            out.append(c["name"])
    return out


# ── collect ──────────────────────────────────────────────────────────────────
def company_of(platform: str, board: str) -> str:
    return board.split(".")[0] if platform == "workday" else board


def collect(cache: dict, countries: list[dict], platforms=None) -> dict[str, list[tuple]]:
    """{country: [(platform, board, roles)]}, boards with most roles first."""
    compiled = [compile_country(c) for c in countries]
    found: dict[str, dict[tuple, list]] = {c["name"]: {} for c in countries}
    for platform, state in (cache.get("platforms") or {}).items():
        if platforms and platform not in platforms:
            continue
        for board, jobs in (state.get("boards") or {}).items():
            eu_dc, seen = is_eu_datacenter(platform, board), set()
            for job in jobs:
                if job["url"] in seen or not is_swe(job.get("title") or ""):
                    continue
                seen.add(job["url"])
                for name in countries_of(location_text(job), compiled, eu_dc):
                    found[name].setdefault((platform, board), []).append(job)
    out = {}
    for name, boards in found.items():
        rows = [(p, b, sorted(r, key=lambda j: j["title"].strip().lower()))
                for (p, b), r in boards.items()]
        out[name] = sorted(rows, key=lambda r: (-len(r[2]), company_of(r[0], r[1]).lower(), r[1]))
    return out


# ── render ───────────────────────────────────────────────────────────────────
def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("[", "(").replace("]", ")").strip()


def _section_head(name: str, rows, continued: bool = False) -> list[str]:
    roles = sum(len(r[2]) for r in rows)
    more = " (continued)" if continued else f" — {len(rows)} boards, {roles} SWE roles"
    return [f"### {name}{more}", "",
            "| Company | SWE roles | Board for `ats_apply` | Example role |",
            "|---|---:|---|---|"]


def _row(platform: str, board: str, roles: list[dict]) -> str:
    job = roles[0]
    return (f"| {company_of(platform, board)} | {len(roles)} | `{platform}:{board}` "
            f"| [{_cell(job['title'])[:70]}]({job['url']}) |")


def render(region: str, by_country: dict, cache: dict, platforms=None,
           max_body: int = MAX_BODY) -> tuple[str, list[str]]:
    """(title, [issue body, comment, comment, ...])."""
    date = cache.get("date", "?")
    live = {k: v for k, v in by_country.items() if v}
    order = sorted(live, key=lambda k: (-sum(len(r[2]) for r in live[k]), k))
    boards = {(p, b) for rows in live.values() for p, b, _ in rows}
    companies = {company_of(p, b) for p, b in boards}
    urls = {j["url"] for rows in live.values() for *_, roles in rows for j in roles}
    used = sorted({p for p, _ in boards}) or sorted(platforms or [])
    label = " / ".join(p.title() for p in used) or "ATS"
    states = [s for p, s in (cache.get("platforms") or {}).items()
              if not platforms or p in platforms]
    cands = sum(s.get("candidates", 0) for s in states)
    alive = sum(len(s.get("boards") or {}) for s in states)

    title = (f"{label} employers with SWE openings in {region} "
             f"({len(boards)} boards, {date} scan)")
    intro = [
        f"{label} employers with **software-engineering openings in {region}**, from the "
        f"scan of {date}.",
        "",
        f"**{len(boards)} boards from {len(companies)} companies, {len(urls)} SWE roles** "
        f"(some companies run several boards; a board with roles in several countries is "
        f"listed under each).",
        "",
        "| Country | Boards | SWE roles |",
        "|---|---:|---:|",
        *[f"| {k} | {len(live[k])} | {sum(len(r[2]) for r in live[k])} |" for k in order],
        "",
        "### How this was made",
        f"- `{SCAN_COMMAND}` checked {cands:,} candidate boards with the same discovery code "
        f"`ats_apply` runs; {alive:,} are live. Jobs are cached in `data/ats_boards.json`. "
        f"This report is `uv run python scripts/ats_region_report.py {region.lower()}` over "
        f"that cache.",
        f"- **{region}**: matched on the job's location text, or the location segment of its "
        f"URL when the text is \"N Locations\". Covers "
        f"{', '.join(c for c in by_country)}. Place names shared with North America "
        f"(Cambridge, Birmingham, ...) count only with a country marker or on a board in "
        f"Workday's EU datacenter (`wd3`).",
        "- **SWE**: title matches software / backend / full-stack / front-end / platform / "
        "cloud / DevOps / SRE / data / ML / AI engineer / developer / SDET. Internships, "
        "co-ops, students, graduates, trainees, sales, managers and directors are excluded.",
        "- Workday boards are searched for the term \"engineer\" only, so this list is a "
        "lower bound. Counts are as of the scan date; postings close. Seniority, language "
        "and visa sponsorship were not checked.",
        "- To apply: paste the board ref (or a job URL) into **ATS Apply → Job Boards**; "
        "for Workday tick **Create Workday accounts**. Dry run first.",
        "",
    ]
    parts, cur = [], "\n".join(intro)
    for name in order:
        rows = live[name]
        block = _section_head(name, rows)
        for row in rows:
            line = _row(*row)
            if len(cur) + len("\n".join(block)) + len(line) + 2 > max_body:
                if len(block) > 4:                       # flush the rows gathered so far
                    cur += "\n" + "\n".join(block) + "\n"
                    block = _section_head(name, rows, continued=True)
                parts.append(cur)
                cur = ""
            block.append(line)
        cur += "\n" + "\n".join(block) + "\n"
    parts.append(cur)
    return title, [p.strip() + "\n" for p in parts]


# ── cli ──────────────────────────────────────────────────────────────────────
def create_issue(title: str, files: list[Path], repo: str | None) -> str:
    """Open the issue from files[0] and post the rest as comments; returns its URL."""
    scope = ["--repo", repo] if repo else []
    done = subprocess.run(["gh", "issue", "create", "--title", title, "--body-file",
                           str(files[0]), *scope], check=True, capture_output=True, text=True)
    url = done.stdout.strip().splitlines()[-1]
    for extra in files[1:]:
        subprocess.run(["gh", "issue", "comment", url, "--body-file", str(extra)], check=True,
                       capture_output=True)
    return url


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("regions", nargs="*", help=f"any of: {', '.join(REGIONS)}")
    ap.add_argument("--list", action="store_true", help="list the regions and their countries")
    ap.add_argument("--platform", action="append",
                    help="only this platform's boards (repeatable; default: all in the cache)")
    ap.add_argument("--cache", default=DEFAULT_CACHE)
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    ap.add_argument("--create-issue", action="store_true", help="open the issue with `gh`")
    ap.add_argument("--repo", help="owner/name for --create-issue (default: current repo)")
    args = ap.parse_args(argv)

    if args.list:
        for key, (_, countries) in REGIONS.items():
            print(f"{key:8} {', '.join(c['name'] for c in countries)}")
        return
    unknown = [r for r in args.regions if r not in REGIONS]
    if unknown or not args.regions:
        sys.exit(f"pick a region: {', '.join(REGIONS)}" + (f" (unknown: {unknown})" if unknown else ""))
    path = Path(args.cache)
    cache = json.loads(path.read_text()) if path.exists() else {}
    if not any(s.get("boards") for s in (cache.get("platforms") or {}).values()):
        sys.exit(f"no scan cache at {args.cache} - run `{SCAN_COMMAND}` first")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for key in args.regions:
        region, countries = REGIONS[key]
        by_country = collect(cache, countries, args.platform)
        title, parts = render(region, by_country, cache, args.platform)
        files = []
        for i, part in enumerate(parts):
            suffix = "" if i == 0 else f"_part{i + 1}"
            files.append(out_dir / f"{key}_{cache.get('date', 'scan')}{suffix}.md")
            files[-1].write_text(part)
        print(title)
        for name, rows in by_country.items():
            if rows:
                print(f"  {name:16} {len(rows):4} boards {sum(len(r[2]) for r in rows):5} roles")
        print(f"  → {', '.join(str(f) for f in files)}")
        if args.create_issue:
            print(f"  {create_issue(title, files, args.repo)}")


if __name__ == "__main__":
    main()
