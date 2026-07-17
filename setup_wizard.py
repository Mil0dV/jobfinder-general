"""First-run setup wizard: build a personal profile.json from a resume + a few questions.

Run with `uv run python cli.py --setup` (also offered automatically when neither a
profile nor a resume exists). Ingests a resume in docx/pdf/txt, has Claude draft the
full search profile (target roles, title regexes, query seeds, location machinery),
lets the user review it, and writes profile.json — which candidate_profile.py loads
on the next start. Optionally generates a personal cover-letter styleguide from
sample letters.
"""

import json
import os
import re
import sys

from dotenv import load_dotenv

_project_root = os.path.dirname(os.path.abspath(__file__))
PROFILE_PATH = os.path.join(_project_root, "profile.json")
STYLE_GUIDE_PATH = os.path.join(_project_root, "Cover-letter styleguide.md")
ENV_PATH = os.path.join(_project_root, ".env")

# Countries Adzuna actually serves (https://developer.adzuna.com/docs/search);
# embedded in the generation prompt so the model can't invent codes.
_ADZUNA_SUPPORTED = "gb us at au be br ca ch de es fr in it mx nl nz pl sg za"

_PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "target_roles": {"type": "array", "items": {"type": "string"}},
        "candidate_profile": {"type": "string"},
        "role_patterns": {"type": "array", "items": {"type": "string"}},
        "exclude_patterns": {"type": "array", "items": {"type": "string"}},
        "query_seeds": {"type": "array", "items": {"type": "string"}},
        "fit_rubric_examples": {
            "type": "object",
            "properties": {
                "criteria": {"type": "string"},
                "five": {"type": "string"},
                "three": {"type": "string"},
                "zero": {"type": "string"},
            },
            "required": ["criteria", "five", "three", "zero"],
            "additionalProperties": False,
        },
        "locations": {
            "type": "object",
            "properties": {
                # Maps are expressed as entry arrays: the constrained-output API
                # doesn't allow schema-valued additionalProperties. Converted to
                # dicts by _entries_to_maps() before writing profile.json.
                "country_terms": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "country": {"type": "string"},
                            "terms": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["country", "terms"],
                        "additionalProperties": False,
                    },
                },
                "adzuna_countries": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "country": {"type": "string"},
                            "code": {"type": "string"},
                        },
                        "required": ["country", "code"],
                        "additionalProperties": False,
                    },
                },
                "adzuna_default": {"type": ["string", "null"]},
                "climatebase_countries": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "country": {"type": "string"},
                            "display_name": {"type": "string"},
                        },
                        "required": ["country", "display_name"],
                        "additionalProperties": False,
                    },
                },
                "timezone": {"type": "string"},
                "salary_region_hint": {"type": "string"},
            },
            "required": [
                "country_terms",
                "adzuna_countries",
                "adzuna_default",
                "climatebase_countries",
                "timezone",
                "salary_region_hint",
            ],
            "additionalProperties": False,
        },
    },
    "required": [
        "target_roles",
        "candidate_profile",
        "role_patterns",
        "exclude_patterns",
        "query_seeds",
        "fit_rubric_examples",
        "locations",
    ],
    "additionalProperties": False,
}

_GENERATION_SYSTEM = f"""You configure a climate/green-tech job search tool for a new user.
From their resume and stated preferences, produce a search profile as JSON.

Field guidance:
- target_roles: 5-8 human-readable target job titles/families, at the user's seniority.
- candidate_profile: a 3-5 line paragraph describing who they are and what they want,
  ALWAYS including their location and remote preferences and preferred company stage.
  It is pasted into an LLM prompt that scores job postings for them.
- role_patterns: Python `re` regexes (matched case-insensitively against job TITLES;
  a title matches if ANY pattern matches). Cover every target role plus common synonyms
  and seniority variants. Where a stated country has a widely-used non-English job-title
  language, add patterns for local-language titles too (e.g. German "Produktmanager").
  Keep each pattern simple and robust; prefer several small patterns over one giant one.
- exclude_patterns: regexes for titles that pass the role patterns but are never a fit.
  Always include interns/working students, unpaid/volunteer roles, and unrelated fields
  that reuse the same words (e.g. for software roles: construction/mechanical/sales
  "engineer"). Add local-language equivalents for the user's countries.
- query_seeds: at most 10 short search phrases for job boards with server-side search.
  Plain role names, no boolean syntax.
- fit_rubric_examples: anchors for a 0-5 role-fit scale used by the scoring LLM.
  "criteria": what to weigh when scoring (one line). "five": what a 5 looks like, with
  example titles. "three": what a 3 looks like. "zero": what a 0-1 looks like.
- locations.country_terms: one entry per country/region the user mentioned:
  "country" = lowercase English country name; "terms" = list of lowercase matchable
  terms — the country name in English and its local language(s), the 5-8 largest
  cities (both English and local spellings), and the ISO 3166-1 alpha-2 code.
- locations.adzuna_countries: one entry per stated country with its Adzuna country
  code. Adzuna ONLY supports: {_ADZUNA_SUPPORTED}. Omit countries not in that list.
- locations.adzuna_default: the code of the user's primary country if supported by
  Adzuna, else null.
- locations.climatebase_countries: one entry per stated country: "country" = lowercase
  English name, "display_name" = the English display name as used by climatebase.org
  facets (e.g. "uk" -> "United Kingdom").
- locations.timezone: IANA timezone of the user's primary location.
- locations.salary_region_hint: the region for salary estimates (e.g. "Europe",
  "North America").

Ground everything in the resume; never invent seniority the resume doesn't support."""


