"""Cover letter and application question generation.

Cover letters and application questions use Claude Sonnet 4.6 (Anthropic) in two passes:
a draft, then a self-critique pass that hunts AI tells and streams the rewrite.
Salary estimates use Claude Haiku (Anthropic), single pass.
Uses the candidate's resume and style guide as context. Supports job selection from CSV,
URL-based job descriptions, and interactive commands.
"""

import csv
import json
import os
import re
import subprocess
import sys
import threading
import time

import anthropic
import trafilatura
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from dotenv import load_dotenv

from resume_data import candidate_name, resume_as_text

load_dotenv()

# ---------------------------------------------------------------------------
# Clients (lazy init)
# ---------------------------------------------------------------------------

_anthropic_client = None


def _get_anthropic_client():
    """Get or create the shared Anthropic client (for salary estimates)."""
    global _anthropic_client
    if _anthropic_client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            print("Error: ANTHROPIC_API_KEY not set. Add it to your .env file.")
            sys.exit(1)
        _anthropic_client = anthropic.Anthropic(api_key=api_key)
    return _anthropic_client


# ---------------------------------------------------------------------------
# Context loading
# ---------------------------------------------------------------------------

_project_root = os.path.dirname(os.path.abspath(__file__))


def _load_text_file(filename: str) -> str:
    path = os.path.join(_project_root, filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        print(f"Warning: {filename} not found at {path}")
        return ""


def _load_style_guide() -> str:
    return _load_text_file("Cover-letter styleguide.md")


def _load_resume() -> str:
    """Plain-text resume context, rendered from the structured resume (resume_data)."""
    return resume_as_text()


def _candidate_name() -> str:
    return candidate_name()


# ---------------------------------------------------------------------------
# Job selection from CSV
# ---------------------------------------------------------------------------

CSV_PATH = os.path.join(_project_root, "jobs.csv")


def load_jobs_from_csv() -> list[dict]:
    """Read jobs.csv and return a list of dicts."""
    try:
        with open(CSV_PATH, "r", newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def get_job_by_number(n: int) -> dict | None:
    """Return the nth job (1-indexed) from jobs.csv, or None."""
    jobs = load_jobs_from_csv()
    if 1 <= n <= len(jobs):
        return jobs[n - 1]
    return None


def list_jobs_brief() -> str:
    """Return a numbered list of jobs showing title, company, climate_score."""
    jobs = load_jobs_from_csv()
    if not jobs:
        return "No jobs found in jobs.csv"

    lines = [f"\nJobs in jobs.csv ({len(jobs)} total):"]
    display = jobs[:30]
    for i, job in enumerate(display, 1):
        score = job.get("climate_score", "?")
        title = job.get("title", "Unknown")
        company = job.get("company", "Unknown")
        lines.append(f"  {i:3d}. {title} @ {company} [score: {score}]")
    if len(jobs) > 30:
        lines.append(f"  ... and {len(jobs) - 30} more")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# URL fetching
# ---------------------------------------------------------------------------

# Fetched descriptions are cached on disk so the TUI card previews, the deck's
# full-description view, and later /cl / /apply runs only hit the site once.
DESCRIPTIONS_CACHE_PATH = os.path.join(_project_root, "job_descriptions.json")

_desc_cache: dict | None = None
_desc_cache_lock = threading.Lock()


def _desc_cache_key(url: str) -> str:
    return (url or "").split("?")[0].strip().rstrip("/")


def _load_desc_cache() -> dict:
    global _desc_cache
    if _desc_cache is None:
        try:
            with open(DESCRIPTIONS_CACHE_PATH, "r", encoding="utf-8") as f:
                _desc_cache = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            _desc_cache = {}
    return _desc_cache


def _fetch_via_jina(url: str) -> str:
    """Fallback text extraction through r.jina.ai (renders JS-heavy pages)."""
    try:
        import requests

        resp = requests.get(
            f"https://r.jina.ai/{url}",
            headers={"X-Return-Format": "text"},
            timeout=25,
        )
        if resp.status_code == 200 and len(resp.text.strip()) > 200:
            return resp.text.strip()
    except Exception:
        pass
    return ""


# A failed fetch is cached too (empty description) and only retried this often,
# so unfetchable postings don't hit the network on every search / card view.
_FAILED_FETCH_RETRY_DAYS = 7


def _cache_entry_usable(entry: dict) -> bool:
    """True if a cache entry answers the question (success, or a recent failure)."""
    if entry.get("description"):
        return True
    try:
        fetched = time.mktime(time.strptime(entry.get("fetched", ""), "%Y-%m-%d %H:%M"))
    except (ValueError, OverflowError):
        return False  # legacy empty entry without a usable timestamp: retry
    return (time.time() - fetched) < _FAILED_FETCH_RETRY_DAYS * 86400


def fetch_job_description(url: str, use_cache: bool = True) -> str:
    """Extract clean text from a job posting URL — fetched once, then re-used.

    Resolution order: local on-disk cache → cross-device sync DB (another
    device may have fetched it already) → direct fetch (trafilatura) →
    r.jina.ai rendering proxy for pages that resist direct fetching.
    Every outcome is cached locally (failures retry after a week); successful
    fetches are pushed to the sync DB so other devices never re-fetch.
    """
    key = _desc_cache_key(url)
    if use_cache and key:
        with _desc_cache_lock:
            entry = _load_desc_cache().get(key)
        if entry and _cache_entry_usable(entry):
            return entry.get("description", "")

    text = ""
    try:
        from sync import get_remote_description

        text = get_remote_description(url) or ""
    except Exception:
        pass

    fetched_here = False
    if not text:
        try:
            downloaded = trafilatura.fetch_url(url)
            if downloaded:
                text = trafilatura.extract(downloaded) or ""
        except Exception:
            pass
        if len(text.strip()) < 200:  # empty or boilerplate-only: try the hard way
            text = _fetch_via_jina(url) or text
        fetched_here = bool(text)

    if key:
        with _desc_cache_lock:
            cache = _load_desc_cache()
            cache[key] = {
                "description": text,  # empty = negative cache entry
                "fetched": time.strftime("%Y-%m-%d %H:%M"),
            }
            with open(DESCRIPTIONS_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=2, ensure_ascii=False)
        if text and fetched_here:
            try:
                from sync import push_description_async

                push_description_async(url, text)
            except Exception:
                pass
    return text


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------


def _cover_letter_system_prompt() -> str:
    style_guide = _load_style_guide()
    resume = _load_resume()
    return f"""You are writing a cover letter as {_candidate_name()}. Write ONLY the cover letter text.
No preamble, no commentary, no "Here is your cover letter" — just the letter itself.

The example cover letters in the style guide are the authoritative voice to match.
Study them carefully before writing. That register — direct, casual-but-sharp, personal, real — is the target.

STYLE GUIDE AND EXAMPLES:
{style_guide}

RESUME:
{resume}

DO NOT sound like AI or LinkedIn. Never use:
- "I am excited/thrilled/passionate to apply..."
- "I am confident that I would be a great asset..."
- "My experience has equipped me with..."
- "I am eager to contribute to your team..."
- Any bullet points or list formatting in the letter body
- Em-dashes (—)
- Buzzwords: "leverage", "synergies", "impact-driven", "stakeholder", "fast-paced environment", "team player"
- The dramatic backwards setup: "[description of company/product] — that's exactly the kind of X I want to be doing/building."
- "not just X, but Y" / "not only... but also" constructions
- Rule-of-three lists ("clear, focused, and effective")
- Hedged openers: "I believe that...", "I feel that..."
- "resonates", "aligns with my values", "genuinely excites me"
- Ending every paragraph on a tidy punchline

SOUND LIKE A PERSON:
- Vary the rhythm. A long winding sentence, then a short one. Fragments are fine.
- AI writes every sentence at the same medium length with neat parallel structure. Don't.
- Include one or two touches of real character: a dry aside, a parenthetical remark, a concrete opinion about the company's product or problem.
- Starting a sentence with "And", "But" or "Which" is fine. So is a mild colloquialism.
- Small imperfections are human: a slightly informal construction, a thought that runs on a bit. But NEVER spelling errors or typos.
- Don't polish every transition; people jump between thoughts.
- It should read like it was typed in one sitting by someone sharp who means it, not assembled by committee.

RULES:
- 150-220 words. Short paragraphs.
- Only draw from the resume. Never fabricate experience.
- End simply: "I look forward to expanding on this in an interview."
"""


def _question_system_prompt() -> str:
    style_guide = _load_style_guide()
    resume = _load_resume()
    return f"""You are answering an application form question as {_candidate_name()}.
Write ONLY the answer. No preamble, no commentary.

The cover letter examples in the style guide define the voice — direct, casual-but-sharp, real.
Apply that same register to application answers: plain language, precise verbs, no performance.

STYLE GUIDE AND EXAMPLES:
{style_guide}

RESUME:
{resume}

DO NOT sound like AI or LinkedIn. Never use:
- "I am passionate about..."
- "I thrive in fast-paced environments..."
- "I am a strong communicator/collaborator..."
- "This opportunity excites me because..."
- Buzzwords: "leverage", "synergies", "impact-driven", "stakeholder", "team player"
- "not just X, but Y" / "not only... but also" constructions
- Rule-of-three lists ("clear, focused, and effective")
- Hedged openers: "I believe that...", "I feel that..."
- "resonates", "aligns with my values", "genuinely excites me"

SOUND LIKE A PERSON:
- Vary the rhythm. A long winding sentence, then a short one. Fragments are fine.
- AI writes every sentence at the same medium length with neat parallel structure. Don't.
- A dry aside, a parenthetical remark, or a concrete opinion is welcome where it fits.
- Starting a sentence with "And", "But" or "Which" is fine. So is a mild colloquialism.
- Small imperfections are human: a slightly informal construction, a thought that runs on a bit. But NEVER spelling errors or typos.
- It should read like someone sharp answering honestly, not performing for a form.

RULES:
- 50-200 words depending on the question. Shorter is usually better.
- Only draw from the resume. Never fabricate experience.
- Answer the actual question directly — don't warm up to it.
"""


# Appended to the cover-letter system prompt for "pearl" supermatches: a rough,
# riskier single-pass draft that Milo will rewrite by hand anyway — lower polish
# on purpose, because a slightly-off draft is easier to rework than a slick one.
_DRUNK_ADDENDUM = """

PEARL MODE — this company is a dream match, and this draft is deliberately a rough
one: Milo will rewrite it by hand. So loosen up and swing harder than usual:
- Take creative risks. An unexpected opening, a weird-but-apt metaphor, an
  opinionated hot take on their product or market. If a line makes you hesitate
  "is this too much?" — keep it.
- Be funnier and more personal than feels safe. Overshoot the voice rather than
  undershoot it.
- A half-finished thought or a missing transition is fine. Interesting beats
  polished. Do NOT sand the edges afterwards.
- The banned-phrases list still applies, and the facts must still come straight
  from the resume. Wild delivery, true content.
- 150-260 words."""


_REVISE_INSTRUCTION = """Now re-read your draft as a skeptical editor who can smell AI writing a mile off.
Hunt for: uniform sentence rhythm, symmetric constructions, tidy punchline endings,
rule-of-three lists, generic claims that could appear in anyone's application,
anything performed rather than said, and any banned pattern that slipped through.
Rewrite it, fixing what you find and keeping what already sounds human. Keep the
same facts and roughly the same length. Output ONLY the final text — no commentary,
no explanation of what you changed."""


# ---------------------------------------------------------------------------
# User prompt construction
# ---------------------------------------------------------------------------


def _build_job_context(job: dict | None, extra_description: str = "") -> str:
    """Build a job context string from a CSV job dict and/or fetched description."""
    if not job and not extra_description:
        return ""

    parts = []
    if job:
        parts.append(f"Title: {job.get('title', 'Unknown')}")
        parts.append(f"Company: {job.get('company', 'Unknown')}")
        if job.get("location"):
            parts.append(f"Location: {job['location']}")
        if job.get("salary") and job["salary"] != "Not listed":
            parts.append(f"Salary: {job['salary']}")
        if job.get("wfh_policy"):
            parts.append(f"WFH Policy: {job['wfh_policy']}")
        if job.get("url"):
            parts.append(f"URL: {job['url']}")
        if job.get("snippet"):
            parts.append(f"Description snippet: {job['snippet']}")

    if extra_description:
        parts.append(f"\nFull job description:\n{extra_description}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Streaming generation
# ---------------------------------------------------------------------------


def _stream_response(system_prompt: str, user_prompt: str, header: str, revise: bool = True) -> str:
    """Claude Sonnet 4.6 generation with word count footer. Returns full text.

    Default is two passes: pass 1 writes a draft; pass 2 re-reads it as a
    skeptical editor hunting AI tells and streams the rewrite. The system prompt
    (style guide + resume) is cache-marked so the second pass reuses it. If the
    revision pass fails, the draft is used as-is. With revise=False (pearl /
    "drunk draft" mode) the first pass streams directly, unpolished.
    """
    client = _get_anthropic_client()

    print("\n" + "=" * 60)
    print(f"   {header}")
    print("=" * 60 + "\n")

    system = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
    messages = [{"role": "user", "content": user_prompt}]
    draft_text = ""
    full_text = ""

    try:
        if not revise:
            with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=system,
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    full_text += text
                    print(text, end="", flush=True)
        else:
            print("   Drafting...", flush=True)
            draft = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=system,
                messages=messages,
            )
            draft_text = "".join(b.text for b in draft.content if b.type == "text")
            messages += [
                {"role": "assistant", "content": draft_text},
                {"role": "user", "content": _REVISE_INSTRUCTION},
            ]
            print("   Rereading for AI tells...\n", flush=True)

            with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=system,
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    full_text += text
                    print(text, end="", flush=True)

    except anthropic.AuthenticationError:
        print("\nError: Invalid ANTHROPIC_API_KEY. Check your .env file.")
        return ""
    except anthropic.APIConnectionError:
        print("\nError: Could not connect to Anthropic API.")
        if draft_text and not full_text:
            print("   Revision failed; using the draft as-is.\n")
            print(draft_text)
            full_text = draft_text
        else:
            return ""
    except anthropic.APIError as e:
        print(f"\nAPI error: {e}")
        if draft_text and not full_text:
            print("   Revision failed; using the draft as-is.\n")
            print(draft_text)
            full_text = draft_text
        else:
            return ""

    word_count = len(full_text.split())
    print("\n")
    print("=" * 60)
    print(f"   ({word_count} words)")
    print("=" * 60)
    return full_text


_OUTPUT_DIR = os.path.join(_project_root, "applications")


def _filename_safe(s: str) -> str:
    """Strip characters that are invalid in filenames."""
    return re.sub(r'[\\/*?:"<>|]', "", s).strip()


def extract_job_meta(text: str, job: dict | None = None) -> tuple[str, str]:
    """Return (company, role) for file naming.

    Uses the job dict directly when available; otherwise asks Haiku to extract
    from the description text (no more than one short call).
    """
    if job:
        company = job.get("company", "").strip()
        title = job.get("title", "").strip()
        if company and title:
            return company, title

    if not text:
        return "Unknown", "Role"

    client = _get_anthropic_client()
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=60,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Extract the hiring company name and job title from the text below. "
                        "Reply with exactly this format (no extra words): COMPANY | ROLE\n\n"
                        + text[:3000]
                    ),
                }
            ],
        )
        parts = response.content[0].text.strip().split("|")
        if len(parts) >= 2:
            return parts[0].strip(), parts[1].strip()
    except Exception:
        pass
    return "Unknown", "Role"


