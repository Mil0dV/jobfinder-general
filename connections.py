"""LinkedIn network job search.

Turns a LinkedIn ``Connections.csv`` export into a job source: group the
connections by company, keep the climate-relevant ones (batched LLM pass),
find each company's job board, and return matching openings tagged with the
person you know there.

Four stages, each disk-cached so the slow first run happens only once:

1. ``load_connections``  — parse the export (it has a ``Notes:`` preamble).
2. ``companies``         — normalize + group contacts by company.
3. ``classify_companies``— climate score per company (connections_companies.json).
4. ``resolve_board``     — ATS slug probes → web search → careers-page scrape
                           (connections_boards.json, with a TTL).

``search_connections`` has the same ``(query, location)`` signature as every
other source in ``agent.ALL_SOURCES``, so the usual pipeline (dedup, liveness,
description fetch, LLM validation, ranking) applies unchanged.
"""

import os
import re
import csv
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from agent import (
    _get_anthropic_client,
    _make_job,
    _matches_location,
    _location_terms,
    _strip_apply_suffix,
    _title_matches,
    VALIDATION_MODEL,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONNECTIONS_CSV = os.path.join(BASE_DIR, "connections.csv")
COMPANIES_CACHE = os.path.join(BASE_DIR, "connections_companies.json")
BOARDS_CACHE = os.path.join(BASE_DIR, "connections_boards.json")

MIN_CLIMATE = 3  # company climate score needed before we go looking for its board
HIT_TTL_DAYS = 30  # re-resolve a found board after this long
MISS_TTL_DAYS = 14  # re-try a company we couldn't resolve after this long
MAX_SCRAPED_PER_PAGE = 40  # cap on listings taken off one scraped careers page

_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) JobFinder/1.0"}
_TIMEOUT = 8

# Legal suffixes / filler stripped when keying a company name.
_SUFFIXES = {
    "gmbh",
    "ag",
    "ug",
    "kg",
    "mbh",
    "bv",
    "nv",
    "cv",
    "inc",
    "incorporated",
    "ltd",
    "limited",
    "llc",
    "llp",
    "plc",
    "sa",
    "sas",
    "sarl",
    "srl",
    "spa",
    "oy",
    "ab",
    "as",
    "aps",
    "holding",
    "holdings",
    "group",
    "the",
    "co",
    "corp",
    "corporation",
    "company",
}

# Company names that carry no signal (freelancers, students, job seekers).
_NOISE_NAMES = {
    "self employed",
    "self-employed",
    "freelance",
    "freelancer",
    "independent",
    "student",
    "none",
    "n/a",
    "unemployed",
    "retired",
}


# --- 1. PARSE ---------------------------------------------------------------


def load_connections(path: str = "") -> list:
    """Parse a LinkedIn Connections.csv export.

    The export starts with a "Notes:" preamble before the real header row, so
    skip forward until the line beginning with "First Name". Rows without a
    company are dropped — they carry nothing we can search.
    """
    path = path or CONNECTIONS_CSV
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        lines = f.read().splitlines()

    start = next(
        (i for i, line in enumerate(lines) if line.lstrip('"').startswith("First Name")), None
    )
    if start is None:
        raise ValueError(f"{path} has no 'First Name' header row — is it a LinkedIn export?")

    conns = []
    for row in csv.DictReader(lines[start:]):
        company = (row.get("Company") or "").strip()
        if not company:
            continue
        first = (row.get("First Name") or "").strip()
        last = (row.get("Last Name") or "").strip()
        conns.append(
            {
                "name": f"{first} {last}".strip(),
                "company": company,
                "position": (row.get("Position") or "").strip(),
                "url": (row.get("URL") or "").strip(),
                "connected_on": (row.get("Connected On") or "").strip(),
            }
        )
    return conns


# --- 2. GROUP ---------------------------------------------------------------


def company_key(name: str) -> str:
    """Normalize a company name: lowercase, punctuation and legal suffixes gone."""
    text = re.sub(r"[^\w\s&+-]", " ", (name or "").lower())
    text = text.replace("&", " and ")
    words = [w for w in re.split(r"[\s\-_/]+", text) if w]
    while words and words[-1] in _SUFFIXES:
        words.pop()
    while words and words[0] in _SUFFIXES:
        words.pop(0)
    return " ".join(words).strip()


