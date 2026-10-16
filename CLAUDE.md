# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Climate & Green Tech Job Finder — an AI-powered agent that searches twelve job sources (Arbeitnow, RemoteOK, Adzuna, Greenhouse, Lever, ClimateTechList, Climatebase, Jobs in Carbon, Remote Impact, climate.jobs, CDRjobs, plus optional Welcome to the Jungle) for climate/green-tech positions matching the candidate's target-role profile, validates role fit + climate relevance with Claude, and outputs ranked results to CSV. All LLM calls go through the Anthropic API (no OpenAI dependency).

## Commands

```bash
# Setup
uv sync                    # Install dependencies
uv sync --extra dev        # Install with dev tools (pytest, black, ruff)
uv run python cli.py --setup   # First-run wizard: import resume (docx/pdf/txt), generate profile.json

# Run
uv run python cli.py --tui                                        # Full-screen TUI: search → swipe → apply
uv run python cli.py --profile -l "Switzerland"                   # Search all target roles (recommended)
uv run python cli.py --import-connections ~/Connections.csv       # One-off: import a LinkedIn export
uv run python cli.py --connections -l "my locations"              # Search companies where you know someone
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
# /sober 5                    — force the regular cover letter, even for a pearl
# /applied 5                  — mark a job as applied (stored + synced); next queued job, else exit
# /answer 5: Why this company? — answer application question for job #5
# /aq: Tell me about yourself — answer question without job context (one paragraph)
# /liked  ·  /next (/n)       — queue liked-but-not-applied jobs from earlier swipes; step through
# /inbox [all] (/replies)     — what companies wrote back (invites first); see "Inbox Replies"
# /set 5  ·  /paste           — set an active job (then omit the #); or paste a description
#                               (pasted/URL jobs not yet in jobs.csv are saved + synced)
# /retry  ·  /redo make it shorter — re-run the last generation (redo adds guidance)
# /pdf [docx_path]            — redo the docx → pdf conversion (default: latest docx)
# /open (/o) [docx_path]      — open the last generated docx (or a given file)
# /url (/u) [#|@name|url]     — open the job posting URL in the browser (default: active job)
# /contact 5 (/who)          — show the LinkedIn connection you have at that company
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
- **`tui.py`** — Full-screen terminal UI (`cli.py --tui`), hand-rolled ANSI/termios (no extra deps). Splash screen (ASCII witch with notepad, skipped on terminals under 72×25) → menu → search (profile or role, location picker with history in `.jobfinder_tui.json`) → animated progress (driven by `search_jobs`'s `_progress` callback; library prints are swallowed via a devnull stdout swap while `Term` owns the screen) → tinder-style card deck (`→` like / `←` pass / `u` undo within 5s / `d` description / `c` company info / `o` open browser / `a` apply now; pearls sort first and carry a magenta 🦪 banner; network jobs carry a cyan 🤝 "via <contact>" line). Cards show as much of the role description as fits the screen (prefetched for the next few cards; fetched text is cached on disk in `job_descriptions.json` via `cover_letter.fetch_job_description`, shared with the shell's `/cl`/`/apply`). `a` suspends the TUI (`Term.suspend()`) and drops into the interactive shell with that job active (`_run_interactive(initial_job=...)`); exiting the shell returns to the same card — unless the shell decided it (`/applied`), then the deck moves on to the next one. Decisions persist per normalized URL in `job_decisions.json`; undecided saved jobs can be reviewed from the menu (liveness is checked during search, so the deck opens instantly; climate-gate rejects — `climate_score` < 2, see `_is_general` — are excluded), the menu's "General roles (non-climate, saved)" deck shows exactly those rejects without searching (every normal `search_jobs` run returns them as `general_jobs`, and `run_profile_search` / the TUI search save them to `jobs.csv`; the menu is empty until a profile search has run), and the menu's "Apply to liked jobs" (`liked_from_csv`) re-queues liked-but-not-applied jobs from earlier sessions. Liked jobs are returned to `cli.py`, which enters the interactive shell with them queued (`/next` steps through).
- **`connections.py`** — LinkedIn network search (see "LinkedIn Network Search" below).
  Turns a `Connections.csv` export into a job source: parse → group by company →
  climate-score the companies (batched, cached) → find each company's job board →
  return matching openings tagged with the person you know there.
- **`candidate_profile.py`** — Loader for the candidate's target-role profile. Reads `profile.json` (gitignored; written by `cli.py --setup`, overridable via `JOBFINDER_PROFILE` env var) with hardcoded `_DEFAULT_*` fallbacks so a config-less checkout behaves exactly as before. Exports: `TARGET_ROLES` (human-readable, fed to LLM validation), `ROLE_PATTERNS` (regex families for local title filtering; invalid config regexes are skipped with a warning), `EXCLUDE_PATTERNS` (interns, volunteers, recruiting spam), `PROFILE_QUERY_SEEDS` (short queries for sources with server-side search), `FIT_RUBRIC` (anchors for the validator's 0–5 fit scale), location machinery (`COUNTRY_TERMS`, `ADZUNA_COUNTRIES`/`ADZUNA_DEFAULT`, `CLIMATEBASE_COUNTRIES`, `TIMEZONE`, `SALARY_REGION_HINT`) and the "my locations" policy (`HOME_COUNTRIES`, from `locations.home_countries`: per country a `mode` of `any` / `hybrid_or_remote` / `remote_only`, optional `hybrid_cities` that unlock hybrid in a remote-only country, optional `extra_terms`; `agent._preferred_location_verdict`, the Adzuna/Climatebase/climate.jobs country fan-outs and the Jev region string all derive from it — remote-only countries are in the net via Climatebase and the full feeds but not fanned out per country; built-in default = Switzerland any, Germany remote-only + Freiburg hybrid, Netherlands hybrid/remote), pearl thresholds, and `DRUNK_MODE_ENABLED` (config default false; fallback true). Edit `profile.json` — or re-run `--setup` — to change what a "fit" job is; a config edit needs a restart (loaded at import).
- **`setup_wizard.py`** — First-run setup (`cli.py --setup`, auto-offered when neither `profile.json` nor a resume exists): checks/collects `ANTHROPIC_API_KEY`, imports a resume via `resume_data.import_resume()`, asks target roles/seniority/countries/remote/drunk-mode, then a JSON-schema-constrained Claude call generates the full `profile.json` (roles, regexes incl. local-language variants, seeds, fit rubric, location maps and `home_countries` rules — Adzuna codes restricted to Adzuna's real country list; map fields are generated as entry arrays because constrained output forbids schema-valued `additionalProperties`, converted by `_entries_to_maps`). Generated regexes are compile-checked and matched against the user's stated roles; the user can accept / regenerate with feedback / quit. Optionally generates a personal `Cover-letter styleguide.md` from 1–2 sample letters.
- **`cover_letter.py`** — Cover letter and application question generation using Claude (Anthropic API). Two-pass generation: a draft (streamed dimmed), then a self-critique pass (`_REVISE_INSTRUCTION`) that rereads the draft for AI tells (uniform rhythm, symmetric constructions, banned phrases) and streams the rewrite; the system prompt (style guide + resume) is cache-marked so pass 2 reuses it, and a failed revision falls back to the draft. Pearl supermatches instead get a "drunk draft" (`drunk=True`): a standalone visibly-drunk system prompt (`_drunk_system_prompt` — self-interruptions, tangents, loosening grammar, with sober→drunk few-shot pairs and a banned list of performed-casualness tells like "Honestly..."), followed by a "morning-after" pass (`_DRUNK_TELLCHECK_INSTRUCTION`) that strips AI tells without sobering the letter up and annotates each rewritten line with two inline `[or: … / …]` alternatives to hand-pick from; the automatic pearl→drunk trigger is gated on `candidate_profile.DRUNK_MODE_ENABLED`, while the shell's `/drunk` forces that mode for any job and `/sober` forces the regular cover letter even for a pearl. Without a personal `Cover-letter styleguide.md`, the tracked neutral `styleguide_default.md` is used. Supports job selection from CSV, URL-based job descriptions, and interactive commands (`cl`, `answer`/`aq`, `list jobs`).
- **`resume_tailor.py`** — Resume tailoring using Claude. Makes minimal, natural edits to match a role description. Builds a styled docx matching the original Resume.pdf layout, converts to PDF via LibreOffice. Interactive command: `resume <job#>` or `resume <url>`.
- **`resume_data.py`** — Resume loading: the first existing `Resume_base.<docx|pdf|txt>` (or `resume.txt`) is the source of truth, parsed by Claude into `resume.json` and regenerated on content-hash change; a hand-written `resume.json` works without any source file. `import_resume(path)` (used by the wizard) copies an arbitrary docx/pdf/txt into the project root as `Resume_base.<ext>` and parses it. PDF text extraction via `pypdf`.

