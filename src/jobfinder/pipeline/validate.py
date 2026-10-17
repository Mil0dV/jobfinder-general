"""LLM validation: fit / startup / climate scores for candidate jobs."""

import json

from jobfinder.config.profile import CANDIDATE_PROFILE, FIT_RUBRIC
from jobfinder.sources.base import PROFILE_QUERY

VALIDATION_MODEL = "claude-haiku-4-5"  # fast + cheap; scoring is a simple batch task

_anthropic_client = None

# Sources that only list climate jobs — climate relevance is inherent.
CLIMATE_BOARD_SOURCES = {
    "climatebase",
    "climatetechlist",
    "greenhouse",
    "lever",
    "jobsincarbon",
    "remoteimpact",
    "climatejobs",
    "cdrjobs",
}  # (wttj is a general tech board — deliberately not here)

# Structured-output schema for one validation batch.
_VALIDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "evaluations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "fit": {"type": "integer"},
                    "startup": {"type": "integer"},
                    "climate": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "fit", "startup", "climate", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["evaluations"],
    "additionalProperties": False,
}


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        import jobfinder.llm.client as llm

        _anthropic_client = llm.get_client()
    return _anthropic_client


def _salvage_evaluations_json(text: str) -> dict | None:
    """Recover the complete evaluations from a truncated validation JSON reply.

    A batch cut off mid-write fails json.loads outright; the old fallback then
    scored the *whole* batch with priors (fit=3 / climate=4 on climate boards —
    jobs that pass the gates unvalidated). This trims to the last complete
    evaluation object and closes the structure, so only the genuinely missing
    tail falls back.
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    last_close = -1  # position just past the last complete evaluation object
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])  # complete, not truncated
                except json.JSONDecodeError:
                    return None
            if depth == 1:  # closed one evaluation inside evaluations[]
                # (outer object = depth 1 while open, each evaluation = depth 2)
                last_close = i + 1
    if last_close <= start:
        return None
    # Truncated (possibly mid-string): keep everything up to the last complete
    # evaluation and close evaluations[] + the outer object.
    try:
        data = json.loads(text[start:last_close] + "]}")
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and isinstance(data.get("evaluations"), list):
        return data
    return None


def _validation_json(prompt: str) -> dict | None:
    """Score a validation batch via the LLM with a JSON-schema-constrained response."""
    import jobfinder.llm.client as llm

    if not llm.api_key():
        return None
    try:
        response = _get_anthropic_client().messages.create(
            model=VALIDATION_MODEL,
            max_tokens=4000,  # 15 evaluations + reasons ≈ 1.5-2.5k; 2000 truncated whole batches
            output_config={"format": {"type": "json_schema", "schema": _VALIDATION_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        text = next(b.text for b in response.content if b.type == "text")
    except Exception as e:
        print(f"   Validation error ({VALIDATION_MODEL}): {e}")
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = _salvage_evaluations_json(text)
        if data is None:
            print(f"   Validation reply unparseable ({VALIDATION_MODEL}) — batch left unvalidated")
            return None
        print(
            f"   Validation reply truncated — salvaged {len(data.get('evaluations', []))} "
            f"complete evaluation(s), only the missing tail falls back"
        )
    return {
        str(ev["id"]): ev
        for ev in data.get("evaluations", [])
        if isinstance(ev, dict) and "id" in ev
    }


def validate_jobs_batch(jobs: list, search_query: str) -> list:
    """Score a batch of jobs (max ~15) for role fit and climate relevance via LLM."""
    if not jobs:
        return []

    role_target = (
        "the candidate profile below"
        if search_query == PROFILE_QUERY
        else f'the search query "{search_query}" and the candidate profile below'
    )
    jobs_text = "".join(
        f"ID {i}: {j.get('title')} at {j.get('company')} ({j.get('location', '?')}) "
        f"- {(j.get('_desc') or j.get('snippet') or '')[:1000]}\n"
        for i, j in enumerate(jobs)
    )
    prompt = f"""Evaluate these job postings for a specific candidate.

CANDIDATE PROFILE:
{CANDIDATE_PROFILE}

JOBS:
{jobs_text}

Respond with JSON: an "evaluations" array with one object per job ID, each with:
- id: the job ID from the list above.
- fit: 0-5. How well the ROLE matches {role_target}. Score the role itself:
  {FIT_RUBRIC["criteria"]}.
  5 = {FIT_RUBRIC["five"]}
  3 = {FIT_RUBRIC["three"]}
  0-1 = {FIT_RUBRIC["zero"]}
- startup: 0-5. How startup-like (non-bureaucratic) the COMPANY feels, judged from the
  posting's tone, company size/stage, and process signals.
  5 = scrappy startup/scale-up: small team, founders close, fast decisions, ship-first
  3 = grown-up scale-up: some process and structure, still moves quickly
  0-1 = big corporate / government / consultancy: layers, committees, enterprise
        process, compliance-speak, "alignment with stakeholders"
- climate: 0-5. Climate relevance of the COMPANY/mission.
  5 = core climate mission (carbon removal, renewables, grid, climate SaaS)
  3 = meaningful sustainability angle
  0-1 = no climate connection, or fossil/heavy-polluter
- reason: one short sentence.

Example:
{{"evaluations": [{{"id": 0, "fit": 5, "startup": 4, "climate": 4, "reason": "Head of Product at a carbon accounting startup"}}]}}"""

    results_map = _validation_json(prompt)
    if results_map is None:
        # No LLM available: titles already passed the profile regex, so assume
        # moderate fit; trust climate-only boards for climate relevance.
        return [
            {
                "fit_score": 3,
                "startup_score": 3,
                "climate_score": 4 if j.get("source") in CLIMATE_BOARD_SOURCES else 0,
                "reason": "unvalidated (no LLM available)",
            }
            for j in jobs
        ]

    validations = []
    for i, job in enumerate(jobs):
        res = results_map.get(str(i), {})
        try:
            fit = int(res.get("fit", 3))
            startup = int(res.get("startup", 3))
            climate = int(res.get("climate", 0))
        except (TypeError, ValueError):
            fit, startup, climate = 3, 3, 0
        if climate == 0 and job.get("source") in CLIMATE_BOARD_SOURCES:
            climate = 3  # climate board listing outranks a missing LLM judgment
        validations.append(
            {
                "fit_score": fit,
                "startup_score": startup,
                "climate_score": climate,
                "reason": res.get("reason", ""),
            }
        )
    return validations