def make_output_stem(prefix: str, company: str, role: str) -> str:
    """Build a clean filename stem: e.g. 'CL_Ionity - Head of Product'."""
    return f"{prefix}_{_filename_safe(company)} - {_filename_safe(role)}"


def _save_cover_letter_pdf(text: str, filename_stem: str) -> str:
    """Save cover letter text as a PDF in the applications dir. Returns PDF path."""
    os.makedirs(_OUTPUT_DIR, exist_ok=True)

    # Build a simple docx
    doc = Document()
    for section in doc.sections:
        section.top_margin = Inches(1.0)
        section.bottom_margin = Inches(1.0)
        section.left_margin = Inches(1.1)
        section.right_margin = Inches(1.1)

    for para_text in text.strip().split("\n"):
        p = doc.add_paragraph()
        run = p.add_run(para_text)
        run.font.name = "Calibri"
        run.font.size = Pt(11)
        run.font.color.rgb = RGBColor(0, 0, 0)
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.line_spacing = Pt(14)

    docx_path = os.path.join(_OUTPUT_DIR, f"{filename_stem}.docx")
    doc.save(docx_path)
    print(f"   Saved:  {docx_path}")

    result = subprocess.run(
        ["libreoffice", "--headless", "--convert-to", "pdf", docx_path, "--outdir", _OUTPUT_DIR],
        capture_output=True,
        text=True,
        timeout=30,
    )

    if result.returncode != 0:
        print(f"   Warning: PDF conversion failed: {result.stderr}")
        return docx_path

    pdf_path = os.path.join(_OUTPUT_DIR, f"{filename_stem}.pdf")
    print(f"   Saved:  {pdf_path}")
    return pdf_path


