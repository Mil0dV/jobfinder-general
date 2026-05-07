# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Climate & Green Tech Job Finder — an AI-powered agent that searches multiple free job APIs (Arbeitnow, RemoteOK, Greenhouse, Adzuna, ClimateTechList) for climate/green-tech positions, validates relevance with GPT, and outputs results to CSV.

## Commands

```bash
# Setup
uv sync                    # Install dependencies
uv sync --extra dev        # Install with dev tools (pytest, black, ruff)

# Run
uv run python cli.py "Software Engineer"                          # Basic search
uv run python cli.py "Data Scientist" --location "Switzerland"    # With location
uv run python cli.py "Engineer,PM" -l "Germany" --interactive     # Multi-role, interactive

# Standalone actions (all have short flags)
uv run python cli.py -c 1                                         # Cover letter for job #1
uv run python cli.py -r 1                                         # Tailor resume for job #1
uv run python cli.py -a 1                                         # Both CL + resume for job #1
uv run python cli.py -a https://example.com/job                   # Both from URL
# Long forms: --cl/--cover-letter, --tailor/--resume, --apply

# Interactive mode commands
# cl 5                        — cover letter for job #5 from CSV
# cl 5 - mention terraform    — with extra context
# cl https://example.com/job  — from a URL
# resume 5                    — tailor resume for job #5
# resume https://example.com  — tailor resume from URL
# apply 5                     — both cover letter + tailored resume
# apply https://example.com   — both from URL
# answer 5: Why this company? — answer application question for job #5
# aq: Tell me about yourself  — answer question without job context
# list jobs                   — show numbered job list

# Lint & Format
black .                    # Format code
black --check .            # Check formatting
ruff check .               # Lint
ruff check . --fix         # Auto-fix lint issues
```

Style: 100-char line length, Python 3.10+ target (configured in pyproject.toml for both black and ruff).

## Architecture

The codebase is a single-module Python application with two entry points:

- **`cli.py`** — argparse CLI that parses job titles (comma-separated), location, job-type, experience level, `--interactive` flag, and `--cover-letter`/`--cl` flag. Invokes the agent from `agent.py` and cover letter commands from `cover_letter.py`.
- **`agent.py`** — Contains everything else: the custom agentic loop, all tool functions, and agent initialization.
- **`cover_letter.py`** — Cover letter and application question generation using Claude (Anthropic API). Streams output with thinking indicator. Supports job selection from CSV, URL-based job descriptions, and interactive commands (`cl`, `answer`/`aq`, `list jobs`).
- **`resume_tailor.py`** — Resume tailoring using Claude. Makes minimal, natural edits to match a role description. Builds a styled docx matching the original Resume.pdf layout, converts to PDF via LibreOffice. Interactive command: `resume <job#>` or `resume <url>`.

### Agent Loop (`OpenAIAgent` class)

Custom agentic loop using OpenAI function calling. Runs up to 10 iterations of tool calls against `gpt-4o-2024-08-06`. The agent is initialized with three tools: `search_jobs`, `get_company_info`, `save_csv`.

### Tool Functions in `agent.py`

- **`search_jobs()`** — Unified entry point. Queries all five APIs in parallel, deduplicates by URL, then batch-validates with LLM. Returns up to 50 jobs.
- **`search_arbeitnow()`** — EU/DACH-focused, free API, includes EN↔DE fuzzy title matching.
- **`search_remoteok()`** — Remote-only jobs from RemoteOK JSON feed.
- **`search_greenhouse()`** — Scrapes Greenhouse boards of curated climate companies (Climeworks, Watershed, Pachama, etc.) with 10 concurrent workers.
- **`search_adzuna()`** — Multi-country API; requires `ADZUNA_APP_ID` and `ADZUNA_API_KEY` env vars.
- **`search_climatetechlist()`** — Scrapes ClimateTechList's Airtable shared view (~10,600 jobs from ~580 climate companies). No API key needed.
- **`validate_jobs_batch()`** — Sends jobs in batches of 15 to `gpt-4o-mini` for relevance scoring and climate score (0–5).
- **`get_company_info()`** — DuckDuckGo search for company sustainability info; cached in `company_cache.json`.
- **`save_csv()`** — Appends to `jobs.csv`, deduplicates by normalized URL, handles dynamic columns.

### Data Flow

CLI args → `OpenAIAgent.run()` → agent calls `search_jobs` (parallel API queries → dedup → LLM validation) → optionally `get_company_info` → `save_csv` → `jobs.csv`

## Environment Variables

Defined in `.env` (see `.env.example`):
- `OPENAI_API_KEY` (required)
- `ADZUNA_APP_ID` / `ADZUNA_API_KEY` (optional, for Adzuna source)
- `ANTHROPIC_API_KEY` (required for cover letter generation)