### LinkedIn Network Search (`connections.py`)

A separate search mode — `cli.py --connections`, or "Search my LinkedIn network" in the
TUI menu — over the companies where the candidate already knows someone. Four cached
stages: `load_connections()` parses the export (skipping LinkedIn's `Notes:` preamble);
`companies()` normalizes names (legal suffixes stripped) and groups contacts;
`classify_companies()` climate-scores every company in batches of 60 via a constrained-JSON
fast-tier call, cached in `connections_companies.json` — only companies scoring
≥ `MIN_CLIMATE` (3) are probed; `resolve_board()` finds the job board in three escalating
tiers, cached in `connections_boards.json` (30 d on a hit, 14 d on a miss):

1. **Slug probes** — whole-name slugs (`squashed`, `hyphen-ated` — never a first-word
   guess, which lands on unrelated companies' boards) against the Greenhouse, Lever,
   Ashby, Personio, Recruitee, Workable and SmartRecruiters APIs. An empty-but-well-formed
   response counts as a hit (real board, nothing open) — except SmartRecruiters, which
   answers 200 + `[]` for *any* slug (`_PERMISSIVE_PROVIDERS`).
2. **Web search** — `ddgs` for the careers page; an ATS URL in the results promotes back to
   tier 1. Aggregators (LinkedIn, Indeed, builtin, …) and hosts unrelated to the company
   name are rejected (`_plausible_careers_url`).
3. **Careers-page scrape** — anchors whose text matches the role profile; for JS-only pages,
   `cover_letter.fetch_job_description` + one fast-tier extraction call.

`search_connections(query, location)` has the same signature as every other source, so
`search_jobs(..., _sources=[search_connections])` reuses the whole pipeline unchanged
(dedup → liveness → descriptions → LLM validation → ranking). Jobs carry `contact`,
`contact_url` and `contacts_n`; `contact` is a CSV column, a cyan 🤝 badge on the TUI card,
and `/contact` in the shell. Cover-letter prompts are deliberately untouched — mentioning a
connection stays manual (`/cl 5 - mention Jane, who runs ops there`). `connections.csv` and
both caches are gitignored personal data.

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
are saved to jobs.csv via `_register_pasted_job` (deduped by the job's
cross-device identity in `save_jobs`), like shell URLs via `_register_url_job`, so
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

- **`search_jobs(query, location, ...)`** — Unified entry point. Empty query or `"profile"` searches the full target-role profile. Queries all sources in parallel, filters titles via `_title_matches()` (profile regexes or free-text query), dedupes by cross-device identity (`sync.norm_key`: company|title, URL fallback — the same key save_jobs/decisions/sync use), drops jobs older than `max_age_days` (default 90), verifies postings are still online, fetches full descriptions in parallel (`fetch_descriptions`, attached as runtime-only `_desc`, disk-cached in `job_descriptions.json`), batch-validates with LLM (which scores the full description, not just the feed snippet), keeps jobs with `fit_score >= 3` and `climate_score >= 2`, sorted pearls-first → fit → climate → startup → recency. Accepts an internal `_progress(phase, done, total, detail)` callback (used by the TUI; underscore params are excluded from agent tool schemas) and `_sources` to override the source list (the network search passes `[search_connections]`); a source declaring a `_progress` parameter gets the callback passed through.
- **`verify_jobs_online(jobs)`** — Parallel liveness check for postings older than `VERIFY_AGE_DAYS` (30); runs inside `search_jobs` right after dedup, before description fetching and LLM validation. A posting counts as offline ONLY on a definitive 404/410 or an ATS soft-404 (200-redirect to the board root with `?error=true` / job-id path segment gone). Crawl refusals (401/403/405/429), bot walls, server errors, and timeouts get the benefit of the doubt and are kept.
- **`search_arbeitnow()`** — EU/DACH-focused, free API. Follows `links.next` until a page crosses the 90-day window (the feed's `created_at` is a Unix epoch; page 1 alone is only ~1 day). Remote roles bypass the local location filter (the feed carries a `remote` boolean the city-only location string doesn't reflect); the feed rate-limits bursts, so pages are spaced and a 429 keeps partial results.
- **`search_remoteok()`** — Remote-only jobs via RemoteOK's HTML load-more feed (`?action=get_jobs&offset=N`), parsing each row's JSON-LD JobPosting + `data-epoch`, walking until 90 days. NOT the `/api` endpoint — that hard-caps at the newest ~100 (~4 days) and ignores `?offset`. `applicantLocationRequirements` becomes the location so the downstream US/UK-only policy can act.
- **`search_adzuna()`** — Multi-country API; requires `ADZUNA_APP_ID` and `ADZUNA_API_KEY` env vars. In profile mode fans out over `PROFILE_QUERY_SEEDS`. Pages each `(country, seed)` (`/search/{page}`) until a short page ends it or `_ADZUNA_MAX_PAGES` (mind the API quota); `max_days_old=90` matches the pipeline window.
- **`search_greenhouse()` / `search_lever()`** — Curated climate companies' ATS boards (slugs verified 2026-07; dead slugs skip silently — refresh the lists occasionally). Full board per slug (no cap); location filtered locally.
- **`search_climatetechlist()`** — Scrapes ClimateTechList's Airtable shared view (~10k jobs from ~580 climate companies). No API key needed. The single `readSharedViewData` response holds the full catalog (no pagination cap).
- **`search_climatebase()`** — Climatebase.org (~14k climate jobs) via its public search-only Algolia index (credentials extracted from their frontend; if they rotate, re-extract from the `_app-*.js` bundle). Seeds (`PROFILE_QUERY_SEEDS`) fan out in parallel, each bounded server-side to 90 days via the numeric `activation_date_i` and paged (relevance-ordered, so the date filter — not the top-50 page — governs); country facet + Remote.
- **`search_jobsincarbon()`** — Jobs in Carbon (`jobsincarbon.com` — migrated off niceboard) via its sitemap; title-filters slugs, then fetches each match's page for its JSON-LD JobPosting (title/company/location/date). Location filtered *after* the fetch, so no role is dropped by a fetch cap before it is location-checked.
- **`search_remoteimpact()`** — Remote Impact's climate & environment category (HTML). Pages `?page=N` (the old `page/N/` form 404'd) for `_REMOTEIMPACT_MAX_PAGES` (~2 weeks; cards are undated) and parses each card's real location instead of hardcoding "Remote".
- **`search_climatejobs()`** — climate.jobs (server-rendered HTML, ~24 cards/page, date-sorted pagination with a 90-day stop). Carries Remote Climate Jobs' premium listings when the logged-in `cj_session` cookie is set (`CLIMATEJOBS_SESSION` in `.env`; obtained via magic-link login — expires after ~30 days, re-login and re-copy when it lapses). Uses `/jobs/location/<country>` pages for server-side location filtering, `?q=` for free-text queries.
- **`validate_jobs_batch()`** — Sends jobs in batches of 15 (6 parallel workers) to `claude-haiku-4-5` with a JSON-schema-constrained response (`output_config.format`), falling back to source-based priors if the API is unavailable (climate-only boards get climate=4). Scores `fit` (0–5 role: tech focus, product/vision ownership, team leadership), `startup` (0–5 startup-ness: 5 = scrappy startup, 0 = bureaucratic enterprise), and `climate` (0–5 company mission) with a one-line `reason`. A **pearl** (supermatch) is all three ≥ 4 with sum ≥ 13 (`candidate_profile.is_pearl`); pearls sort first in both decks, get a 🦪 banner, and their cover letters come as "drunk drafts" (see cover_letter.py).
- **`jev.py`** — Optional TypeSafe Jev (`TYPESAFE_API_KEY`; one POST per job to `api.typesafe.ai/v1/systemone`, Score/Noul questions run in parallel, numbers only). `search_jobs` runs it after descriptions are fetched: three 6-level Scores (fit from `FIT_RUBRIC` + candidate profile, startup, climate) that override the LLM's scores (disagreements ≥ 2 are noted in `reason`), plus a Noul "region-locked" (≥ 0.8 drops the job unless its location field already names the searched place; drops land in `location_debug.csv` as `Jev: region-locked`). Jobs failing the fit/climate thresholds on Jev's scores are dropped *before* the LLM step, so the LLM only writes the one-line `reason` for survivors (and still does the full scoring for jobs Jev couldn't judge). No key or an API failure = LLM validator alone.
- **`search_cdrjobs()`** — CDRjobs (cdrjobs.earth/job-board, ~600 carbon-removal jobs). The Wix page is just a pym iframe over `embed.cdrjobs.earth`, whose Apollo GraphQL API (`prod-backend.cdrjobs.earth/graphql`, no login, introspection off) takes `clientKey` = the embed UUID. `limit` caps at 100, so it walks the keyset cursor `pagination{takeAfter: last publishedAt, takeAfterId: last id}`. Title/location filtered locally; `sourceUrl` is the employer's own ATS posting. If it breaks, re-read the embed URL from the page's "Add job board" HTML-embed snippet and the queries from the embed's `_next/static/chunks/*.js`.
- **`search_wttj()` / `wttj.py`** — Welcome to the Jungle (ex-Otta), **optional**. `app.welcometothejungle.com` is login-only (Google SSO) with no public API, so `cli.py --login-wttj` opens Playwright's bundled Chromium on a persistent profile (`.jobfinder_wttj_profile/`, gitignored) where the user signs in once; it also logs the app's API calls (URLs/bodies/response *shapes*, never headers/cookies) to `.jobfinder_wttj_capture.jsonl` for re-discovering the API if it changes. Playwright is the `wttj` extra (`uv sync --extra wttj && uv run playwright install chromium`); `search_wttj` silently returns `[]` without the profile or Playwright. Headless, it POSTs the app's GraphQL (`api.exp.welcometothejungle.com/graphql`, `_otta_session` cookie + `x-csrf-token` = the `_csrf_token` cookie): `jobRecommendations(ignoreSubfunctions: true)` (~90 roles; the strict queue is a handful) then `publicJob(externalId)` per job. It is a personalized queue, not a search, and read-only — never send `CreateJobReaction` (it would swipe on the user's real account). `insertedAt` is first-seen, not freshness, so jobs are undated. An expired session prints a re-login hint.
- **`get_company_info()`** — DuckDuckGo search for company sustainability info; cached in `company_cache.json`.
- **`save_jobs()` / `save_csv()`** — Appends to `jobs.csv`, deduplicates by the job's cross-device identity (`sync.norm_key`: company|title, or normalized URL when there's no real company+title) — the same key the sync doc id and swipe decisions use, so the same role from two sources (different URLs) is one job on every device. Handles dynamic columns. CSV columns include `fit_score`, `startup_score`, `climate_score`, `reason`.