def generate_cover_letter(
    job: dict | None = None,
    extra_context: str = "",
    url: str = "",
    description: str = "",
    drunk: bool = False,
):
    """Generate and stream a cover letter for a job.

    drunk=True (used for pearl supermatches) skips the self-critique polish
    pass and prompts for a rougher, riskier single draft — raw material to
    rewrite by hand, not a finished letter.
    """
    extra_description = description
    if url:
        print("   Fetching job description...")
        extra_description = fetch_job_description(url)
        if not extra_description:
            print("   Warning: Could not extract text from URL.")

    job_context = _build_job_context(job, extra_description)

    if not job_context:
        print("Error: No job context provided. Specify a job number or URL.")
        return

    user_prompt = f"Write a cover letter for this position:\n\n{job_context}"
    if extra_context:
        user_prompt += f"\n\nAdditional context: {extra_context}"

    # Resolve company/role for header and filename
    company, title = extract_job_meta(job_context, job)
    header = f"Cover Letter: {title} @ {company}"
    system_prompt = _cover_letter_system_prompt()
    if drunk:
        from candidate_profile import is_pearl

        header += "   🦪 pearl · drunk draft" if job and is_pearl(job) else "   🍸 drunk draft"
        system_prompt += _DRUNK_ADDENDUM
    text = _stream_response(system_prompt, user_prompt, header, revise=not drunk)

    if text:
        _save_cover_letter_pdf(text, make_output_stem("CL", company, title))


