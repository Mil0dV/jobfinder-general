"""Candidate target-role profile: what counts as a "fit" job.

Central definition used by agent.py for title matching (regex families),
API query seeding, location matching, and LLM validation. Values are loaded
from profile.json (written by `cli.py --setup`, gitignored) when present;
without it, the built-in defaults below apply unchanged. Set JOBFINDER_PROFILE
to load a config from a different path (useful for testing).
"""

import json
import os
import re
import sys

_PROFILE_PATH = os.environ.get(
    "JOBFINDER_PROFILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "profile.json"),
)

# --- Built-in defaults (the original hardcoded profile) --------------------

_DEFAULT_TARGET_ROLES = [
    "Entrepreneur in Residence (EiR)",
    "Product Owner / Product Lead / Head of Product",
    "Technical Product Manager / Senior Technical Product Manager",
    "Platform Product Manager / Technical Product Lead (AI / platform)",
    "CTO / VP Engineering / Director of (Software) Engineering",
    "First Engineer / Founding Engineer / Tech Lead",
    "Principal / Staff Product Engineer",
]

_DEFAULT_ROLE_PATTERNS = [
    r"entrepreneur[\s-]+in[\s-]+residence|\beir\b",
    r"(head|director|vp|vice president) of (product|engineering|software|technology|tech)\b",
    r"chief (technology|technical|product) officer|\bcto\b|\bcpo\b",
    r"(founding|first|principal|staff|lead) (software |full[\s-]?stack |platform |ai |product )?engineer",
    r"product (owner|lead|director|manager)",
    r"(technical|platform|senior|principal|staff|group|lead) product manager",
    r"tech(nical)? lead",
    r"engineering (director|lead|manager)",
    r"(produkt(manager|leiter|owner)|technische[r]? leiter|leiter (produkt|entwicklung))",
]

_DEFAULT_EXCLUDE_PATTERNS = [
    r"\b(intern|internship|werkstudent|praktik)",
    r"\bunpaid\b|\bvolunteer\b",
    r"(construction|electrical|mechanical|civil|hvac|hardware|process|sales|marketing)\s+"
    r"(engineer|manager|lead)\b",
    r"art director",
]

_DEFAULT_QUERY_SEEDS = [
    "head of product",
    "CTO",
    "technical product manager",
    "product manager",
    "director of engineering",
    "founding engineer",
    "product lead",
    "staff product engineer",
    "entrepreneur in residence",
]

# How the LLM validator anchors the 0-5 "fit" scale (see agent.validate_jobs_batch).
_DEFAULT_FIT_RUBRIC = {
    "criteria": "hands-on technical focus, product/vision ownership, and team leadership",
    "five": "exact target role at right seniority (e.g. Head of Product, CTO, Founding Engineer,\n"
    "      Technical/Platform PM, Director of Engineering, EiR) with real ownership",
    "three": "adjacent role or seniority slightly off (e.g. plain Product Manager, Engineering\n"
    "      Manager), or ownership unclear",
    "zero": "wrong role family (sales, marketing, hardware/mechanical, junior roles, internships)",
}

# Location machinery defaults (consumed by agent.py).
_DEFAULT_COUNTRY_TERMS = {
    "germany": [
        "germany",
        "deutschland",
        "berlin",
        "munich",
        "münchen",
        "hamburg",
        "frankfurt",
        "cologne",
        "köln",
        "stuttgart",
        "leipzig",
        "freiburg",
        "de",
    ],
    "switzerland": [
        "switzerland",
        "schweiz",
        "suisse",
        "zurich",
        "zürich",
        "geneva",
        "genf",
        "basel",
        "bern",
        "lausanne",
        "zug",
        "ch",
    ],
    "austria": ["austria", "österreich", "vienna", "wien", "graz", "linz", "at"],
    "netherlands": [
        "netherlands",
        "nederland",
        "amsterdam",
        "rotterdam",
        "utrecht",
        "the hague",
        "eindhoven",
        "nl",
    ],
    "europe": ["europe", "eu", "emea", "european"],
}

_DEFAULT_ADZUNA_COUNTRIES = {
    "switzerland": "ch",
    "germany": "de",
    "austria": "at",
    "netherlands": "nl",
    "uk": "gb",
    "united kingdom": "gb",
    "france": "fr",
    "usa": "us",
}
_DEFAULT_ADZUNA_DEFAULT = "ch"

_DEFAULT_CLIMATEBASE_COUNTRIES = {
    "switzerland": "Switzerland",
    "germany": "Germany",
    "austria": "Austria",
    "netherlands": "Netherlands",
    "uk": "United Kingdom",
    "france": "France",
}

_DEFAULT_TIMEZONE = "Europe/Zurich"
_DEFAULT_SALARY_REGION_HINT = "Europe"


# --- Config loading ---------------------------------------------------------