### Data Flow

`--profile`: CLI → `run_profile_search()` → `search_jobs` (parallel sources → title filter → dedup → LLM validation → threshold + rank) → `save_jobs` → `jobs.csv` (deterministic, no agent loop).

Free-text search: CLI args → `ClaudeAgent.run()` → agent calls `search_jobs` → optionally `get_company_info` → `save_csv` → `jobs.csv`.

## Job Sources

Integrated: CDRjobs (GraphQL behind the Wix embed), climate.jobs (HTML scrape; full catalog incl. Remote Climate Jobs premium listings with the `CLIMATEJOBS_SESSION` cookie), Climatebase (public Algolia index — app `8PSNFFQTXQ`, index `Job_production`; if it stops working, re-extract credentials from their `_app-*.js` bundle), ClimateTechList (Airtable shared view), Greenhouse + Lever ATS boards (curated slugs, verified 2026-07), Jobs in Carbon (jobsincarbon.com sitemap; migrated off niceboard), Remote Impact (HTML), Arbeitnow, RemoteOK (HTML load-more feed, not /api), Adzuna (needs API key).

Probed but not integrable (checked 2026-07): **remoteclimatejobs.co** is a beehiiv newsletter whose jobs all live on **climate.jobs** (integrated above — a paid RCJ subscription = a climate.jobs login); **inclimate.com** serves an empty JS shell with no data API; **idealist.org** is a generalist board largely covered by Adzuna/Climatebase (Welcome to the Jungle is integrated as an optional login-gated source, see `wttj.py`); **climatehack.global / startup.ch / joinup.ch** have no machine-readable job feeds. Re-probe before assuming this is still true.

