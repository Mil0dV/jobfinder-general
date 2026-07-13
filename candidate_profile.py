"""Candidate target-role profile: what counts as a "fit" job.

Central definition used by agent.py for title matching (regex families),
API query seeding, and LLM validation. Edit TARGET_ROLES / ROLE_PATTERNS
to change what the profile search looks for.
"""

import re

# Human-readable list, used in the LLM validation prompt.
TARGET_ROLES = [
    "Entrepreneur in Residence (EiR)",
    "Product Owner / Product Lead / Head of Product",
    "Technical Product Manager / Senior Technical Product Manager",
    "Platform Product Manager / Technical Product Lead (AI / platform)",
    "CTO / VP Engineering / Director of (Software) Engineering",
    "First Engineer / Founding Engineer / Tech Lead",
    "Principal / Staff Product Engineer",
]

CANDIDATE_PROFILE = """\
Senior product & engineering leader looking for hands-on leadership roles at
climate/green-tech startups and scale-ups. Target roles: {roles}.
Prefers early-stage or scale-up companies. Based in Switzerland; open to roles
in Switzerland, Germany, Netherlands, or remote within Europe.""".format(
    roles="; ".join(TARGET_ROLES)
)

# Regex families for local title filtering (feeds without server-side search).
# A title matches the profile if ANY pattern matches.
ROLE_PATTERNS = [
    r"entrepreneur[\s-]+in[\s-]+residence|\beir\b",
    r"(head|director|vp|vice president) of (product|engineering|software|technology|tech)\b",
    r"chief (technology|technical|product) officer|\bcto\b|\bcpo\b",
    r"(founding|first|principal|staff|lead) (software |full[\s-]?stack |platform |ai |product )?engineer",
    r"product (owner|lead|director)",
    r"(technical|platform|senior|principal|staff|group|lead) product manager",
    r"tech(nical)? lead",
    r"engineering (director|lead|manager)",
    r"(produkt(manager|leiter|owner)|technische[r]? leiter|leiter (produkt|entwicklung))",
]

_COMPILED = [re.compile(p, re.IGNORECASE) for p in ROLE_PATTERNS]

# Titles that pass a role pattern but are almost never a fit (recruiting spam,
# unrelated fields that reuse the words).
EXCLUDE_PATTERNS = [
    r"\b(intern|internship|werkstudent|praktik)",
    r"\bunpaid\b|\bvolunteer\b",
    r"(construction|electrical|mechanical|civil|hvac|hardware|process|sales|marketing)\s+"
    r"(engineer|manager|lead)\b",
    r"art director",
]

_EXCLUDED = [re.compile(p, re.IGNORECASE) for p in EXCLUDE_PATTERNS]

# Short query seeds for sources with server-side search (Adzuna, Climatebase).
PROFILE_QUERY_SEEDS = [
    "head of product",
    "CTO",
    "technical product manager",
    "director of engineering",
    "founding engineer",
    "product lead",
    "staff product engineer",
    "entrepreneur in residence",
]


# A "pearl" is a supermatch: strong on all three scores (role fit, startup-ness,
# climate), with at least one 5 among them. Pearls jump to the front of the deck
# and get the looser "drunk draft" cover letter treatment.
PEARL_MIN_EACH = 4
PEARL_MIN_TOTAL = 13


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