def _input(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or default


def _yes(prompt: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    val = input(f"{prompt} [{hint}]: ").strip().lower()
    if not val:
        return default
    return val in ("y", "yes")


def _ensure_api_key() -> bool:
    """Make sure ANTHROPIC_API_KEY is available, offering to write it to .env."""
    load_dotenv()
    if os.getenv("ANTHROPIC_API_KEY"):
        return True
    print("\nThe wizard uses Claude, so it needs an Anthropic API key")
    print("(get one at https://console.anthropic.com/settings/keys).")
    key = _input("Paste your ANTHROPIC_API_KEY (Enter to abort)")
    if not key:
        print("No key provided — aborting setup. Re-run with `cli.py --setup` any time.")
        return False
    os.environ["ANTHROPIC_API_KEY"] = key
    with open(ENV_PATH, "a", encoding="utf-8") as f:
        f.write(f"\nANTHROPIC_API_KEY={key}\n")
    print(f"   Saved to {ENV_PATH}.")
    print("   Optional: add ADZUNA_APP_ID / ADZUNA_API_KEY there too (see .env.example).")
    return True


def _get_client():
    import anthropic

    return anthropic.Anthropic()


def _ingest_resume() -> dict | None:
    """Ask for a resume file and import it; returns the parsed resume data."""
    from resume_data import RESUME_SOURCE_CANDIDATES, import_resume

    existing = next((p for p in RESUME_SOURCE_CANDIDATES if os.path.exists(p)), None)
    if existing and _yes(f"Found {os.path.basename(existing)} — use it?", default=True):
        path = existing
    else:
        path = _input("Path to your resume (.docx, .pdf, or .txt)")
        if not path:
            print("A resume is required — aborting setup.")
            return None
        path = os.path.expanduser(path)
        if not os.path.exists(path):
            print(f"File not found: {path}")
            return None

    try:
        data = import_resume(path)
    except ValueError as e:
        print(f"Error: {e}")
        return None

    titles = [e.get("title", "") for e in data.get("experiences", [])[:3]]
    print(f"\n   Parsed resume for: {data.get('name') or '(no name found)'}")
    for t in titles:
        print(f"     - {t}")
    if not _yes("Look right?", default=True):
        print("Fix the resume file (or resume.json) and re-run `cli.py --setup`.")
        return None
    return data


def _ask_preferences(resume: dict) -> dict:
    """Collect search preferences, with resume-derived guesses as defaults."""
    first_title = ""
    if resume.get("experiences"):
        # "Job Title — Company, Location (dates)" → "Job Title"
        first_title = re.split(r"\s+[—–-]\s+", resume["experiences"][0].get("title", ""))[0]

    print("\nA few questions about what you're looking for (Enter accepts the default):")
    answers = {
        "roles": _input("Target roles (comma-separated)", default=first_title),
        "seniority": _input("Seniority level (e.g. senior, staff, director, C-level)"),
        "locations": _input('Countries/regions (e.g. "Switzerland, Germany, remote Europe")'),
        "remote": _input("Remote preference (remote-only / hybrid / on-site OK)", default="hybrid"),
    }
    answers["drunk_mode"] = _yes(
        'Enable "drunk draft" cover letters for supermatches? (deliberately loose, '
        "unfiltered first drafts meant to be hand-rewritten)",
        default=False,
    )
    return answers


def _generate_profile(resume_text: str, answers: dict, feedback: str = "") -> dict:
    """Claude call: resume + answers → the profile.json body."""
    client = _get_client()
    user_msg = f"""RESUME:
{resume_text}

PREFERENCES:
- Target roles: {answers['roles']}
- Seniority: {answers['seniority'] or 'infer from the resume'}
- Countries/regions: {answers['locations'] or 'infer from the resume'}
- Remote preference: {answers['remote']}
"""
    if feedback:
        user_msg += f"\nREVISION FEEDBACK from the user (apply it):\n{feedback}\n"
    user_msg += "\nProduce the search profile JSON."

    print("\n   Generating your search profile with Claude...")
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        system=_GENERATION_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
        output_config={"format": {"type": "json_schema", "schema": _PROFILE_SCHEMA}},
    )
    text = next(b.text for b in response.content if b.type == "text")
    return _entries_to_maps(json.loads(text))


def _entries_to_maps(profile: dict) -> dict:
    """Convert the schema's entry arrays into the dicts profile.json uses."""
    loc = profile.get("locations") or {}
    loc["country_terms"] = {
        e["country"].lower(): [t.lower() for t in e["terms"]] for e in loc.get("country_terms", [])
    }
    loc["adzuna_countries"] = {
        e["country"].lower(): e["code"].lower() for e in loc.get("adzuna_countries", [])
    }
    loc["climatebase_countries"] = {
        e["country"].lower(): e["display_name"] for e in loc.get("climatebase_countries", [])
    }
    return profile


def _check_profile(profile: dict, answers: dict) -> list[str]:
    """Compile-check regexes (dropping broken ones) and sanity-check coverage."""
    warnings = []
    for key in ("role_patterns", "exclude_patterns"):
        kept = []
        for p in profile.get(key, []):
            try:
                re.compile(p, re.IGNORECASE)
                kept.append(p)
            except re.error as e:
                warnings.append(f"dropped invalid {key} regex {p!r}: {e}")
        profile[key] = kept

    compiled = [re.compile(p, re.IGNORECASE) for p in profile.get("role_patterns", [])]
    excluded = [re.compile(p, re.IGNORECASE) for p in profile.get("exclude_patterns", [])]
    for role in (r.strip() for r in answers["roles"].split(",") if r.strip()):
        if any(p.search(role) for p in excluded) or not any(p.search(role) for p in compiled):
            warnings.append(f"your stated target role {role!r} would NOT match the title filters")
    return warnings


def _review_profile(profile: dict, warnings: list[str]) -> str:
    """Show the generated profile; returns 'accept', 'regenerate', or 'abort'."""
    print("\n" + "=" * 60)
    print("   Generated profile")
    print("=" * 60)
    print(json.dumps(profile, indent=2, ensure_ascii=False))
    for w in warnings:
        print(f"   ⚠ {w}")
    print("\n[a]ccept and save · [r]egenerate with feedback · [q]uit")
    while True:
        choice = input("> ").strip().lower()
        if choice in ("a", "accept", ""):
            return "accept"
        if choice in ("r", "regenerate"):
            return "regenerate"
        if choice in ("q", "quit"):
            return "abort"


def _generate_style_guide(sample_paths: list[str]) -> bool:
    """Claude call: sample letters → a personal Cover-letter styleguide.md."""
    samples = []
    for p in sample_paths:
        try:
            with open(os.path.expanduser(p), encoding="utf-8") as f:
                samples.append(f.read().strip())
        except OSError as e:
            print(f"   Skipping {p}: {e}")
    if not samples:
        return False

    with open(
        os.path.join(_project_root, "Cover-letter styleguide.md.example"), encoding="utf-8"
    ) as f:
        template = f.read()

    client = _get_client()
    sample_block = "\n\n---\n\n".join(samples)
    print("   Generating your personal styleguide with Claude...")
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=(
            "You write a personal cover-letter style guide by studying someone's real "
            "letters. Follow the section structure of the template exactly, but derive "
            "every tone note, quirk, and style example from the letters themselves. "
            "Embed the provided letters verbatim in the final 'Cover letter examples' "
            "section. Output ONLY the markdown document."
        ),
        messages=[
            {
                "role": "user",
                "content": f"TEMPLATE:\n{template}\n\nTHE USER'S REAL LETTERS:\n{sample_block}",
            }
        ],
    )
    text = next(b.text for b in response.content if b.type == "text").strip()
    with open(STYLE_GUIDE_PATH, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(f"   Wrote {os.path.basename(STYLE_GUIDE_PATH)}.")
    return True


def run_setup() -> bool:
    """The full first-run flow. Returns True when profile.json was written."""
    print("=" * 60)
    print("   Climate Job Finder — first-time setup")
    print("=" * 60)

    if os.path.exists(PROFILE_PATH):
        if not _yes("profile.json already exists — overwrite it?", default=False):
            return False

    if not _ensure_api_key():
        return False

    resume = _ingest_resume()
    if resume is None:
        return False

    from resume_data import resume_as_text

    answers = _ask_preferences(resume)

    feedback = ""
    while True:
        try:
            profile = _generate_profile(resume_as_text(resume), answers, feedback)
        except Exception as e:
            print(f"Error generating profile: {e}")
            return False
        warnings = _check_profile(profile, answers)
        action = _review_profile(profile, warnings)
        if action == "abort":
            print("Setup aborted; nothing written. You can also hand-write profile.json.")
            return False
        if action == "accept":
            break
        feedback = _input("What should change")

    profile_out = {"version": 1, **profile, "drunk_mode": answers["drunk_mode"]}
    with open(PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump(profile_out, f, indent=2, ensure_ascii=False)
    print(f"\n   Wrote {os.path.basename(PROFILE_PATH)}.")

    print("\nOptional: point me at 1-2 cover letters you've written and I'll build a")
    print("personal style guide from them (otherwise a neutral built-in style is used).")
    sample = _input("Path(s) to sample letter(s), comma-separated (Enter to skip)")
    if sample:
        _generate_style_guide([p.strip() for p in sample.split(",") if p.strip()])

    print("\nSetup complete. Restart the tool to pick up the new profile:")
    print("   uv run python cli.py --tui")
    return True


if __name__ == "__main__":
    try:
        run_setup()
    except (KeyboardInterrupt, EOFError):
        print("\nSetup aborted.")
        sys.exit(1)