## Android App (`android/`)

Kotlin/Compose port of the TUI flow (see `android/README.md`): profile search over
the same ten sources → tinder-style swipe deck (most-recent-first) → streamed cover
letter / application answers / salary estimate. Also an `ACTION_SEND` share target:
share a job URL to the app to start applying. Terminal-look UI (monospace, dark,
underlined-word buttons) modeled on `tui.py`, including the splash art. Search
pipeline is a port of `agent.py` (`data/Sources.kt`, `data/SearchEngine.kt`); prompts
are verbatim from `cover_letter.py` (`data/Generator.kt`). Both sides have a **best
general roles (non-climate)** mode: `search_jobs(non_climate=True)` /
`SearchEngine.search(nonClimate=true)` invert the climate gate — keep only jobs with
fit ≥ `NONCLIMATE_MIN_FIT` (4, profile.json `nonclimate_min_fit`) and climate < 2 —
served as the `--non-climate` CLI flag and Android menu item (the TUI menu item instead *displays* the saved rejects, no new search); such jobs
live in the same store/sync and carry a 💼 card banner wherever they surface. Location policy is fixed
in `data/Profile.kt` (not profile-driven, unlike the Python side's `home_countries`): CH hybrid/remote, DE/NL remote-only. API keys are baked into
the gitignored `data/Keys.kt` (personal build; `Keys.kt.example` is the template);
`assets/resume.txt` + `assets/styleguide.md` are gitignored personal assets.
Build: `export JAVA_HOME=/home/milo/.local/share/jdks/jdk-21 && cd android && ./gradlew :app:assembleDebug`
(the Android Studio flatpak JBR moved to Java 25, which Gradle 8.13 cannot run on).
(toolchain mirrors ~/.code/Eigenflow: Gradle 8.13, AGP 8.12, Kotlin 2.0, compileSdk 34).
Unit tests (JVM, no device): `./gradlew :app:testDebugUnitTest`.

## Inbox Replies (Gmail scan)

Closes the loop after `/applied`: the **Android app** scans Gmail, works out which
replies are about jobs you applied to, and fires a notification for interview
invites. Python only reads the results — no mail credentials on the laptop.

- **Access is IMAP + app password**, not the Gmail API: JavaMail's Android port is
  pure JVM, so it needs no Google Play Services and works on GrapheneOS. Set
  `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` in the gitignored `data/Keys.kt` (2-step
  verification → https://myaccount.google.com/apppasswords). Blank = feature off.
- **`data/Mail.kt`** — IMAPS against `imap.gmail.com:993`, one login per scan.
  Incremental by UID; `mail_state.json` (filesDir) holds `uid_validity`/`last_uid`.
  A changed `uidValidity` invalidates stored UIDs and forces a date rescan.
  Paging is asymmetric on purpose: incremental scans take the **oldest** unseen
  page so the cursor never steps over a backlog, while the first run takes the
  **newest** of a 90-day window and jumps the cursor past the rest.
- **`data/ReplyClassifier.kt`** — two passes on `Anthropic.FAST_MODEL` with
  JSON-schema output. Pass 1 triages every new sender+subject in *one* call (a
  couple dozen subject lines is a tiny prompt, so there is no keyword prefilter
  that could silently drop an oddly-worded invite); pass 2 fetches bodies only for
  the candidates and returns `{status, job_index, confidence}`. Statuses:
  `invited` / `action_needed` / `rejected` / `acknowledged` / `other`.
- **`action_needed` is the second thing that notifies.** The application is parked
  until Milo answers screening questions, takes an assessment or re-uploads a CV
  ("Complete two questions to apply as …", "One more step to complete your
  application"). It is deliberately *not* `invited`: no human has offered to talk,
  and the screening vendors that send these serve them from URLs like
  `/candidate/assessment/invite`, so the word "invite" in the body proves nothing —
  both prompt and `Heuristics.ACTION_NEEDED` say a task is a task. It is also not
  `acknowledged`: ignoring one kills the application. Ordering in the heuristic
  fallback matters — a genuine strong invitation still wins, then rejections, then
  action, then weak invite evidence, then ack.
- **Recall over precision, everywhere.** Both prompts say so explicitly, and every
  failure path lands on `Heuristics` (EN/DE/NL patterns, weighted so one strong
  invitation phrase wins unless an equally strong rejection outweighs it) rather
  than skipping a message. `ReplyClassifierTest` asserts **recall == 1.00 on both
  invitations and `action_needed`** against `app/src/test/resources/mail_fixtures.json`
  (40 anonymised samples from the real inbox, July 2026 — wording verbatim,
  identities invented);
  per-label accuracy is reported, not asserted. `LiveClassifierTest` runs the same
  bar against the real prompts (treating `invited`/`action_needed` as
  interchangeable, since both notify), opt-in because it spends credit:
  `JOBFINDER_LIVE_TEST=1 ./gradlew :app:testDebugUnitTest --tests '*LiveClassifierTest*'`.
  Cases the corpus exists to pin down, each found by reading real mail rather than
  imagining it: a German invitation whose ask is only a calendar link
  ("Wähle dafür einfach einen freien Termin"); a plain-prose "I'd like to schedule
  a short call"; a follow-up nudge with almost no content; and ATS acknowledgements
  saying "**if** you are selected for an interview", which the conditional guard
  (`CONDITIONAL_INTERVIEW`) keeps from reading as invitations — including the
  "should your profile match our requirements, we will be in touch to schedule the
  next steps" variant that sits in the *same* ATS's acknowledgement as the
  `action_needed` mails. Known heuristic gap:
  a German soft rejection carrying none of the usual phrasing ("wir suchen jemanden,
  der Deutsch als Muttersprache spricht") lands on `other` — the LLM path gets it.
- **Out of API credits** (`data/LlmBudget.kt`): a 401/402/403/429 or a "credit
  balance" 400 pauses LLM classification for 6 h and the scan degrades to
  heuristics; timeouts and 5xx degrade too but pause nothing. A degraded scan
  **does not advance the UID cursor**, so those messages get a proper verdict once
  the API is reachable — records are keyed by message id, so the better verdict
  overwrites the provisional one, and `notified` is preserved so nothing buzzes
  twice.
- **`data/Matcher.kt`** — maps a reply to an applied job. `companyKey` mirrors
  `connections.py:company_key`; because that only strips suffixes it recognises as
  words ("Overstory B.V." → `overstory b v`), matching also falls back to the lead
  token so mail from `@overstory.com` still lands.
- **`work/InboxWorker.kt`** — WorkManager (JobScheduler, not Play Services), hourly
  + on launch + "scan now" on the replies screen. Doze can stretch the interval.
  It runs whether or not the app is open, but only after one manual launch —
  Android keeps a freshly installed or force-stopped app in the *stopped* state,
  and `AppViewModel.init` is what arms the worker.
- **One `Store`/`ScanState` per process** (`Store.of` / `ScanState.of`). Both cache
  their JSON in memory and rewrite the whole file, so the background worker and the
  UI must share instances: with one each, the worker's scan would notify about an
  invite the UI's stale snapshot never showed and then lose it on the next save.
  `InboxScanner.scan()` is serialized on a process-wide lock for the same reason,
  and `MainActivity.onResume` re-reads the list after a background scan.
- **Notifications** — two channels, both actionable, nothing else buzzes:
  `interviews` (HIGH) for invites, `applications` (DEFAULT) for `action_needed`.
  Rejections and acknowledgements update quietly. Tapping opens the replies screen.
- **Python side is read-only**: `sync.pull_replies()` mirrors the collection into
  the gitignored `job_replies.json` during `sync_down`; `sync.waiting_count()`
  (= `invites_waiting()` + `actions_waiting()`) / `replies_sorted()` back the TUI's
  `Replies (✉ N)` menu item, the shell's startup notice, and `/inbox [all]`.

## Cross-Device Sync (`sync.py`)

Optional Cloud Firestore sync (free tier) shared with the Android app. Backend:
Firebase project `dog-o-meter` (366421906137), Firestore `(default)` @ eur3, Email/
Password auth, rules locked to Milo's uid (`firestore.rules`; deploy with
`npx -y firebase-tools@latest deploy --only firestore`). Collections, with document
ids `sha1(company|title, normalized-URL fallback)` (`sync.norm_key`; jobs, decisions and dedup all share this identity so a role cross-posted under two URLs is one job everywhere — descriptions stay keyed by URL alone since page text is per-URL): `jobs` ↔ jobs.csv, `decisions` ↔
job_decisions.json (last `ts` wins; a swipe on one device hides the job everywhere),
`descriptions` ↔ job_descriptions.json (pushed on fetch, pulled on cache miss — so a
page one device can't scrape is served from the other's fetch;
`fetch_job_description` additionally falls back to the `r.jina.ai` rendering proxy
for bot-walled/JS-only pages), and `replies` → job_replies.json (doc id
`sha1(rfc822 message-id)`, one doc per email not per job; the Android inbox scan is
the only writer and Python pulls it read-only — see "Inbox Replies"). Python talks Firestore REST (no SDK); the Android app
uses the Firebase SDK (`data/Sync.kt`) against the same collections. Hooks:
`save_jobs` pushes new rows (`_push=False` inside sync itself), `run_profile_search`/
`run_connection_search`/`run_tui` pull *then push* at startup (a plain `sync_down`
would only ever pull — a `push_decision_async`/`push_jobs_async` that silently failed
or got killed mid-flight, e.g. the process exiting before its daemon thread finished,
would otherwise stay local-only forever; the startup `sync_up` flushes it), TUI swipes
also push in the background as they happen, and a URL used in the interactive shell
(`_register_url_job` in cli.py) becomes a saved job with source `shared`. The Android
app does the same full pull-then-push in `Store.syncWithRemote()` on every launch.
Configured via `FIREBASE_*` in `.env` (see `.env.example`);
unconfigured = every hook is a silent no-op. `uv run python sync.py` does a full
two-way sync and prints the auth uid for the rules. `FIREBASE_API_KEY=emulator` +
`FIRESTORE_EMULATOR_HOST` run it against a local Firestore emulator.

## Environment Variables

Defined in `.env` (see `.env.example`):
- `LLM_PROVIDER` — `openrouter` (Kimi K3 via OpenRouter's OpenAI-compatible API) or `anthropic` (Claude). Unset: openrouter if `OPENROUTER_API_KEY` is present, else anthropic. All LLM calls go through `llm.py`, which mimics the Anthropic SDK surface (`messages.create`/`messages.stream`, tool-use blocks, JSON-schema output) over OpenRouter; in openrouter mode the claude-* model names at call sites only pick the tier: opus-tier calls (cover letters, application answers) use `OPENROUTER_MODEL` (default `moonshotai/kimi-k3`), everything else uses `OPENROUTER_FAST_MODEL` (default `deepseek/deepseek-v4-flash`). 429/5xx responses retry with Retry-After-aware backoff.
- `OPENROUTER_API_KEY` / `OPENROUTER_MODEL` / `OPENROUTER_FAST_MODEL` (openrouter mode)
- `MARVIN_KEY` (optional — the local V100 llama rig, ik_llama.cpp). When set and the
  rig answers `GET $MARVIN_BASE_URL/models` at import, cover letters and resume tailoring
  run on whichever model llama-swap (`http://marvin:8080/v1`) has loaded: `MARVIN_PREFERRED_MODEL`
  (default `qwen3.8-27b`) if `/v1/models` reports it `loaded`, else `MARVIN_MODEL` (default
  `gandalf`); setting `MARVIN_MODEL` in `.env` pins it and skips the check. Probed at import and
  re-probed on each generation call while it is down (a swap stalls the call, so we never trigger one for the preferred model); when it is
  down, an interactive session gets a 5 s press-G-to-retry countdown before falling back to the normal tier models
  (non-interactive: falls back at once, then stays quiet for 60 s). A mid-call network error to marvin goes through
  the same retry/fallback. Every generation call prints the model it really runs on (`llm.describe_model`), and the resume
  and cover-letter passes stream the rig's reasoning (dimmed) so you can read along. Override
  with `MARVIN_BASE_URL` / `MARVIN_MODEL`.
- `MARVIN_THINKING` (optional, default `low`) — rig chat-template knob for cover letters
  and resume tailoring: `off` = enable_thinking=false, else passed as `reasoning_effort`
  (`low`/`medium` cost about the same on flash-next; `high` is ~3x slower for no gain).
  Tell-check/revision passes use `MARVIN_TELLCHECK_THINKING` (default `off` — mechanical
  flattening gains nothing from reasoning).
- `ANTHROPIC_API_KEY` (anthropic mode — agent loop `claude-opus-4-8`, validation `claude-haiku-4-5`, cover letters, resume tailoring)
- `ADZUNA_APP_ID` / `ADZUNA_API_KEY` (optional, for Adzuna source)
- `CLIMATEJOBS_SESSION` (optional — logged-in `cj_session` cookie for climate.jobs; unlocks the Remote Climate Jobs premium cards, free subset works without it)
- `FIREBASE_API_KEY` / `FIREBASE_PROJECT_ID` / `FIREBASE_EMAIL` / `FIREBASE_PASSWORD` (optional, cross-device sync)