def estimate_salary(job: dict | None = None, url: str = "", description: str = ""):
    """Stream a salary estimate for a job using Claude Haiku."""
    extra_description = description
    if url:
        print("   Fetching job description...")
        extra_description = fetch_job_description(url)
        if not extra_description:
            print("   Warning: Could not extract text from URL.")

    job_context = _build_job_context(job, extra_description)

    if not job_context:
        print("Error: No job context provided. Specify a job number or URL.")
        return

    system_prompt = (
        "You are a compensation expert with deep knowledge of tech and climate-tech salaries "
        "across Europe and globally. Given a job posting, provide a concise salary estimate. "
        "Structure your answer as:\n"
        "- Estimated range (annual, gross) in the relevant local currency\n"
        "- Key factors influencing the estimate (location, seniority, company type)\n"
        "- A brief note on equity/bonus if typical for this type of role\n"
        "Be specific and data-driven. No fluff. If location is ambiguous, state your assumption."
    )
    user_prompt = f"Estimate the salary for this role:\n\n{job_context}"

    title = job.get("title", "Unknown") if job else "Job from URL"
    company = job.get("company", "Unknown") if job else (url.split("/")[2] if url else "Unknown")
    header = f"Salary Estimate: {title} @ {company}"

    client = _get_anthropic_client()

    print("\n" + "=" * 60)
    print(f"   {header}")
    print("=" * 60 + "\n")

    try:
        with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        ) as stream:
            for text in stream.text_stream:
                print(text, end="", flush=True)
    except anthropic.AuthenticationError:
        print("\nError: Invalid ANTHROPIC_API_KEY. Check your .env file.")
        return
    except anthropic.APIConnectionError:
        print("\nError: Could not connect to Anthropic API.")
        return
    except anthropic.APIError as e:
        print(f"\nAPI error: {e}")
        return

    print("\n" + "=" * 60)


