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
OPENAI_API_KEY=...          # Required — job search & validation
ANTHROPIC_API_KEY=...       # Required — cover letter generation
ADZUNA_APP_ID=...           # Optional — adds more job results
ADZUNA_API_KEY=...
```

---

## Job Search

```bash
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

**Data sources searched in parallel:**
- **Arbeitnow** — EU/DACH jobs, no key needed
- **RemoteOK** — global remote jobs, no key needed
- **ClimateTechList** — ~11k jobs from ~580 climate companies, no key needed
- **Adzuna** — broad coverage across 16+ countries (optional, needs key)

Each job is validated by GPT-4o-mini for title relevance and climate score (0–5).

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

Start with `--interactive` (with or without a search), then use these commands:

| Command | What it does |
|---------|--------------|
| `list jobs` | Show numbered list of jobs in jobs.csv |
| `cl 5` | Cover letter for job #5 |
| `cl 5 - mention my terraform work` | Cover letter with extra context |
| `cl https://boards.greenhouse.io/...` | Cover letter from a URL |
| `cl 5 https://example.com/job` | Job #5 augmented with full description from URL |
| `answer 5: Why do you want to work here?` | Answer application question for job #5 |
| `aq: Tell us about a time you led a team` | Answer question without specific job context |
| `answer https://example.com/job: Why this role?` | Answer question with URL context |

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
I founded CO₂ok to make climate-neutral shopping easy for...

============================================================
   (94 words)
============================================================
```

---

## Project Structure

```
├── cli.py              # Entry point: argparse CLI + interactive loop
├── agent.py            # OpenAI agent loop + all job search tools
├── cover_letter.py     # Cover letter & question generation (Anthropic/Claude)
├── jobs.csv            # Output: accumulated job results
├── resume.txt          # Milo's resume (context for cover letters)
├── Cover-letter styleguide.md  # Writing style guide (context for Claude)
├── pyproject.toml      # Dependencies
└── .env.example        # Environment variable template
```

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
