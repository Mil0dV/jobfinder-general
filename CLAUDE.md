# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Climate & Green Tech Job Finder — an AI-powered agent that searches nine free job sources (Arbeitnow, RemoteOK, Adzuna, Greenhouse, Lever, ClimateTechList, Climatebase, Jobs in Carbon, Remote Impact) for climate/green-tech positions matching the candidate's target-role profile, validates role fit + climate relevance with Claude, and outputs ranked results to CSV. All LLM calls go through the Anthropic API (no OpenAI dependency).

## Commands

```bash
# Setup
uv sync                    # Install dependencies
uv sync --extra dev        # Install with dev tools (pytest, black, ruff)
uv run python cli.py --setup   # First-run wizard: import resume (docx/pdf/txt), generate profile.json

# Run
uv run python cli.py --tui                                        # Full-screen TUI: search → swipe → apply
uv run python cli.py --profile -l "Switzerland"                   # Search all target roles (recommended)
uv run python cli.py "Software Engineer"                          # Basic search
uv run python cli.py "Data Scientist" --location "Switzerland"    # With location
uv run python cli.py "Engineer,PM" -l "Germany" --interactive     # Multi-role, interactive

# Standalone actions (all have short flags)
uv run python cli.py -c 1                                         # Cover letter for job #1
uv run python cli.py -r 1                                         # Tailor resume for job #1
uv run python cli.py -a 1                                         # Both CL + resume for job #1
uv run python cli.py -a https://example.com/job                   # Both from URL
# Long forms: --cl/--cover-letter, --tailor/--resume, --apply

# Interactive mode — a slash-command shell (see "Interactive Shell" below).
# A leading '/' is optional, so every command below also works without it.
# Tab completes commands & job numbers; Up/Ctrl+R recall history (persisted to
# .jobfinder_history); /help lists everything; type /exit (or Ctrl+C twice) to quit.
# /cl 5                       — cover letter for job #5 from CSV
# /cl 5 - mention terraform   — with extra context
# /cl @overstory              — fuzzy-pick a saved job by title/company
# /cl https://example.com/job — from a URL
# /resume 5  ·  /apply 5  ·  /salary 5   — tailor resume / CL+resume / salary estimate
# /drunk 5                    — force the drunk-draft cover letter (pearls get it by default)
# /applied 5                  — mark a job as applied (stored + synced); next queued job, else exit
# /answer 5: Why this company? — answer application question for job #5
# /aq: Tell me about yourself — answer question without job context (one paragraph)
# /liked  ·  /next (/n)       — queue liked-but-not-applied jobs from earlier swipes; step through
# /set 5  ·  /paste           — set an active job (then omit the #); or paste a description
#                               (pasted/URL jobs not yet in jobs.csv are saved + synced)
# /retry  ·  /redo make it shorter — re-run the last generation (redo adds guidance)
# /pdf [docx_path]            — redo the docx → pdf conversion (default: latest docx)
# /open (/o) [docx_path]      — open the last generated docx (or a given file)
# /url (/u) [#|@name|url]     — open the job posting URL in the browser (default: active job)
# /status  ·  /jobs  ·  !ls applications  — utilities

# Lint & Format
black .                    # Format code
black --check .            # Check formatting
ruff check .               # Lint
ruff check . --fix         # Auto-fix lint issues
```

Style: 100-char line length, Python 3.10+ target (configured in pyproject.toml for both black and ruff).

## Architecture

The codebase is a single-module Python application with two entry points:

- **`cli.py`** — argparse CLI that parses job titles (comma-separated), location, job-type, experience level, `--profile`, `--interactive`, and `--cover-letter`/`--cl` flags. `--profile` runs `run_profile_search()` (deterministic, no agent loop); free-text titles go through the agent from `agent.py`. Cover letter commands come from `cover_letter.py`.
- **`agent.py`** — Contains everything else: the custom agentic loop, all source scrapers, LLM validation, and agent initialization.
- **`tui.py`** — Full-screen terminal UI (`cli.py --tui`), hand-rolled ANSI/termios (no extra deps). Splash screen (ASCII witch with notepad, skipped on terminals under 72×25) → menu → search (profile or role, location picker with history in `.jobfinder_tui.json`) → animated progress (driven by `search_jobs`'s `_progress` callback; library prints are swallowed via a devnull stdout swap while `Term` owns the screen) → tinder-style card deck (`→` like / `←` pass / `u` undo within 5s / `d` description / `c` company info / `o` open browser / `a` apply now; pearls sort first and carry a magenta 🦪 banner). Cards show as much of the role description as fits the screen (prefetched for the next few cards; fetched text is cached on disk in `job_descriptions.json` via `cover_letter.fetch_job_description`, shared with the shell's `/cl`/`/apply`). `a` suspends the TUI (`Term.suspend()`) and drops into the interactive shell with that job active (`_run_interactive(initial_job=...)`); exiting the shell returns to the same card — unless the shell decided it (`/applied`), then the deck moves on to the next one. Decisions persist per normalized URL in `job_decisions.json`; undecided saved jobs can be reviewed from the menu (liveness is checked during search, so the deck opens instantly), and the menu's "Apply to liked jobs" (`liked_from_csv`) re-queues liked-but-not-applied jobs from earlier sessions. Liked jobs are returned to `cli.py`, which enters the interactive shell with them queued (`/next` steps through).
- **`candidate_profile.py`** — Loader for the candidate's target-role profile. Reads `profile.json` (gitignored; written by `cli.py --setup`, overridable via `JOBFINDER_PROFILE` env var) with hardcoded `_DEFAULT_*` fallbacks so a config-less checkout behaves exactly as before. Exports: `TARGET_ROLES` (human-readable, fed to LLM validation), `ROLE_PATTERNS` (regex families for local title filtering; invalid config regexes are skipped with a warning), `EXCLUDE_PATTERNS` (interns, volunteers, recruiting spam), `PROFILE_QUERY_SEEDS` (short queries for sources with server-side search), `FIT_RUBRIC` (anchors for the validator's 0–5 fit scale), location machinery (`COUNTRY_TERMS`, `ADZUNA_COUNTRIES`/`ADZUNA_DEFAULT`, `CLIMATEBASE_COUNTRIES`, `TIMEZONE`, `SALARY_REGION_HINT`), pearl thresholds, and `DRUNK_MODE_ENABLED` (config default false; fallback true). Edit `profile.json` — or re-run `--setup` — to change what a "fit" job is; a config edit needs a restart (loaded at import).
- **`setup_wizard.py`** — First-run setup (`cli.py --setup`, auto-offered when neither `profile.json` nor a resume exists): checks/collects `ANTHROPIC_API_KEY`, imports a resume via `resume_data.import_resume()`, asks target roles/seniority/countries/remote/drunk-mode, then a JSON-schema-constrained Claude call generates the full `profile.json` (roles, regexes incl. local-language variants, seeds, fit rubric, location maps — Adzuna codes restricted to Adzuna's real country list; map fields are generated as entry arrays because constrained output forbids schema-valued `additionalProperties`, converted by `_entries_to_maps`). Generated regexes are compile-checked and matched against the user's stated roles; the user can accept / regenerate with feedback / quit. Optionally generates a personal `Cover-letter styleguide.md` from 1–2 sample letters.
- **`cover_letter.py`** — Cover letter and application question generation using Claude (Anthropic API). Two-pass generation: a silent draft, then a self-critique pass (`_REVISE_INSTRUCTION`) that rereads the draft for AI tells (uniform rhythm, symmetric constructions, banned phrases) and streams the rewrite; the system prompt (style guide + resume) is cache-marked so pass 2 reuses it, and a failed revision falls back to the draft. Pearl supermatches instead get a "drunk draft" (`drunk=True`): a standalone visibly-drunk system prompt (`_drunk_system_prompt` — self-interruptions, tangents, loosening grammar, with sober→drunk few-shot pairs and a banned list of performed-casualness tells like "Honestly..."), followed by a "morning-after" pass (`_DRUNK_TELLCHECK_INSTRUCTION`) that strips AI tells without sobering the letter up and annotates each rewritten line with two inline `[or: … / …]` alternatives to hand-pick from; the automatic pearl→drunk trigger is gated on `candidate_profile.DRUNK_MODE_ENABLED`, while the shell's `/drunk` forces that mode for any job. Without a personal `Cover-letter styleguide.md`, the tracked neutral `styleguide_default.md` is used. Supports job selection from CSV, URL-based job descriptions, and interactive commands (`cl`, `answer`/`aq`, `list jobs`).
- **`resume_tailor.py`** — Resume tailoring using Claude. Makes minimal, natural edits to match a role description. Builds a styled docx matching the original Resume.pdf layout, converts to PDF via LibreOffice. Interactive command: `resume <job#>` or `resume <url>`.
- **`resume_data.py`** — Resume loading: the first existing `Resume_base.<docx|pdf|txt>` (or `resume.txt`) is the source of truth, parsed by Claude into `resume.json` and regenerated on content-hash change; a hand-written `resume.json` works without any source file. `import_resume(path)` (used by the wizard) copies an arbitrary docx/pdf/txt into the project root as `Resume_base.<ext>` and parses it. PDF text extraction via `pypdf`.

### Interactive Shell (`cli.py`)

`_run_interactive()` is a stdlib-`readline` REPL. A module-level `COMMANDS`
registry (built by `_build_registry()`) is the single source of truth that
drives `_dispatch()`, Tab completion (`_completer`), `/help` (`_print_help_table`),
and difflib did-you-mean (`_suggest`) — so the command list never drifts out of
sync. Each `Command` maps a verb to a thin handler `(args, ctx)` over the existing
`generate_cover_letter` / `tailor_resume` / `estimate_salary` /
`answer_application_question` functions; `_Ctx` holds the active session job and
the last generation (for `/retry` and `/redo`). A leading `/` is stripped before
lookup, so legacy bare commands (`cl 5`, `set job 2`, `list jobs`, `aq:`) keep
working unchanged. `_resolve_target()` resolves a `#`, `@name` fuzzy match, URL,
or the session job into a job context. Persistent history lives in
`.jobfinder_history` (gitignored); `select()`-based paste detection (`_absorb_paste`)
captures a pasted multi-line job description as the active job; identified pastes
are saved to jobs.csv via `_register_pasted_job` (URL-less rows dedupe by
company + title in `save_jobs`), like shell URLs via `_register_url_job`, so
`/applied` can track them. `/liked` (and `/next` with no queue) re-queues
liked-but-not-applied jobs from `job_decisions.json`. `/applied` writes
an `"applied"` decision to `job_decisions.json` (the TUI swipe store, synced
across devices); entered from a TUI card (`ctx.from_tui`) it exits the shell
back to the deck (which then advances past it), with a liked-jobs queue running
it advances to the next queued job, and otherwise it exits the app. Ctrl+C cancels a
running generation back to the prompt; Ctrl+C at an empty prompt exits.

### Agent Loop (`ClaudeAgent` class)

Manual tool-use loop against the Anthropic Messages API (`claude-opus-4-8`, adaptive thinking). Runs up to 10 iterations: builds tool `input_schema`s from function signatures via `inspect`, executes every `tool_use` block in the response, and returns all `tool_result` blocks in a single user message (errors go back with `is_error: true`). The agent is initialized with three tools: `search_jobs`, `get_company_info`, `save_csv`.

### Tool Functions in `agent.py`

- **`search_jobs(query, location, ...)`** — Unified entry point. Empty query or `"profile"` searches the full target-role profile. Queries all nine sources in parallel, filters titles via `_title_matches()` (profile regexes or free-text query), dedupes by URL and (company, title), drops jobs older than `max_age_days` (default 90), verifies postings are still online, fetches full descriptions in parallel (`fetch_descriptions`, attached as runtime-only `_desc`, disk-cached in `job_descriptions.json`), batch-validates with LLM (which scores the full description, not just the feed snippet), keeps jobs with `fit_score >= 3` and `climate_score >= 2`, sorted pearls-first → fit → climate → startup → recency. Accepts an internal `_progress(phase, done, total, detail)` callback (used by the TUI; underscore params are excluded from agent tool schemas).
- **`verify_jobs_online(jobs)`** — Parallel liveness check for postings older than `VERIFY_AGE_DAYS` (30); runs inside `search_jobs` right after dedup, before description fetching and LLM validation. A posting counts as offline ONLY on a definitive 404/410 or an ATS soft-404 (200-redirect to the board root with `?error=true` / job-id path segment gone). Crawl refusals (401/403/405/429), bot walls, server errors, and timeouts get the benefit of the doubt and are kept.
- **`search_arbeitnow()`** — EU/DACH-focused, free API.
- **`search_remoteok()`** — Remote-only jobs from RemoteOK JSON feed.
- **`search_adzuna()`** — Multi-country API; requires `ADZUNA_APP_ID` and `ADZUNA_API_KEY` env vars. In profile mode fans out over `PROFILE_QUERY_SEEDS`.
- **`search_greenhouse()` / `search_lever()`** — Curated climate companies' ATS boards (slugs verified 2026-07; dead slugs skip silently — refresh the lists occasionally).
- **`search_climatetechlist()`** — Scrapes ClimateTechList's Airtable shared view (~10k jobs from ~580 climate companies). No API key needed.
- **`search_climatebase()`** — Climatebase.org (~14k climate jobs) via its public search-only Algolia index (credentials extracted from their frontend; if they rotate, re-extract from the `_app-*.js` bundle). Fans out over `PROFILE_QUERY_SEEDS`, filters by country facet + Remote.
- **`search_jobsincarbon()`** — Jobs in Carbon niceboard via its sitemap; fetches matching job pages for real titles.
- **`search_remoteimpact()`** — Remote Impact's climate & environment category (HTML parsing).
- **`validate_jobs_batch()`** — Sends jobs in batches of 15 (6 parallel workers) to `claude-haiku-4-5` with a JSON-schema-constrained response (`output_config.format`), falling back to source-based priors if the API is unavailable (climate-only boards get climate=4). Scores `fit` (0–5 role: tech focus, product/vision ownership, team leadership), `startup` (0–5 startup-ness: 5 = scrappy startup, 0 = bureaucratic enterprise), and `climate` (0–5 company mission) with a one-line `reason`. A **pearl** (supermatch) is all three ≥ 4 with sum ≥ 13 (`candidate_profile.is_pearl`); pearls sort first in both decks, get a 🦪 banner, and their cover letters come as "drunk drafts" (see cover_letter.py).
- **`get_company_info()`** — DuckDuckGo search for company sustainability info; cached in `company_cache.json`.
- **`save_jobs()` / `save_csv()`** — Appends to `jobs.csv`, deduplicates by normalized URL (URL-less rows, e.g. pasted descriptions, by company + title), handles dynamic columns. CSV columns include `fit_score`, `startup_score`, `climate_score`, `reason`.

### Data Flow

`--profile`: CLI → `run_profile_search()` → `search_jobs` (parallel sources → title filter → dedup → LLM validation → threshold + rank) → `save_jobs` → `jobs.csv` (deterministic, no agent loop).

Free-text search: CLI args → `ClaudeAgent.run()` → agent calls `search_jobs` → optionally `get_company_info` → `save_csv` → `jobs.csv`.

## Job Sources

Integrated (all free): Climatebase (public Algolia index — app `8PSNFFQTXQ`, index `Job_production`; if it stops working, re-extract credentials from their `_app-*.js` bundle), ClimateTechList (Airtable shared view), Greenhouse + Lever ATS boards (curated slugs, verified 2026-07), Jobs in Carbon (niceboard sitemap), Remote Impact (HTML), Arbeitnow, RemoteOK, Adzuna (needs API key).

Probed but not integrable (checked 2026-07): **remoteclimatejobs.co** is a beehiiv newsletter (jobs only in prose posts); **inclimate.com** serves an empty JS shell with no data API; **cdrjobs.earth** is a Webflow site with client-side-only job data; **welcometothejungle** and **idealist.org** are generalist boards largely covered by Adzuna/Climatebase; **climatehack.global / startup.ch / joinup.ch** have no machine-readable job feeds. Re-probe before assuming this is still true.

## Android App (`android/`)

Kotlin/Compose port of the TUI flow (see `android/README.md`): profile search over
the same nine sources → tinder-style swipe deck (most-recent-first) → streamed cover
letter / application answers / salary estimate. Also an `ACTION_SEND` share target:
share a job URL to the app to start applying. Terminal-look UI (monospace, dark,
underlined-word buttons) modeled on `tui.py`, including the splash art. Search
pipeline is a port of `agent.py` (`data/Sources.kt`, `data/SearchEngine.kt`); prompts
are verbatim from `cover_letter.py` (`data/Generator.kt`). Location policy is fixed
in `data/Profile.kt`: CH hybrid/remote, DE/NL remote-only. API keys are baked into
the gitignored `data/Keys.kt` (personal build; `Keys.kt.example` is the template);
`assets/resume.txt` + `assets/styleguide.md` are gitignored personal assets.
Build: `export JAVA_HOME=/var/lib/flatpak/app/com.google.AndroidStudio/current/active/files/extra/jbr && cd android && ./gradlew :app:assembleDebug`
(toolchain mirrors ~/.code/Eigenflow: Gradle 8.13, AGP 8.12, Kotlin 2.0, compileSdk 34).

## Cross-Device Sync (`sync.py`)

Optional Cloud Firestore sync (free tier) shared with the Android app. Backend:
Firebase project `dog-o-meter` (366421906137), Firestore `(default)` @ eur3, Email/
Password auth, rules locked to Milo's uid (`firestore.rules`; deploy with
`npx -y firebase-tools@latest deploy --only firestore`). Collections, with document
ids `sha1(normalized url | "company|title")`: `jobs` ↔ jobs.csv, `decisions` ↔
job_decisions.json (last `ts` wins; a swipe on one device hides the job everywhere),
`descriptions` ↔ job_descriptions.json (pushed on fetch, pulled on cache miss — so a
page one device can't scrape is served from the other's fetch;
`fetch_job_description` additionally falls back to the `r.jina.ai` rendering proxy
for bot-walled/JS-only pages). Python talks Firestore REST (no SDK); the Android app
uses the Firebase SDK (`data/Sync.kt`) against the same collections. Hooks:
`save_jobs` pushes new rows (`_push=False` inside sync itself),
`run_profile_search`/`run_tui` pull first, TUI swipes push in the background, and a
URL used in the interactive shell (`_register_url_job` in cli.py) becomes a saved job
with source `shared`. Configured via `FIREBASE_*` in `.env` (see `.env.example`);
unconfigured = every hook is a silent no-op. `uv run python sync.py` does a full
two-way sync and prints the auth uid for the rules. `FIREBASE_API_KEY=emulator` +
`FIRESTORE_EMULATOR_HOST` run it against a local Firestore emulator.

## Environment Variables

Defined in `.env` (see `.env.example`):
- `ANTHROPIC_API_KEY` (required — agent loop `claude-opus-4-8`, validation `claude-haiku-4-5`, cover letters, resume tailoring)
- `ADZUNA_APP_ID` / `ADZUNA_API_KEY` (optional, for Adzuna source)
- `FIREBASE_API_KEY` / `FIREBASE_PROJECT_ID` / `FIREBASE_EMAIL` / `FIREBASE_PASSWORD` (optional, cross-device sync)
