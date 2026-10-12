"""TypeSafe Jev — fast typed judgments on job postings (climate score, region lock).

Jev is a "System One" model: one POST carries a state plus named questions
(Score / Noul / Choice) and returns calibrated probabilities, no text generation.
Used by `agent.search_jobs`, all in one request per job (questions run in parallel):

- **fit** (0–5): role match against the candidate profile and `FIT_RUBRIC`.
- **startup** (0–5): how scrappy vs. bureaucratic the company reads.
- **climate** (0–5): Score question with described levels — judged on what the
  company actually does, not on the posting's green vocabulary.
- **region lock**: Noul — "only people in the US/UK/… may apply", which the
  location field misses when a posting says "Remote" and buries the restriction
  in the body.

Unconfigured (no `TYPESAFE_API_KEY`) or failing = every function returns None and
the caller falls back to the existing LLM validation. Jev only produces numbers; the
LLM still writes the one-line `reason`, but only for jobs that survive the thresholds. Docs: https://docs.typesafe.ai
"""

import os

import requests
from dotenv import load_dotenv

from candidate_profile import CANDIDATE_PROFILE, FIT_RUBRIC

load_dotenv()

API_URL = os.getenv("TYPESAFE_API_URL", "https://api.typesafe.ai/v1/systemone")
MODEL = os.getenv("TYPESAFE_MODEL", "jev-latest")
_STATE_CHARS = 12000  # ~3k tokens; the model takes 32k for state + longest question

# Drop a job as region-locked above this noul (0.5 = unsure → keep, per the
# "benefit of the doubt" policy used by verify_jobs_online).
REGION_LOCK_THRESHOLD = 0.8

CLIMATE_LEVELS = [
    "No climate connection: the company's product or service has nothing to do with "
    "emissions, energy, or the environment",
    "Fossil fuel or heavy-polluting business (oil, gas, coal, mining, airlines), or "
    "climate only in marketing and ESG boilerplate",
    "Generic company or consultancy with a minor sustainability side-line, "
    "sustainability reporting for clients, or a green mention in one project",
    "Meaningful sustainability angle: a real business unit, product feature or "
    "customer segment is about sustainability, but it is not the whole company",
    "Climate-focused company: most of the revenue or work serves decarbonisation, "
    "energy efficiency, clean mobility, circular economy or adaptation",
    "Core climate mission: the company exists to cut or remove emissions (carbon "
    "removal, renewables, grid and storage, climate software, green hydrogen)",
]


FIT_LEVELS = [
    f"Wrong role family: {FIT_RUBRIC['zero']}",
    "Unrelated or junior role: barely touches the candidate's target roles, no ownership",
    "Related field but the wrong kind of role: a pure individual-contributor or support "
    "function with no product, technical-leadership or team ownership",
    f"Adjacent role: {FIT_RUBRIC['three']}",
    "Close match: a target-style role where seniority or ownership is a notch off, or "
    "the scope is narrower than the candidate wants",
    f"Exact target role: {FIT_RUBRIC['five']}",
]

STARTUP_LEVELS = [
    "Government, big corporate or consultancy: layers, committees, enterprise process, "
    "compliance-speak, 'alignment with stakeholders'",
    "Large established company: clear hierarchy and heavy process, slow decisions",
    "Mid-size company or institute: structured, some autonomy, process-driven",
    "Grown-up scale-up: some process and structure, but still moves quickly",
    "Startup: small team, informal, ownership expected, decisions made fast",
    "Scrappy early-stage startup: founders close, tiny team, ship-first, everyone wears "
    "several hats",
]


def api_key() -> str:
    return os.getenv("TYPESAFE_API_KEY", "")


def enabled() -> bool:
    return bool(api_key())


def _state(job: dict) -> dict:
    text = job.get("_desc") or job.get("snippet") or ""
    return {
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "location": job.get("location", ""),
        "remote_policy": job.get("wfh_policy", ""),
        "source": job.get("source", ""),
        "description": text[:_STATE_CHARS],
    }


def _questions(region: str) -> dict:
    return {
        "fit": {
            "type": "score",
            "instructions": "How well does this ROLE match the candidate described here? "
            f"Judge the role itself ({FIT_RUBRIC['criteria']}), not the company.\n\n"
            f"CANDIDATE:\n{CANDIDATE_PROFILE}",
            "criteria": FIT_LEVELS,
        },
        "startup": {
            "type": "score",
            "instructions": "How startup-like (non-bureaucratic) does this COMPANY feel, judged "
            "from size, stage, tone and hiring-process signals in the posting?",
            "criteria": STARTUP_LEVELS,
        },
        "climate": {
            "type": "score",
            "instructions": "How central is climate action to what this COMPANY does "
            "(judge the company's business, not the wording of this posting)?",
            "criteria": CLIMATE_LEVELS,
        },
        "region_locked": {
            "type": "noul",
            "instructions": "Does the posting restrict applicants to a place that excludes "
            f"someone living in {region}: e.g. 'remote, US only', 'must be authorized to "
            "work in the US', a US/UK/Canada time-zone requirement, or a US-state-based "
            "remote role?",
            "criteria": {
                "true": f"Applicants must live in or be authorized to work in a region "
                f"that does not include {region}",
                "false": f"Open to candidates in {region}: on-site or hybrid there, "
                "remote in Europe, EMEA or worldwide, or no restriction stated",
            },
        },
    }


def evaluate(job: dict, region: str = "Switzerland or the Netherlands", retries: int = 2):
    """Return {"fit", "startup", "climate": floats 0–5, "climate_confidence": float,
    "region_locked": float 0–1} for one job, or None when Jev is unconfigured or unreachable."""
    if not enabled():
        return None
    body = {"model": MODEL, "state": _state(job), "questions": _questions(region)}
    headers = {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"}
    import time

    for attempt in range(retries + 1):
        try:
            resp = requests.post(API_URL, headers=headers, json=body, timeout=30)
            if resp.status_code in (429, 529) and attempt < retries:
                time.sleep(float(resp.headers.get("Retry-After") or 2 ** (attempt + 1)))
                continue
            if resp.status_code == 401:
                print("   Jev: invalid TYPESAFE_API_KEY — falling back to the LLM validator")
                return None
            resp.raise_for_status()
            answers = resp.json()["answers"]
            return {
                "fit": float(answers["fit"]["score"]),
                "startup": float(answers["startup"]["score"]),
                "climate": float(answers["climate"]["score"]),
                "climate_confidence": float(answers["climate"].get("confidence", 0)),
                "region_locked": float(answers["region_locked"]["noul"]),
            }
        except Exception as e:
            if attempt == retries:
                print(f"   Jev error ({job.get('company', '?')}): {e}")
                return None
            time.sleep(2**attempt)
    return None


def annotate(jobs: list, region: str = "Switzerland or the Netherlands", _progress=None) -> int:
    """Attach runtime-only `_jev` to each job in parallel; returns how many got a verdict."""
    if not enabled() or not jobs:
        return 0
    from concurrent.futures import ThreadPoolExecutor, as_completed

    got = 0
    with ThreadPoolExecutor(max_workers=16) as ex:
        futures = {ex.submit(evaluate, j, region): j for j in jobs}
        for done, future in enumerate(as_completed(futures), 1):
            job = futures[future]
            res = future.result()
            if res:
                job["_jev"] = res
                got += 1
            if _progress:
                _progress("jev", done, len(jobs), job.get("company", ""))
    return got
