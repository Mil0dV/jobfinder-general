# Climate & Green Tech Job Finder

AI-powered job search across multiple free APIs, with built-in cover letter generation using Claude.

## Setup

**Requires:** Python 3.10+, [uv](https://docs.astral.sh/uv/)

```bash
uv sync
cp .env.example .env
# Edit .env and add your API keys
```

**.env keys:**
```
ANTHROPIC_API_KEY=...       # Required — agent, job validation, cover letters, resumes
ADZUNA_APP_ID=...           # Optional — adds more job results
ADZUNA_API_KEY=...
```

---

## Job Search

```bash
# Search all target roles from candidate_profile.py (recommended)
uv run python cli.py --profile -l "Switzerland"

# Search by role
uv run python cli.py "Software Engineer"

# With location
uv run python cli.py "Product Manager" --location "Switzerland"

# Multiple roles at once
uv run python cli.py "Engineer,PM,Data Scientist" -l "Germany"

# Then drop into interactive mode
uv run python cli.py "Software Engineer" -l "Switzerland" --interactive
```

Results are saved to `jobs.csv`. Each run appends new jobs (deduped by URL).

**Data sources searched in parallel (no key needed unless noted):**
- **Climatebase** — ~14k climate jobs, queried via its public Algolia search index
- **ClimateTechList** — ~10k jobs from ~580 climate companies, via its Airtable shared view
- **Greenhouse / Lever** — ATS boards of curated climate companies (Watershed, Overstory, Patch, Pachama, Voltus, ...)
- **Jobs in Carbon** — carbon-markets niceboard, via its sitemap
- **Remote Impact** — remote climate & environment jobs
- **Arbeitnow** — EU/DACH jobs
- **RemoteOK** — global remote jobs
- **Adzuna** — broad coverage across 16+ countries (optional, needs free API key)

**Sources not (yet) integrated, and why:**
- **remoteclimatejobs.co** — a beehiiv newsletter, not a structured job board; listings only exist inside prose posts
- **inclimate.com** — ships an empty JS shell (~3 KB HTML); jobs render client-side with no discoverable data API
- **cdrjobs.earth** — Webflow site whose job board loads client-side; no job data in the served HTML
- **welcometothejungle, idealist.org** — generalist boards behind their own search APIs; Adzuna + Climatebase already cover most of their climate listings
- **climatehack.global, startup.ch, joinup.ch** — community/newsletter sites without machine-readable job feeds

Each job is scored by Claude (Haiku 4.5) for role fit (0–5, against `candidate_profile.py`) and company climate relevance (0–5); only good matches are kept.

---

## Cover Letters

Generates cover letters and answers application questions using Claude Sonnet, drawing on your resume and style guide. Output streams token-by-token with a thinking indicator.

### Standalone (no search needed)

```bash
# Cover letter for job #2 from jobs.csv
uv run python cli.py --cl 2

# Interactive mode — skip the search, go straight to cover letters
uv run python cli.py --interactive
```

### Interactive mode commands

Start with `--interactive` (with or without a search). It's a slash-command shell —
**a leading `/` is optional**, so `cl 5` and `/cl 5` are identical. Press **Tab** to
complete commands and job numbers, **↑ / Ctrl+R** to recall history (persisted across
sessions in `.jobfinder_history`), and type `/help` for the full list. A typo gets a
"did you mean…?" hint.

| Command | What it does |
|---------|--------------|
| `/jobs` | Show numbered list of jobs in jobs.csv (also `list jobs`) |
| `/cl 5` | Cover letter for job #5 |
| `/cl 5 - mention my terraform work` | Cover letter with extra context |
| `/cl @climeworks` | Fuzzy-pick a saved job by title/company |
| `/cl https://boards.greenhouse.io/...` | Cover letter from a URL |
| `/resume 5` · `/apply 5` · `/salary 5` | Tailor resume · cover letter + resume · salary estimate |
| `/answer 5: Why do you want to work here?` | Answer application question for job #5 |
| `/aq: Tell us about a time you led a team` | Answer question without specific job context |
| `/set 5` · `/paste` | Set an active job (then omit the #) · or paste a description |
| `/retry` · `/redo make it shorter` | Re-run the last generation (`/redo` adds guidance) |
| `/open [docx\|pdf\|dir]` · `/status` | Open the latest generated file · show the active session |
| `!ls -t applications` | Run a shell command |

Ctrl+C cancels a running generation and returns you to the prompt; Ctrl+C at an empty
prompt (or `/exit`) quits.

### Example session

```
🌱 You: list jobs

Jobs in jobs.csv (23 total):
    1. Senior Product Manager @ Lakera [score: 4]
    2. Software Engineer @ Climeworks [score: 5]
    ...

🌱 You: cl 2

============================================================
   Cover Letter: Software Engineer @ Climeworks
============================================================

   thinking...
I came across the Software Engineer role at Climeworks and...
[streams token by token]

============================================================
   (187 words)
============================================================

🌱 You: cl 1 - emphasize my carbon footprinting tool work

[streams cover letter with extra context applied]

🌱 You: answer 2: Why do you want to work at Climeworks?

============================================================
   Application Question: Software Engineer @ Climeworks
   Q: Why do you want to work at Climeworks?
============================================================

   thinking...
I've spent the last few years building tools that cut carbon at scale...

============================================================
   (94 words)
============================================================
```

---

## Project Structure

```
├── cli.py              # Entry point: argparse CLI + interactive loop
├── agent.py            # Claude agent loop + all job search tools
├── cover_letter.py     # Cover letter & question generation (Anthropic/Claude)
├── resume_tailor.py    # Resume tailoring (Anthropic/Claude) → styled docx/PDF
├── resume_data.py      # Loads Resume_base.docx → structured resume.json (cached)
├── jobs.csv            # Output: accumulated job results (gitignored)
├── Resume_base.docx    # Your resume — the single source of truth (gitignored)
├── resume.json         # Structured resume, derived from the docx (gitignored)
├── Cover-letter styleguide.md  # Your writing style guide (context for Claude; gitignored)
├── pyproject.toml      # Dependencies
└── .env.example        # Environment variable template
```

> **Note:** `Resume_base.docx`, `resume.json`, `Cover-letter styleguide.md`, generated
> documents in `applications/`, and `jobs.csv` are gitignored so your personal data stays
> local. To get started:
>
> - Drop your resume in as **`Resume_base.docx`** — this is the single source of truth.
>   On first use it's parsed (by Claude) into `resume.json`, the structured form used for
>   tailoring and cover-letter context. Edit the docx and it's re-parsed automatically.
>   (No docx? Copy `resume.json.example` → `resume.json` and edit it directly instead.)
> - `Cover-letter styleguide.md.example` → `Cover-letter styleguide.md` (paste a few of
>   your own cover letters so Claude matches your voice)

## CLI Reference

```
uv run python cli.py [OPTIONS] [JOB_TITLES]

Arguments:
  JOB_TITLES           Comma-separated job titles to search for (optional with --interactive or --cl)

Options:
  -l, --location       Location filter (city or country)
  -t, --job-type       full-time | part-time | contract | remote
  -e, --experience     entry | mid | senior
  --interactive        Drop into interactive mode after search
  --cover-letter N     Generate cover letter for job #N and exit
  --cl N               Alias for --cover-letter
```