def companies(conns: list) -> dict:
    """Group connections by normalized company key.

    Returns {key: {"display": str, "contacts": [connection, ...]}}. The display
    name is the most common spelling among the contacts.
    """
    grouped = {}
    for c in conns:
        key = company_key(c["company"])
        if not key or key in _NOISE_NAMES or len(key) < 2:
            continue
        entry = grouped.setdefault(key, {"display": c["company"], "contacts": [], "_names": {}})
        entry["contacts"].append(c)
        entry["_names"][c["company"]] = entry["_names"].get(c["company"], 0) + 1
    for entry in grouped.values():
        entry["display"] = max(entry.pop("_names").items(), key=lambda kv: kv[1])[0]
    return grouped


# --- CACHES -----------------------------------------------------------------

_cache_lock = threading.Lock()


def _load_cache(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(path: str, data: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
    except OSError:
        pass


def _fresh(entry: dict, ttl_days: int) -> bool:
    ts = entry.get("ts", 0)
    return (time.time() - ts) < ttl_days * 86400


# --- 3. CLIMATE PRE-FILTER --------------------------------------------------

_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "companies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "climate": {"type": "integer"},
                    "note": {"type": "string"},
                },
                "required": ["id", "climate", "note"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["companies"],
    "additionalProperties": False,
}

_CLASSIFY_BATCH = 60


def _classify_batch(batch: list) -> dict:
    """Score one batch of [(key, display)] for climate relevance. {} on failure."""
    listing = "".join(f"ID {i}: {display}\n" for i, (_, display) in enumerate(batch))
    prompt = f"""Rate how climate/sustainability-relevant each company is.

COMPANIES:
{listing}

Respond with JSON: a "companies" array with one object per ID, each with:
- id: the ID from the list above.
- climate: 0-5.
  5 = core climate mission (carbon removal, renewables, grid, climate SaaS, mobility)
  3 = meaningful sustainability angle (a real green product line or programme)
  0-1 = no climate connection, or fossil/heavy-polluter
  Use 0 if you do not recognise the company at all — do not guess upward.
- note: at most six words on what the company does.

Example:
{{"companies": [{{"id": 0, "climate": 5, "note": "carbon accounting software"}}]}}"""

    try:
        response = _get_anthropic_client().messages.create(
            model=VALIDATION_MODEL,
            max_tokens=4000,
            output_config={"format": {"type": "json_schema", "schema": _CLASSIFY_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        text = next(b.text for b in response.content if b.type == "text")
        data = json.loads(text)
    except Exception as e:
        print(f"   Company classification error ({VALIDATION_MODEL}): {e}")
        return {}

    out = {}
    for item in data.get("companies", []):
        try:
            key, display = batch[int(item["id"])]
        except (KeyError, ValueError, TypeError, IndexError):
            continue
        try:
            climate = int(item.get("climate", 0))
        except (TypeError, ValueError):
            climate = 0
        out[key] = {
            "display": display,
            "climate": max(0, min(5, climate)),
            "note": str(item.get("note", ""))[:80],
            "ts": time.time(),
        }
    return out


def classify_companies(grouped: dict, verbose: bool = True) -> dict:
    """Climate-score every company, using and updating connections_companies.json.

    Returns {key: {"display", "climate", "note"}}. Without an LLM, every company
    gets climate=MIN_CLIMATE so the search still runs (nothing is pre-filtered).
    """
    import llm

    cache = _load_cache(COMPANIES_CACHE)
    todo = [(k, v["display"]) for k, v in grouped.items() if k not in cache]

    if todo and not llm.api_key():
        if verbose:
            print("   No LLM available — skipping the climate pre-filter (probing everything).")
        return {
            k: {"display": v["display"], "climate": MIN_CLIMATE, "note": "unclassified"}
            for k, v in grouped.items()
        }

    if todo:
        batches = [todo[i : i + _CLASSIFY_BATCH] for i in range(0, len(todo), _CLASSIFY_BATCH)]
        if verbose:
            print(f"   Classifying {len(todo)} new companies in {len(batches)} batches...")
        with ThreadPoolExecutor(max_workers=4) as ex:
            for result in ex.map(_classify_batch, batches):
                cache.update(result)
        _save_cache(COMPANIES_CACHE, cache)

    return {k: cache[k] for k in grouped if k in cache}


# --- 4. BOARD DISCOVERY -----------------------------------------------------


def _slug_candidates(display: str) -> list:
    """Plausible ATS slugs for a company name, best guess first.

    Only slugs built from the *whole* name — a first-word guess ("sunrise" for
    "Sunrise Energy Zurich") happily lands on an unrelated company's board and
    would then tag its jobs with the wrong contact. Names whose real slug is
    shorter than this still get found by the web-search tier.
    """
    words = company_key(display).split()
    if not words:
        return []
    out = []
    for cand in ("".join(words), "-".join(words)):
        if len(cand) >= 3 and cand not in out:
            out.append(cand)
    return out


def _get_json(url: str):
    import requests

    r = requests.get(url, headers=_HTTP_HEADERS, timeout=_TIMEOUT)
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def _iso(value, ms: bool = False) -> str:
    """Best-effort date → YYYY-MM-DD."""
    if not value:
        return ""
    try:
        if ms or isinstance(value, (int, float)):
            secs = float(value) / 1000 if ms else float(value)
            return datetime.fromtimestamp(secs, tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return ""
    return str(value)[:10]


def _fetch_greenhouse(slug: str):
    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    if not isinstance(data, dict) or "jobs" not in data:
        return None
    return [
        {
            "title": j.get("title", ""),
            "url": j.get("absolute_url", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "posted_date": _iso(j.get("updated_at")),
        }
        for j in data.get("jobs", [])
    ]


def _fetch_lever(slug: str):
    data = _get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not isinstance(data, list):
        return None
    return [
        {
            "title": j.get("text", ""),
            "url": j.get("hostedUrl", ""),
            "location": (j.get("categories") or {}).get("location", "") or "",
            "posted_date": _iso(j.get("createdAt"), ms=True),
            "snippet": j.get("descriptionPlain", ""),
            "wfh_policy": "Remote" if j.get("workplaceType") == "remote" else "",
        }
        for j in data
    ]


def _fetch_ashby(slug: str):
    data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if not isinstance(data, dict) or "jobs" not in data:
        return None
    return [
        {
            "title": j.get("title", ""),
            "url": _strip_apply_suffix(j.get("jobUrl", "") or j.get("applyUrl", "")),
            "location": j.get("location", "") or "",
            "posted_date": _iso(j.get("publishedAt")),
            "wfh_policy": "Remote" if j.get("isRemote") else "",
        }
        for j in data.get("jobs", [])
    ]


def _fetch_workable(slug: str):
    data = _get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
    if not isinstance(data, dict) or "jobs" not in data:
        return None
    out = []
    for j in data.get("jobs", []):
        loc = ", ".join(x for x in (j.get("city"), j.get("country")) if x)
        out.append(
            {
                "title": j.get("title", ""),
                "url": j.get("url", "")
                or f"https://apply.workable.com/{slug}/j/{j.get('shortcode', '')}/",
                "location": j.get("location", "") or loc,
                "posted_date": _iso(j.get("published")),
            }
        )
    return out


def _fetch_recruitee(slug: str):
    data = _get_json(f"https://{slug}.recruitee.com/api/offers/")
    if not isinstance(data, dict) or "offers" not in data:
        return None
    return [
        {
            "title": j.get("title", ""),
            "url": _strip_apply_suffix(j.get("careers_url", "") or j.get("careers_apply_url", "")),
            "location": ", ".join(x for x in (j.get("city"), j.get("country")) if x)
            or j.get("location", ""),
            "posted_date": _iso(j.get("published_at")),
        }
        for j in data.get("offers", [])
    ]


def _fetch_personio(slug: str):
    data = _get_json(f"https://{slug}.jobs.personio.de/search.json")
    if not isinstance(data, list):
        return None
    return [
        {
            "title": j.get("name", ""),
            "url": f"https://{slug}.jobs.personio.de/job/{j.get('id', '')}",
            "location": j.get("office", "") or "",
            "posted_date": _iso(j.get("createdAt")),
        }
        for j in data
        if isinstance(j, dict) and j.get("id")
    ]


def _fetch_smartrecruiters(slug: str):
    data = _get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100")
    if not isinstance(data, dict) or "content" not in data:
        return None
    out = []
    for j in data.get("content", []):
        loc = j.get("location") or {}
        out.append(
            {
                "title": j.get("name", ""),
                "url": j.get("ref", "")
                or f"https://jobs.smartrecruiters.com/{slug}/{j.get('id', '')}",
                "location": ", ".join(
                    x for x in (loc.get("city"), loc.get("country")) if x and x != "null"
                ),
                "posted_date": _iso(j.get("releasedDate")),
            }
        )
    return out


# SmartRecruiters answers 200 with an empty list for *any* slug, so an empty
# result there proves nothing. Every other provider 404s on an unknown slug,
# which makes their empty list trustworthy: a real board with nothing open.
_PERMISSIVE_PROVIDERS = {"smartrecruiters"}


def _accepts(provider: str, postings) -> bool:
    if postings is None:
        return False
    return bool(postings) or provider not in _PERMISSIVE_PROVIDERS


# Ordered by how likely a climate-tech company is to use them.
PROVIDERS = {
    "greenhouse": _fetch_greenhouse,
    "lever": _fetch_lever,
    "ashby": _fetch_ashby,
    "personio": _fetch_personio,
    "recruitee": _fetch_recruitee,
    "workable": _fetch_workable,
    "smartrecruiters": _fetch_smartrecruiters,
}

# ATS URLs as they appear in web-search results → (provider, slug).
_ATS_URL_PATTERNS = [
    (re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([\w-]+)"), "greenhouse"),
    (re.compile(r"jobs\.lever\.co/([\w-]+)"), "lever"),
    (re.compile(r"jobs\.ashbyhq\.com/([\w-]+)"), "ashby"),
    (re.compile(r"apply\.workable\.com/([\w-]+)"), "workable"),
    (re.compile(r"([\w-]+)\.recruitee\.com"), "recruitee"),
    (re.compile(r"([\w-]+)\.jobs\.personio\.(?:de|com)"), "personio"),
    (re.compile(r"jobs\.smartrecruiters\.com/([\w-]+)"), "smartrecruiters"),
]

_CAREERS_HINT = re.compile(r"/(careers?|jobs|join-us|vacancies|werken-bij|stellen)\b", re.I)

# Job aggregators: their pages list other companies' roles, so scraping them
# would credit the wrong company. Only the employer's own site is scraped.
_AGGREGATORS = (
    "linkedin.com",
    "indeed.",
    "glassdoor.",
    "builtin.com",
    "wellfound.com",
    "angel.co",
    "ycombinator.com",
    "monster.",
    "ziprecruiter.",
    "jobs.ch",
    "jooble.",
    "trueup.io",
    "climatebase.org",
    "climatetechlist.com",
    "welcometothejungle.com",
    "stepstone.",
    "xing.com",
    "talent.com",
)


def _probe_providers(display: str):
    """Tier 1: guess slugs and probe the ATS APIs. Returns (info, postings) or None.

    An empty-but-well-formed response still counts: the board exists, the
    company just has nothing open today.
    """
    slugs = _slug_candidates(display)
    for provider, fetch in PROVIDERS.items():
        for slug in slugs:
            try:
                postings = fetch(slug)
            except Exception:
                continue
            if _accepts(provider, postings):
                return {"provider": provider, "slug": slug}, postings
    return None


def _plausible_careers_url(url: str, display: str) -> bool:
    """A careers page is only usable if it is plausibly the company's own site."""
    low = url.lower()
    if any(a in low for a in _AGGREGATORS):
        return False
    host = low.split("//")[-1].split("/")[0].replace("www.", "")
    words = company_key(display).split()
    squashed = "".join(words)
    return squashed in host.replace("-", "").replace(".", "") or any(
        len(w) >= 4 and w in host for w in words
    )


def _search_web(display: str):
    """Tier 2: web-search the company's careers page. Returns (info, postings) or None."""
    try:
        from ddgs import DDGS

        with DDGS() as ddgs:
            results = list(ddgs.text(f"{display} careers jobs openings", max_results=6))
    except Exception:
        return None

    careers_url = ""
    for r in results:
        url = r.get("href") or r.get("url") or ""
        for pattern, provider in _ATS_URL_PATTERNS:
            m = pattern.search(url)
            if not m:
                continue
            slug = m.group(1)
            try:
                postings = PROVIDERS[provider](slug)
            except Exception:
                postings = None
            if _accepts(provider, postings):
                return {"provider": provider, "slug": slug}, postings
        if not careers_url and _CAREERS_HINT.search(url) and _plausible_careers_url(url, display):
            careers_url = url
    if careers_url:
        return {"provider": "web", "slug": "", "url": careers_url}, None
    return None


_ANCHOR_RE = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")

_SCRAPE_SCHEMA = {
    "type": "object",
    "properties": {
        "jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "location": {"type": "string"},
                },
                "required": ["title", "location"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["jobs"],
    "additionalProperties": False,
}


def _scrape_careers_page(url: str, query: str) -> list:
    """Tier 3: pull job listings off a plain careers page.

    First pass is deterministic: every <a> whose text matches the role profile
    becomes a posting. If the page has no usable anchors (JS-rendered), fall
    back to the extracted page text and let the fast model list the openings —
    those get the careers-page URL, which is the best link available.
    """
    import requests
    from urllib.parse import urljoin

    html = ""
    try:
        r = requests.get(url, headers=_HTTP_HEADERS, timeout=_TIMEOUT)
        if r.status_code == 200:
            html = r.text
    except Exception:
        pass

    out, seen = [], set()
    for href, inner in _ANCHOR_RE.findall(html):
        title = re.sub(r"\s+", " ", _TAG_RE.sub(" ", inner)).strip()
        if not title or len(title) > 120 or not _title_matches(title, query):
            continue
        link = urljoin(url, href)
        if link in seen:
            continue
        seen.add(link)
        out.append({"title": title, "url": link, "location": "", "posted_date": ""})
        if len(out) >= MAX_SCRAPED_PER_PAGE:
            return out
    if out:
        return out

    # JS-only page: extracted text + one cheap LLM pass.
    try:
        from cover_letter import fetch_job_description

        text = fetch_job_description(url)
    except Exception:
        text = ""
    if not text or len(text) < 100:
        return []
    try:
        response = _get_anthropic_client().messages.create(
            model=VALIDATION_MODEL,
            max_tokens=1500,
            output_config={"format": {"type": "json_schema", "schema": _SCRAPE_SCHEMA}},
            messages=[
                {
                    "role": "user",
                    "content": (
                        "List the open job positions on this careers page. Return only real "
                        "job openings with their location if stated (empty string if not). "
                        "If the page lists no openings, return an empty array.\n\n"
                        f"PAGE:\n{text[:6000]}"
                    ),
                }
            ],
        )
        data = json.loads(next(b.text for b in response.content if b.type == "text"))
    except Exception:
        return []

    for j in data.get("jobs", [])[:MAX_SCRAPED_PER_PAGE]:
        title = str(j.get("title", "")).strip()
        if title and _title_matches(title, query):
            out.append(
                {
                    "title": title,
                    "url": url,
                    "location": str(j.get("location", "")).strip(),
                    "posted_date": "",
                }
            )
    return out


def resolve_board(key: str, display: str, use_cache: bool = True):
    """Find a company's job board. Returns ({"provider","slug","url"}, postings|None).

    postings is None when the caller has to fetch them itself (cache hit, or a
    tier-3 careers page that is scraped per search). ({}, None) means no board.
    """
    if use_cache:
        with _cache_lock:
            entry = _load_cache(BOARDS_CACHE).get(key)
        if entry:
            found = bool(entry.get("provider"))
            if _fresh(entry, HIT_TTL_DAYS if found else MISS_TTL_DAYS):
                return ({k: entry[k] for k in ("provider", "slug", "url") if k in entry}, None)

    result = _probe_providers(display) or _search_web(display)
    info, postings = result if result else ({}, None)

    with _cache_lock:
        cache = _load_cache(BOARDS_CACHE)
        cache[key] = {**info, "display": display, "ts": time.time()}
        _save_cache(BOARDS_CACHE, cache)
    return info, postings


def board_postings(key: str, display: str, query: str) -> list:
    """Resolve a company's board and return its raw postings."""
    info, postings = resolve_board(key, display)
    if postings is not None:
        return postings
    provider = info.get("provider")
    if not provider:
        return []
    if provider == "web":
        return _scrape_careers_page(info.get("url", ""), query) if info.get("url") else []
    try:
        return PROVIDERS[provider](info.get("slug", "")) or []
    except Exception:
        return []


# --- 5. SOURCE --------------------------------------------------------------


def _contact_line(contact: dict) -> str:
    position = contact.get("position", "")
    return f"{contact['name']} — {position}" if position else contact["name"]


def network_targets(path: str = "", verbose: bool = True) -> list:
    """Companies worth probing: [(key, display, contacts, climate)], best first."""
    conns = load_connections(path)
    grouped = companies(conns)
    if verbose:
        print(f"   {len(conns)} connections across {len(grouped)} companies")
    scored = classify_companies(grouped, verbose=verbose)
    targets = [
        (key, scored[key]["display"], grouped[key]["contacts"], scored[key]["climate"])
        for key in grouped
        if key in scored and scored[key]["climate"] >= MIN_CLIMATE
    ]
    targets.sort(key=lambda t: (-t[3], t[1].lower()))
    if verbose:
        print(f"   {len(targets)} climate-relevant companies (climate ≥ {MIN_CLIMATE})")
    return targets


def import_connections(path: str) -> dict:
    """Copy a LinkedIn export into place and run the (cached) climate pass once.

    Returns {"connections", "companies", "relevant"}. Raises on an unreadable or
    non-LinkedIn CSV so the caller can report it.
    """
    import shutil

    src = os.path.expanduser(path.strip().strip("'\""))
    conns = load_connections(src)  # validate before overwriting anything
    if os.path.abspath(src) != os.path.abspath(CONNECTIONS_CSV):
        shutil.copyfile(src, CONNECTIONS_CSV)
        print(f"   Imported → {os.path.basename(CONNECTIONS_CSV)}")

    grouped = companies(conns)
    print(f"   {len(conns)} connections across {len(grouped)} companies")
    scored = classify_companies(grouped)
    relevant = sorted(
        (v for v in scored.values() if v["climate"] >= MIN_CLIMATE),
        key=lambda v: (-v["climate"], v["display"].lower()),
    )
    print(f"   {len(relevant)} climate-relevant companies (climate ≥ {MIN_CLIMATE}):")
    for v in relevant[:20]:
        print(f"      [{v['climate']}] {v['display']} — {v['note']}")
    if len(relevant) > 20:
        print(f"      … and {len(relevant) - 20} more")
    return {"connections": len(conns), "companies": len(grouped), "relevant": len(relevant)}


def search_connections(query: str, location: str = "", _progress=None) -> list:
    """Search the job boards of companies where the candidate knows someone."""
    if not os.path.exists(CONNECTIONS_CSV):
        print(f"   No {os.path.basename(CONNECTIONS_CSV)} — run: cli.py --import-connections PATH")
        return []
    try:
        targets = network_targets()
    except (OSError, ValueError) as e:
        print(f"   Connections error: {e}")
        return []
    if not targets:
        return []

    terms = _location_terms(location)
    jobs, done = [], 0
    lock = threading.Lock()

    def work(target):
        key, display, contacts, _climate = target
        found = []
        try:
            postings = board_postings(key, display, query)
        except Exception:
            postings = []
        contact = _contact_line(contacts[0])
        for p in postings:
            title = p.get("title", "")
            loc = p.get("location", "")
            if not title or not p.get("url"):
                continue
            if not _title_matches(title, query):
                continue
            # Unknown location passes here; the pipeline's location policy decides.
            if loc and not _matches_location(loc, terms):
                continue
            job = _make_job(
                title=title,
                company=display,
                url=p.get("url"),
                location=loc,
                posted_date=p.get("posted_date", ""),
                wfh_policy=p.get("wfh_policy") or ("Remote" if "remote" in loc.lower() else ""),
                snippet=p.get("snippet", ""),
                source="network",
            )
            job["contact"] = contact
            job["contact_url"] = contacts[0].get("url", "")
            job["contacts_n"] = len(contacts)
            found.append(job)
        return found

    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = [ex.submit(work, t) for t in targets]
        for future in as_completed(futures):
            with lock:
                done += 1
                if _progress:
                    _progress("network", done, len(targets), f"{len(jobs)} openings")
            try:
                jobs.extend(future.result())
            except Exception:
                pass

    print(f"   Network: {len(jobs)} matching jobs from {len(targets)} companies")
    return jobs
