"""Resume data loading — single source of truth is Resume_base.<docx|pdf|txt>.

You maintain your resume in a Resume_base file (docx, pdf, or plain text; the setup
wizard's import_resume() copies your resume here in whatever format you have). The
structured resume.json used by the tailoring and cover-letter code is *derived* from
it: parsed once by Claude and cached. It is regenerated automatically whenever the
source content changes, detected via a content hash stored in resume.json under
"_source_hash".

If no source file is present, a hand-written resume.json is used as-is (see
resume.json.example). All of these files are gitignored — they're personal data.
"""

import hashlib
import json
import os
import re
import sys


import llm
from doc_text import extract_text
from dotenv import load_dotenv

load_dotenv()

_project_root = os.path.dirname(os.path.abspath(__file__))

RESUME_DOCX_PATH = os.path.join(_project_root, "Resume_base.docx")
RESUME_JSON_PATH = os.path.join(_project_root, "resume.json")

# Possible resume source files, in resolution order; first match wins.
RESUME_SOURCE_CANDIDATES = [
    os.path.join(_project_root, name)
    for name in ["Resume_base.docx", "Resume_base.pdf", "Resume_base.txt", "resume.txt"]
]

_client = None


def _get_client():
    global _client
    if _client is None:
        if not llm.api_key():
            print(f"Error: {llm.api_key_env()} not set.")
            sys.exit(1)
        _client = llm.get_client()
    return _client


# Top-level keys build_resume_docx expects, with the empty value to fall back to
# if a parsed resume happens to omit one.
_RESUME_DEFAULTS = {
    "name": "",
    "contact": "",
    "professional_statement": "",
    "core_competencies": "",
    "technical_skills": "",
    "experiences": [],
    "earlier_roles": [],
    "education": [],
    "languages": "",
    "interests": "",
}

# JSON schema shown to Claude when parsing the resume text.
_RESUME_SCHEMA = """\
{
  "name": "Full Name",
  "contact": "City, Country | phone | email | LinkedIn: ...",
  "professional_statement": "2-4 sentence summary paragraph.",
  "core_competencies": "Skill • Skill • Skill (a single bullet-separated string)",
  "technical_skills": "Tool • Tool • Language (a single bullet-separated string)",
  "experiences": [
    {
      "title": "Job Title — Company, Location (Start – End)",
      "subtitle": "One-line description of the company.",
      "scope": ["Responsibility bullet", "..."],
      "achievements": ["Quantified achievement bullet", "..."]
    }
  ],
  "earlier_roles": ["Title, Company (Year–Year)", "..."],
  "education": ["Degree, Institution (Year)", "..."],
  "languages": "Language (level) • Language (level)",
  "interests": "Interest • Interest • Interest"
}"""

_base_resume = None


def _validate_resume_data(data: dict) -> dict:
    """Fill any missing top-level keys so the docx builder won't KeyError."""
    for key, default in _RESUME_DEFAULTS.items():
        data.setdefault(key, list(default) if isinstance(default, list) else default)
    return data


def extract_resume_text(path: str) -> str:
    """Extract plain text from a resume file (.docx, .pdf, or plain text)."""
    return extract_text(path)


