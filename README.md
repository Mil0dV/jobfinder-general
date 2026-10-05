# Climate & Green Tech Job Finder

AI-powered job search across multiple free APIs, with built-in cover letter generation using Claude. Bring your own resume and Anthropic API key; a setup wizard builds a personal search profile around them.

## Setup

**Requires:** Python 3.10+, [uv](https://docs.astral.sh/uv/). Optional: LibreOffice (for PDF conversion of generated documents; docx output works without it).

```bash
git clone <this repo> && cd jobfinder
uv sync
cp .env.example .env        # add your ANTHROPIC_API_KEY
uv run python cli.py --setup
```

The setup wizard imports your resume (**docx, pdf, or txt**), asks a few questions
(target roles, seniority, countries, remote preference), and has Claude generate your
personal search profile — `profile.json`: target roles, title-matching regexes, search
query seeds, and the location machinery for your countries. It can also build a
personal cover-letter style guide from 1–2 letters you've written (otherwise a neutral
built-in style is used). Everything it writes is gitignored personal data; edit
`profile.json` by hand or re-run `--setup` any time.

**.env keys:**
```
ANTHROPIC_API_KEY=...       # Required — agent, job validation, cover letters, resumes
ADZUNA_APP_ID=...           # Optional — adds more job results
ADZUNA_API_KEY=...
```

> **Note:** `sync.py` (cross-device Firestore sync), the `android/` app, `.firebaserc`,
> and `firestore.rules` are the author's personal setup. Sync is a silent no-op unless
> you configure `FIREBASE_*` variables in `.env` pointing at your own Firebase project.

### Interview invites

Jobs you mark with `/applied` can be tracked all the way to the reply. The Android
app scans Gmail over IMAP in the background, classifies what each company wrote
back, and notifies you about the two kinds that need you: an invitation to talk,
and an application that is still parked because a screening question, assessment
or document is outstanding. It errs towards flagging too much, since a missed
invitation is the whole point. Verdicts sync to Firestore, so
on the laptop the TUI menu shows `Replies (✉ N)` and the shell announces both at
startup and lists them with `/inbox`. Setup lives entirely on the phone
(`GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` in `android/.../Keys.kt`); no mail
credentials are needed here. See `android/README.md`.

---

## Job Search

```bash
# Full-screen TUI: search, swipe through results, then write applications
uv run python cli.py --tui

# Search all target roles from your profile (recommended)
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

### Welcome to the Jungle (optional)

WTTJ has no public API, so it needs your own login. It's off until you set it up:

```bash
uv sync --extra wttj && uv run playwright install chromium   # ~120 MB, only for this source
uv run python cli.py --login-wttj                             # sign in once (Google SSO works)
```

A browser window opens; sign in, browse the jobs page, then close it. The session is stored in
`.jobfinder_wttj_profile/` (gitignored) and later searches pull your WTTJ recommendations
automatically (read-only; nothing is swiped on your account). Re-run `--login-wttj` when the
session expires. Automated access may be restricted by WTTJ's terms, so use your own account at
your own discretion.

### Your LinkedIn network

Search the job boards of companies where you already know someone. Export your
connections from LinkedIn (**Settings → Data privacy → Get a copy of your data →
Connections**), then:

```bash
# One-off: import the export and score each company for climate relevance
uv run python cli.py --import-connections ~/Downloads/Connections.csv