def answer_application_question(question: str, job: dict | None = None, url: str = ""):
    """Generate and stream an answer to an application question."""
    extra_description = ""
    if url:
        print("   Fetching job description...")
        extra_description = fetch_job_description(url)
        if not extra_description:
            print("   Warning: Could not extract text from URL.")

    job_context = _build_job_context(job, extra_description)

    user_prompt = f"Answer this application question:\n\n{question}"
    if job_context:
        user_prompt += f"\n\nJob context:\n{job_context}"

    # Build header
    header = "Application Question"
    if job:
        title = job.get("title", "Unknown")
        company = job.get("company", "Unknown")
        header = f"Application Question: {title} @ {company}"
    header += f"\n   Q: {question}"

    _stream_response(_question_system_prompt(), user_prompt, header)


# ---------------------------------------------------------------------------
# Command parser
# ---------------------------------------------------------------------------

_URL_PATTERN = re.compile(r"https?://\S+")


def parse_command(user_input: str) -> dict | None:
    """Parse interactive mode input for cover letter / question / list commands.

    Returns a dict describing the action, or None if not a recognized command.
    """
    text = user_input.strip()
    text_lower = text.lower()

    # --- List jobs ---
    if text_lower in ("list jobs", "show jobs", "jobs"):
        return {"action": "list"}

    # --- Answer question: "answer ..." / "aq ..." ---
    answer_match = re.match(
        r"^(?:answer|aq)\s*[:]\s*(.+)|^(?:answer|aq)\s+(.+)", text, re.IGNORECASE | re.DOTALL
    )
    if answer_match:
        # Group 1 = "aq: question" form, Group 2 = "answer question" form
        if answer_match.group(1) is not None:
            # Matched "aq: question" — question is already extracted
            return {
                "action": "answer",
                "job_number": None,
                "url": None,
                "question": answer_match.group(1).strip(),
            }
        rest = answer_match.group(2).strip()
        job_number = None
        url = None
        question = rest

        # Check for leading job number: "answer 5: question"
        num_match = re.match(r"^(\d+)\s*[:]\s*(.+)", rest, re.DOTALL)
        if num_match:
            job_number = int(num_match.group(1))
            question = num_match.group(2).strip()
        else:
            # Check for leading URL: "answer https://...: question"
            url_match = re.match(r"^(https?://\S+)\s*[:]\s*(.+)", rest, re.DOTALL)
            if url_match:
                url = url_match.group(1)
                question = url_match.group(2).strip()
            else:
                # "answer: question" (no job context)
                colon_match = re.match(r"^[:]\s*(.+)", rest, re.DOTALL)
                if colon_match:
                    question = colon_match.group(1).strip()

        return {
            "action": "answer",
            "job_number": job_number,
            "url": url,
            "question": question,
        }

    # --- Salary estimate: "salary 5" / "sal 5" / "salary https://..." ---
    sal_match = re.match(r"^(?:salary|sal)\s+(.+)", text, re.IGNORECASE)
    if sal_match:
        rest = sal_match.group(1).strip()
        job_number = None
        url = None

        num_match = re.match(r"^(\d+)$", rest)
        if num_match:
            job_number = int(num_match.group(1))
        else:
            url_match = _URL_PATTERN.search(rest)
            if url_match:
                url = url_match.group(0)

        if job_number is not None or url is not None:
            return {"action": "salary", "job_number": job_number, "url": url}

    # --- Cover letter: "cl ..." / "cover letter ..." ---
    cl_match = re.match(r"^(?:cl|cover\s*letter)\s*(?:for\s+job\s+)?(.+)", text, re.IGNORECASE)
    if cl_match:
        rest = cl_match.group(1).strip()
        job_number = None
        url = None
        extra_context = ""

        # Try: "cl 5 https://... - extra" or "cl 5 - extra" or "cl 5"
        parts_match = re.match(r"^(\d+)\s*(.*)", rest)
        if parts_match:
            job_number = int(parts_match.group(1))
            remainder = parts_match.group(2).strip()

            # Check for URL after number
            url_in_remainder = _URL_PATTERN.search(remainder)
            if url_in_remainder:
                url = url_in_remainder.group(0)
                remainder = remainder.replace(url, "").strip()

            # Check for extra context after - or :
            ctx_match = re.match(r"^[-:]\s*(.*)", remainder)
            if ctx_match:
                extra_context = ctx_match.group(1).strip()
        else:
            # Try: "cl https://..."
            url_match = _URL_PATTERN.search(rest)
            if url_match:
                url = url_match.group(0)
                remainder = rest.replace(url, "").strip()
                ctx_match = re.match(r"^[-:]\s*(.*)", remainder)
                if ctx_match:
                    extra_context = ctx_match.group(1).strip()

        if job_number is None and url is None:
            return None  # Not a valid cover letter command

        return {
            "action": "generate",
            "job_number": job_number,
            "url": url,
            "extra_context": extra_context,
        }

    return None