def _load_profile_config() -> dict:
    """Read profile.json; on any problem, warn and fall back to the defaults."""
    if not os.path.exists(_PROFILE_PATH):
        return {}
    try:
        with open(_PROFILE_PATH, encoding="utf-8") as f:
            config = json.load(f)
        if not isinstance(config, dict):
            raise ValueError("top-level value must be a JSON object")
        return config
    except (OSError, ValueError) as e:
        print(
            f"Warning: could not load {os.path.basename(_PROFILE_PATH)} ({e}); "
            "using built-in default profile.",
            file=sys.stderr,
        )
        return {}


def _compile_patterns(patterns: list, label: str) -> list:
    """Compile regexes one by one; skip (with a warning) any that don't compile."""
    compiled = []
    for p in patterns:
        try:
            compiled.append(re.compile(p, re.IGNORECASE))
        except re.error as e:
            print(f"Warning: skipping invalid {label} pattern {p!r}: {e}", file=sys.stderr)
    return compiled


_config = _load_profile_config()
_locations = _config.get("locations") or {}

PROFILE_EXISTS = bool(_config)

# Human-readable list, used in the LLM validation prompt.
TARGET_ROLES = _config.get("target_roles") or _DEFAULT_TARGET_ROLES

CANDIDATE_PROFILE = _config.get("candidate_profile") or (
    """\
Senior product & engineering leader looking for hands-on leadership roles at
climate/green-tech startups and scale-ups. Target roles: {roles}.
Prefers early-stage or scale-up companies. Based in Switzerland; open to roles
in Switzerland, Germany, Netherlands, or remote within Europe.""".format(
        roles="; ".join(TARGET_ROLES)
    )
)

# Regex families for local title filtering (feeds without server-side search).
# A title matches the profile if ANY pattern matches.
ROLE_PATTERNS = _config.get("role_patterns") or _DEFAULT_ROLE_PATTERNS

_COMPILED = _compile_patterns(ROLE_PATTERNS, "role")

# Titles that pass a role pattern but are almost never a fit (recruiting spam,
# unrelated fields that reuse the words).
EXCLUDE_PATTERNS = _config.get("exclude_patterns") or _DEFAULT_EXCLUDE_PATTERNS

_EXCLUDED = _compile_patterns(EXCLUDE_PATTERNS, "exclude")

# Short query seeds for sources with server-side search (Adzuna, Climatebase).
PROFILE_QUERY_SEEDS = _config.get("query_seeds") or _DEFAULT_QUERY_SEEDS

# Anchors for the LLM validator's 0-5 fit scale.
FIT_RUBRIC = {**_DEFAULT_FIT_RUBRIC, **(_config.get("fit_rubric_examples") or {})}

# Location machinery (consumed by agent.py's source searchers).
COUNTRY_TERMS = _locations.get("country_terms") or _DEFAULT_COUNTRY_TERMS
ADZUNA_COUNTRIES = _locations.get("adzuna_countries") or _DEFAULT_ADZUNA_COUNTRIES
ADZUNA_DEFAULT = (
    _locations.get("adzuna_default") if "adzuna_default" in _locations else _DEFAULT_ADZUNA_DEFAULT
)
CLIMATEBASE_COUNTRIES = _locations.get("climatebase_countries") or _DEFAULT_CLIMATEBASE_COUNTRIES
TIMEZONE = _locations.get("timezone") or _DEFAULT_TIMEZONE
SALARY_REGION_HINT = _locations.get("salary_region_hint") or _DEFAULT_SALARY_REGION_HINT

# Whether pearls automatically get the "drunk draft" cover letter treatment
# (see cover_letter.py). Explicit /drunk always works regardless.
DRUNK_MODE_ENABLED = bool(_config.get("drunk_mode", True))


# A "pearl" is a supermatch: strong on all three scores (role fit, startup-ness,
# climate), with at least one 5 among them. Pearls jump to the front of the deck
# and get the looser "drunk draft" cover letter treatment.
_pearls = _config.get("pearls") or {}
PEARL_MIN_EACH = int(_pearls.get("min_each", 4))
PEARL_MIN_TOTAL = int(_pearls.get("min_total", 13))

# "Best general roles (non-climate)": postings that fail the climate gate still
# make a deck when the ROLE fit is strong. Override via profile.json
# "nonclimate_min_fit".
NONCLIMATE_MIN_FIT = int(_config.get("nonclimate_min_fit", 4))


def is_pearl(job: dict) -> bool:
    """True when a job's three scores make it a supermatch."""
    try:
        fit = int(job.get("fit_score"))
        startup = int(job.get("startup_score"))
        climate = int(job.get("climate_score"))
    except (TypeError, ValueError):
        return False
    return min(fit, startup, climate) >= PEARL_MIN_EACH and (
        fit + startup + climate >= PEARL_MIN_TOTAL
    )


def matches_target_roles(title: str) -> bool:
    """True if a job title matches the candidate's target-role profile."""
    if not title:
        return False
    if any(p.search(title) for p in _EXCLUDED):
        return False
    return any(p.search(title) for p in _COMPILED)