# Search the climate-relevant ones (also in the TUI menu: "Search my LinkedIn network")
uv run python cli.py --connections -l "my locations"
```

Matching jobs are ranked like any other, plus a `contact` column naming the person
you know there — `/contact` in interactive mode shows them, and you decide whether
to mention them: `/cl 5 - mention Jane, who runs ops there`.

The first run is the slow one: each company's board is found by probing the common
ATS APIs, then web search, then scraping the careers page. Results are cached
(`connections_boards.json`), so later runs only re-check what expired.

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

Each job is scored by Claude (Haiku 4.5) for role fit (0–5, against your profile) and company climate relevance (0–5); only good matches are kept. Postings older than 30 days get a liveness check — definitively dead links (404/410, board-root soft-404s) are dropped, while crawl refusals (403/429/timeouts) get the benefit of the doubt.

---

## TUI

`uv run python cli.py --tui` opens a full-screen terminal UI (greeted by the job witch, who has already made up her mind about you):

1. **Search** — your full target-role profile or a specific role, with a location picker that remembers previous locations (or type a new one).
2. **Progress animation** — live per-source status, scoring and link-verification phases, and a growing-plant progress bar. 🌱→🌳
3. **Swipe deck** — one job card at a time: `→` like, `←` pass, `u` undo (5-second window), `d` expands the full role description, `c` expands company info, `o` opens the posting in your browser. Decisions persist in `job_decisions.json`, so each job only has to be judged once.
4. **Applications** — liked jobs are handed to the interactive shell; `/next` steps through them, making each the active job for `/cl`, `/resume`, `/apply`, `/salary`.

The menu also offers **Review undecided saved jobs** — swipe through everything in `jobs.csv` you haven't judged yet (dead postings are verified out first).

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
├── setup_wizard.py     # First-run setup: resume import + profile generation
├── agent.py            # Claude agent loop + all job search tools
├── connections.py      # LinkedIn network search: export → companies → their job boards
├── candidate_profile.py # Loads profile.json (falls back to built-in defaults)
├── cover_letter.py     # Cover letter & question generation (Anthropic/Claude)
├── resume_tailor.py    # Resume tailoring (Anthropic/Claude) → styled docx/PDF
├── resume_data.py      # Loads Resume_base.<docx|pdf|txt> → structured resume.json (cached)
├── styleguide_default.md # Neutral cover-letter style, used without a personal one
├── profile.json        # Your search profile, written by --setup (gitignored)
├── jobs.csv            # Output: accumulated job results (gitignored)
├── Resume_base.docx    # Your resume — the single source of truth (gitignored)
├── resume.json         # Structured resume, derived from your resume file (gitignored)
├── Cover-letter styleguide.md  # Your writing style guide (context for Claude; gitignored)
├── pyproject.toml      # Dependencies
└── .env.example        # Environment variable template
```

> **Note:** `profile.json`, `Resume_base.*`, `resume.json`, `Cover-letter styleguide.md`,
> generated documents in `applications/`, and `jobs.csv` are gitignored so your personal
> data stays local. `cli.py --setup` creates all of them; to do it by hand instead:
>
> - Drop your resume in as **`Resume_base.docx`** (or `.pdf` / `.txt`) — the single source
>   of truth. On first use it's parsed (by Claude) into `resume.json`, the structured form
>   used for tailoring and cover-letter context. Edit it and it's re-parsed automatically.
>   (No resume file? Copy `resume.json.example` → `resume.json` and edit it directly.)
> - `Cover-letter styleguide.md.example` → `Cover-letter styleguide.md` (paste a few of
>   your own cover letters so Claude matches your voice)
> - Write `profile.json` following the field names in `candidate_profile.py`

## CLI Reference

```
uv run python cli.py [OPTIONS] [JOB_TITLES]

Arguments:
  JOB_TITLES           Comma-separated job titles to search for (optional with --interactive or --cl)

Options:
  -l, --location       Location filter (city or country)
  -t, --job-type       full-time | part-time | contract | remote
  -e, --experience     entry | mid | senior
  --setup              First-time setup wizard (resume import + profile generation)
  --connections        Search companies where you know someone (LinkedIn export)
  --import-connections PATH   Import a LinkedIn Connections.csv export
  --interactive        Drop into interactive mode after search
  --cover-letter N     Generate cover letter for job #N and exit
  --cl N               Alias for --cover-letter
```