def _docx_hash(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _parse_resume_text_to_json(text: str) -> dict:
    """Use Claude to parse resume text into the structured JSON schema."""
    client = _get_client()
    system = (
        "You convert a plain-text resume into structured JSON. "
        "Extract the information faithfully -- never invent or embellish. "
        "Group scope/achievement bullets under the experience they belong to. "
        "Return ONLY valid JSON matching this exact schema "
        "(no markdown fences, no commentary):\n\n" + _RESUME_SCHEMA
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": f"Resume text:\n\n{text}"}],
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
        raw = re.sub(r"\n?```\s*$", "", raw)

    try:
        return _validate_resume_data(json.loads(raw))
    except json.JSONDecodeError as e:
        print(f"Error: Could not parse Claude's resume JSON: {e}")
        print(raw[:500])
        sys.exit(1)


def _generate_json_from_source(source_path: str) -> dict:
    """Parse a resume source file into resume.json, stamped with its content hash."""
    print(f"   Parsing {os.path.basename(source_path)} into resume.json with Claude...")
    data = _parse_resume_text_to_json(extract_resume_text(source_path))
    data["_source_hash"] = _docx_hash(source_path)
    with open(RESUME_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return data


def _load_cached_json() -> dict | None:
    if not os.path.exists(RESUME_JSON_PATH):
        return None
    try:
        with open(RESUME_JSON_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def get_resume_data() -> dict:
    """Return the structured resume, loading and caching it on first use.

    Resolution order:
      1. The first existing Resume_base.<docx|pdf|txt> (source of truth) — derive
         resume.json from it, regenerating whenever the source content changes.
      2. Otherwise, a hand-written resume.json, used as-is.
      3. Otherwise, exit with guidance.
    """
    global _base_resume
    if _base_resume is not None:
        return _base_resume

    cached = _load_cached_json()
    source_path = next((p for p in RESUME_SOURCE_CANDIDATES if os.path.exists(p)), None)

    if source_path:
        current_hash = _docx_hash(source_path)
        if cached and cached.get("_source_hash") == current_hash:
            data = cached
        else:
            if cached:
                print(f"   {os.path.basename(source_path)} changed -- regenerating resume.json...")
            data = _generate_json_from_source(source_path)
    elif cached is not None:
        data = cached
    else:
        print(
            "Error: no resume found. Run `uv run python cli.py --setup`, add your resume "
            "as Resume_base.docx/.pdf/.txt, or create resume.json (see resume.json.example)."
        )
        sys.exit(1)

    data.pop("_source_hash", None)
    _base_resume = _validate_resume_data(data)
    return _base_resume


def import_resume(path: str) -> dict:
    """Import a user-supplied resume (docx/pdf/txt) as the tool's resume source.

    Copies the file into the project root as Resume_base.<ext> (so the usual
    hash-based regeneration keeps working when it's edited later), parses it
    into resume.json with Claude, and returns the structured data.
    """
    import shutil

    global _base_resume

    ext = os.path.splitext(path)[1].lower()
    if ext not in (".docx", ".pdf", ".txt"):
        raise ValueError(f"Unsupported resume format {ext!r}; use .docx, .pdf, or .txt")

    dest = os.path.join(_project_root, f"Resume_base{ext}")
    if os.path.abspath(path) != dest:
        shutil.copyfile(path, dest)

    data = _generate_json_from_source(dest)
    data.pop("_source_hash", None)
    _base_resume = _validate_resume_data(data)
    return _base_resume


def candidate_name(data: dict | None = None) -> str:
    """The candidate's name, falling back to a generic label."""
    data = data if data is not None else get_resume_data()
    return (data.get("name") or "").strip() or "the applicant"


def resume_as_text(data: dict | None = None) -> str:
    """Render the structured resume as plain text, for use as model context."""
    data = data if data is not None else get_resume_data()
    lines: list[str] = []

    if data.get("name"):
        lines.append(data["name"])
    if data.get("contact"):
        lines.append(data["contact"])

    def section(title: str, body: str):
        if body:
            lines.extend(["", title, body])

    section("Professional Statement", data.get("professional_statement", ""))
    section("Core Competencies", data.get("core_competencies", ""))
    section("Technical Skills", data.get("technical_skills", ""))

    if data.get("experiences"):
        lines.extend(["", "Professional Experience"])
        for exp in data["experiences"]:
            lines.extend(["", exp.get("title", "")])
            if exp.get("subtitle"):
                lines.append(exp["subtitle"])
            if exp.get("scope"):
                lines.append("Scope & Responsibilities")
                lines.extend(f"- {b}" for b in exp["scope"])
            if exp.get("achievements"):
                lines.append("Key Achievements")
                lines.extend(f"- {b}" for b in exp["achievements"])

    if data.get("earlier_roles"):
        lines.extend(["", "Earlier Roles"])
        lines.extend(f"- {r}" for r in data["earlier_roles"])
    if data.get("education"):
        lines.extend(["", "Education"])
        lines.extend(f"- {e}" for e in data["education"])

    section("Languages", data.get("languages", ""))
    section("Interests", data.get("interests", ""))

    return "\n".join(lines).strip()
