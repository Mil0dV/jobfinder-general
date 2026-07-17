#!/usr/bin/env python3
"""CLI interface for the Climate Job Finder Agent."""

import argparse
import atexit
import difflib
import os
import re
import readline  # arrow keys, history, Tab completion, Ctrl+R in input()
import select
import subprocess
import sys
import time
from dataclasses import dataclass
from agent import root_agent, run_profile_search
from cover_letter import (
    parse_command,
    list_jobs_brief,
    load_jobs_from_csv,
    get_job_by_number,
    generate_cover_letter,
    answer_application_question,
    estimate_salary,
    fetch_job_description,
    extract_job_meta,
)
from resume_tailor import tailor_resume


def run_search(job_title: str, location: str, job_type: str, experience: str):
    """Run a single job search."""
    query_parts = [f"Find {job_title} jobs in the climate and green tech sector"]

    if location:
        query_parts.append(f"in {location}")
    if job_type:
        query_parts.append(f"({job_type})")
    if experience:
        query_parts.append(f"for {experience}-level candidates")

    query = " ".join(query_parts)

    print(f"\n🔍 Searching for: {job_title}")
    if location:
        print(f"   Location: {location}")
    print("-" * 50)

    response = root_agent.run(query)
    print(response)


def _parse_standalone_val(val: str) -> dict | None:
    """Parse a standalone CLI value (job number or URL) into a command dict."""
    if val.startswith("http://") or val.startswith("https://"):
        return {"job_number": None, "url": val}
    elif val.isdigit():
        return {"job_number": int(val), "url": None}
    return None


def _resolve_job_context(cmd: dict) -> tuple[dict | None, str]:
    """Resolve a command's job/job_number/url into (job_dict, role_description).

    Accepts a pre-resolved `job` dict, a 1-indexed `job_number`, or a `url`.
    Returns (job, role_desc). job may be None for URL-only commands.
    role_desc is empty string on failure.
    """
    job = cmd.get("job")
    role_desc = ""
    if job is None and cmd.get("job_number"):
        job = get_job_by_number(cmd["job_number"])
        if not job:
            print(f"Job #{cmd['job_number']} not found.")
            return None, ""
    if job is not None:
        parts = [f"Title: {job.get('title', '')}"]
        parts.append(f"Company: {job.get('company', '')}")
        if job.get("location"):
            parts.append(f"Location: {job['location']}")
        if job.get("snippet"):
            parts.append(f"Description: {job['snippet']}")
        if job.get("url"):
            print(f"   Fetching full job description from {job['url']}...")
            fetched = fetch_job_description(job["url"])
            if fetched:
                parts.append(f"Full description:\n{fetched}")
        role_desc = "\n".join(parts)
        return job, role_desc
    if cmd.get("url"):
        print(f"   Fetching job description from {cmd['url']}...")
        role_desc = fetch_job_description(cmd["url"])
        if not role_desc:
            print("   Could not extract text from URL.")
            return None, ""
        return None, role_desc

    return None, role_desc


def _handle_resume_command(cmd: dict):
    """Execute a resume tailor command."""
    _job, role_desc = _resolve_job_context(cmd)
    if role_desc:
        tailor_resume(role_desc, extra_context=cmd.get("extra_context", ""))


def _handle_apply_command(cmd: dict):
    """Execute combined cover letter + tailored resume."""
    job, role_desc = _resolve_job_context(cmd)
    if not role_desc:
        return
    extra = cmd.get("extra_context", "")
    generate_cover_letter(job, extra_context=extra, url=cmd.get("url") or "")
    tailor_resume(role_desc, extra_context=extra)


# ---------------------------------------------------------------------------
# Interactive shell: a slash-command REPL with persistent history, Tab
# completion, did-you-mean hints, and Claude-Code-style conveniences.
#
# Design: one COMMANDS registry is the single source of truth. It drives
# dispatch, Tab completion, /help, and did-you-mean. A leading '/' is optional
# everywhere, so every legacy bare command ("cl 5", "set job 2", "list jobs")
# keeps working byte-for-byte; slash is purely additive.
# ---------------------------------------------------------------------------

INTERACTIVE_HISTORY_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".jobfinder_history"
)

# Legacy two-word forms collapsed to a single verb before dispatch.
_MULTIWORD = [
    (r"^cover\s+letter\b", "cl"),
    (r"^list\s+jobs?\b", "jobs"),
    (r"^show\s+jobs?\b", "jobs"),
    (r"^set\s+job\b", "set"),
    (r"^unset\s+job\b", "unset"),
    (r"^paste\s+job\b", "paste"),
]


@dataclass
class Command:
    """One interactive command. `run(args: str, ctx: _Ctx)` returns False to exit."""

    name: str
    run: object
    summary: str
    usage: str
    aliases: tuple = ()
    takes_job: bool = False


class _Ctx:
    """Mutable state shared across interactive commands within one session."""

    def __init__(self):
        # Active job: {"job": dict|None, "role_desc": str, "url": str, "label": str}
        self.session: dict | None = None
        # Last generation, for /retry and /redo.
        self.last_gen: dict | None = None
        # Liked jobs handed over by the TUI; /next steps through them.
        self.queue: list = []
        self.queue_pos: int = 0
        # True when the shell was opened from a TUI card ('a' on the deck);
        # /applied then exits back to the deck instead of advancing the queue.
        self.from_tui: bool = False


# --- target resolution (#, @name, url, or the active session job) ------------


def _resolve_mention(text: str) -> dict | None:
    """Fuzzy-match a saved job by title/company substring. None if 0 or >1 match."""
    text = text.strip().lower()
    if not text:
        print("   Usage: @<part of a job title or company>")
        return None
    jobs = load_jobs_from_csv()
    matches = [
        (i, j)
        for i, j in enumerate(jobs, 1)
        if text in f"{j.get('title', '')} {j.get('company', '')}".lower()
    ]
    if not matches:
        print(f"   No saved job matches '@{text}'. Try `jobs` to list them.")
        return None
    if len(matches) > 1:
        print(f"   '@{text}' matches {len(matches)} jobs — be more specific:")
        for i, j in matches[:10]:
            print(f"      {i}. {j.get('title', '?')} @ {j.get('company', '?')}")
        return None
    return matches[0][1]


def _register_url_job(url: str, role_desc: str):
    """Save a URL used in the shell as a job row (and sync it across devices).

    Skipped when jobs.csv already knows the URL, so extract_job_meta's Haiku
    call only happens for genuinely new postings.
    """
    try:
        norm = (url or "").split("?")[0].strip().rstrip("/")
        known = {
            (j.get("url") or "").split("?")[0].strip().rstrip("/") for j in load_jobs_from_csv()
        }
        if not norm or norm in known:
            return
        company, title = extract_job_meta(role_desc)
        from datetime import date

        from agent import save_jobs

        job = {
            "title": title,
            "company": company,
            "url": url,
            "posted_date": date.today().isoformat(),
            "source": "shared",
            "_desc": role_desc,
        }
        save_jobs([job])  # dedupes, writes jobs.csv, pushes to the sync DB
    except Exception:
        pass


def _register_pasted_job(role_desc: str, company: str, title: str) -> dict | None:
    """Save a pasted (URL-less) job description as a jobs.csv row (source "pasted").

    Returns the job dict to use as the session job, or None when the meta
    extraction couldn't identify the posting. save_jobs dedupes by
    company + title, so re-pasting the same job is harmless.
    """
    if company == "Unknown" or title == "Role":
        return None
    try:
        from datetime import date

        from agent import save_jobs

        job = {
            "title": title,
            "company": company,
            "url": "",
            "posted_date": date.today().isoformat(),
            "source": "pasted",
            "_desc": role_desc,
        }
        save_jobs([job])  # dedupes, writes jobs.csv, pushes to the sync DB
        return job
    except Exception:
        return None


def _resolve_target_core(target: str) -> tuple[dict | None, str, str] | None:
    """Resolve a single token (#|@name|url) to (job, url, role_desc), or None."""
    target = target.strip()
    if target.isdigit():
        job, role_desc = _resolve_job_context({"job_number": int(target)})
        if job is None:
            return None
        return job, "", role_desc
    if target.startswith("@"):
        job = _resolve_mention(target[1:])
        if job is None:
            return None
        _job, role_desc = _resolve_job_context({"job": job})
        return job, "", role_desc
    if target.startswith(("http://", "https://")):
        _job, role_desc = _resolve_job_context({"url": target})
        if not role_desc:
            return None
        _register_url_job(target, role_desc)
        return None, target, role_desc
    print(f"   Don't understand '{target}'. Use a job number, @name, or URL.")
    return None


def _split_target_extra(args: str) -> tuple[str, str]:
    """Split 'TARGET [- extra]' / 'TARGET [: extra]'. TARGET may be empty."""
    args = (args or "").strip()
    if not args:
        return "", ""
    head, _, rest = args.partition(" ")
    if head in ("-", ":"):  # bare "cl - extra"
        return "", rest.strip()
    rest = rest.strip()
    if not rest:
        return head, ""
    m = re.match(r"^[-:]\s*(.*)$", rest, re.DOTALL)
    return head, (m.group(1).strip() if m else rest)


def _resolve_target(args: str, ctx: _Ctx) -> tuple[dict | None, str, str, str] | None:
    """Resolve a command's argument string to (job, url, role_desc, extra).

    Falls back to the active session job when no target is given. Returns None
    (after printing why) when nothing resolves.
    """
    target, extra = _split_target_extra(args)
    if not target:
        if not ctx.session:
            print("   No active job. Pass a <job#|@name|url>, or `set <#|@name|url>` first.")
            return None
        s = ctx.session
        return s["job"], s["url"], s["role_desc"], extra
    resolved = _resolve_target_core(target)
    if resolved is None:
        return None
    job, url, role_desc = resolved
    return job, url, role_desc, extra


def _gen(ctx, kind, job, url, role_desc, extra="", question=None):
    """Run a generation and remember it for /retry and /redo.

    Pearl supermatches get the "drunk draft" cover letter (when drunk_mode is
    enabled in the profile): rougher and more creative on purpose, since those
    get rewritten by hand anyway. kind "drunk" (the /drunk command) forces
    that mode for any job.
    """
    from candidate_profile import DRUNK_MODE_ENABLED, is_pearl

    drunk = kind == "drunk" or (DRUNK_MODE_ENABLED and bool(job) and is_pearl(job))
    if kind in ("cl", "drunk"):
        generate_cover_letter(
            job, extra_context=extra, url=url, description=role_desc if not url else "", drunk=drunk
        )
    elif kind == "resume":
        tailor_resume(role_desc, extra_context=extra)
    elif kind == "apply":
        generate_cover_letter(
            job, extra_context=extra, url=url, description=role_desc if not url else "", drunk=drunk
        )
        tailor_resume(role_desc, extra_context=extra)
    elif kind == "salary":
        estimate_salary(job=job, url=url, description=role_desc if not url else "")
    elif kind == "answer":
        answer_application_question(question, job=job, url=url)
    ctx.last_gen = {
        "kind": kind,
        "job": job,
        "url": url,
        "role_desc": role_desc,
        "extra": extra,
        "question": question,
    }


# --- command handlers --------------------------------------------------------


def _cmd_cl(args, ctx):
    r = _resolve_target(args, ctx)
    if r:
        _gen(ctx, "cl", r[0], r[1], r[2], extra=r[3])


def _cmd_drunk(args, ctx):
    r = _resolve_target(args, ctx)
    if r:
        _gen(ctx, "drunk", r[0], r[1], r[2], extra=r[3])


def _cmd_resume(args, ctx):
    r = _resolve_target(args, ctx)
    if r:
        _gen(ctx, "resume", r[0], r[1], r[2], extra=r[3])


def _cmd_apply(args, ctx):
    r = _resolve_target(args, ctx)
    if r:
        _gen(ctx, "apply", r[0], r[1], r[2], extra=r[3])


def _cmd_applied(args, ctx):
    """Record that the user applied to a job.

    Stored as an "applied" decision in job_decisions.json (same store as TUI
    swipes, synced across devices), so the job never shows up in a deck again.
    Opened from a TUI card, the shell exits back to the deck (which advances
    past it). With a liked-jobs queue running, it advances to the next queued
    job instead. Otherwise it exits the shell.
    """
    r = _resolve_target(args, ctx)
    if r is None:
        return
    job, url, role_desc = r[0], r[1], r[2]
    from tui import _push_decision, load_decisions, norm_url, save_decisions

    if job:
        key = norm_url(job.get("url")) or f"{job.get('company')}|{job.get('title')}"
        company, title = job.get("company", ""), job.get("title", "")
    elif url:
        key = norm_url(url)
        company, title = extract_job_meta(role_desc)
    else:  # pasted description: key by company|title, like URL-less jobs
        company, title = extract_job_meta(role_desc)
        if company == "Unknown":
            print("   Can't identify this pasted job — mark it with a job #, @name, or URL.")
            return
        key = f"{company}|{title}"
    decisions = load_decisions()
    decisions[key] = {
        "decision": "applied",
        "title": title,
        "company": company,
        "ts": time.strftime("%Y-%m-%d %H:%M"),
    }
    save_decisions(decisions)
    _push_decision(key, decisions[key])
    print(f"\n   ✓ Applied: {title} @ {company} — stored & synced. Good luck! 🍀")
    if ctx.from_tui:
        return False  # back to the TUI deck, which advances past this card
    if ctx.queue:
        ctx.session = None
        if ctx.queue_pos < len(ctx.queue):
            _cmd_next("", ctx)
        else:
            print(f"   🎉 All {len(ctx.queue)} liked jobs handled. Queue finished.")
        return
    return False


def _cmd_salary(args, ctx):
    r = _resolve_target(args, ctx)
    if r:
        _gen(ctx, "salary", r[0], r[1], r[2], extra=r[3])


def _cmd_answer(line, ctx):
    """Answer an application question. `line` is the full 'answer ...'/'aq ...' input."""
    cmd = parse_command(line)
    if not cmd or cmd.get("action") != "answer":
        print("   Usage: answer [<#|url>]: <question>    or    aq: <question>")
        return
    job = None
    if cmd.get("job_number"):
        job = get_job_by_number(cmd["job_number"])
        if not job:
            print(f"   Job #{cmd['job_number']} not found.")
            return
    url = cmd.get("url") or ""
    role_desc = ""
    if not job and not url and ctx.session:
        job, url, role_desc = ctx.session["job"], ctx.session["url"], ctx.session["role_desc"]
    answer_application_question(cmd["question"], job=job, url=url)
    ctx.last_gen = {
        "kind": "answer",
        "job": job,
        "url": url,
        "role_desc": role_desc,
        "extra": "",
        "question": cmd["question"],
    }


def _cmd_answer_run(args, ctx):
    """Registry adapter (dispatch routes answer/aq directly via the full line)."""
    _cmd_answer(f"answer {args}".strip(), ctx)


def _cmd_jobs(args, ctx):
    print(list_jobs_brief())


def _cmd_set(args, ctx):
    target = args.strip()
    if target.lower().startswith("job "):  # safety net for "set job <x>"
        target = target[4:].strip()
    if not target:
        print("   Usage: set <job#|@name|url>")
        return
    resolved = _resolve_target_core(target)
    if resolved is None:
        return
    job, url, role_desc = resolved
    company, title = extract_job_meta(role_desc, job)
    ctx.session = {"job": job, "role_desc": role_desc, "url": url, "label": f"{company} - {title}"}
    print(f"   Active job set: {ctx.session['label']}")
    print("   Commands without a #/url now use this job.")


def _cmd_unset(args, ctx):
    ctx.session = None
    print("   Active job cleared.")


def _load_liked_queue(ctx) -> int:
    """Fill ctx.queue with liked-but-not-applied jobs from disk. Returns the count."""
    from tui import liked_from_csv, load_decisions

    ctx.queue = liked_from_csv(load_decisions())
    ctx.queue_pos = 0
    return len(ctx.queue)


def _cmd_liked(args, ctx):
    """List liked-but-not-applied jobs (right-swipes) and queue them for /next."""
    from candidate_profile import is_pearl

    if not _load_liked_queue(ctx):
        print("   No liked jobs waiting — swipe right on some in `jobfinder --tui`.")
        return
    print(f"\n   ♥ {len(ctx.queue)} liked job(s) not applied to yet:")
    for job in ctx.queue:
        num = f"#{job['_num']}" if job.get("_num") else ""
        pearl = "🦪 " if is_pearl(job) else ""
        print(f"      {num:>4}  {pearl}{job.get('title', '?')} @ {job.get('company', '?')}")
    print("   Queued — /next steps through them; /applied checks one off.")


def _cmd_next(args, ctx):
    """Advance to the next liked job from the queue and make it the active job."""
    if not ctx.queue and _load_liked_queue(ctx):
        print(f"   ♥ Loaded {len(ctx.queue)} liked job(s) from your earlier swipes.")
    if not ctx.queue:
        print("   No liked jobs waiting — swipe right on some in `jobfinder --tui`.")
        return
    if ctx.queue_pos >= len(ctx.queue):
        print(f"   🎉 All {len(ctx.queue)} liked jobs handled. Queue finished.")
        return
    job = ctx.queue[ctx.queue_pos]
    ctx.queue_pos += 1
    _job, role_desc = _resolve_job_context({"job": job})
    company, title = extract_job_meta(role_desc, job)
    ctx.session = {
        "job": job,
        "role_desc": role_desc,
        "url": "",
        "label": f"{company} - {title}",
    }
    remaining = len(ctx.queue) - ctx.queue_pos
    print(f"\n   [{ctx.queue_pos}/{len(ctx.queue)}] Active job: {ctx.session['label']}")
    print(
        "   /cl /resume /apply /salary /url use it · /next for the following one"
        + (f" ({remaining} left)" if remaining else " (last one!)")
    )


def _cmd_paste(args, ctx):
    print("   Paste the job description below.")
    print("   Type END on its own line when done (or Ctrl+D):\n")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "END":
            break
        lines.append(line)
    pasted = "\n".join(lines).strip()
    if not pasted:
        print("   Nothing pasted.")
        return
    company, title = extract_job_meta(pasted)
    job = _register_pasted_job(pasted, company, title)
    ctx.session = {"job": job, "role_desc": pasted, "url": "", "label": f"{company} - {title}"}
    print(f"   Active job set: {ctx.session['label']}")
    if job:
        print("   Saved to jobs.csv (synced) — /applied will remember it across devices.")
    print("   Commands without a #/url now use this job.")


def _cmd_url(args, ctx):
    """Open the job's posting URL in the browser (active job by default)."""
    r = _resolve_target(args, ctx)
    if r is None:
        return
    job, url, _role_desc, _extra = r
    url = url or (job.get("url", "") if job else "")
    if not url:
        print("   This job has no URL (pasted description?).")
        return
    print(f"   Opening {url}")
    _os_open(url)


def _cmd_open(args, ctx):
    """Open the last generated docx (or a given file) with the OS default app."""
    path = args.strip().strip("'\"")
    if not path:
        path = _latest_application_file(".docx")
        if not path:
            print("   No .docx files found in applications/.")
            return
    if not os.path.isfile(path):
        print(f"   File not found: {path}")
        return
    print(f"   Opening {os.path.basename(path)}")
    _os_open(path)


def _cmd_pdf(args, ctx):
    """Redo the docx → pdf conversion (same as `jobfinder -p`)."""
    from resume_tailor import docx_to_pdf

    path = args.strip().strip("'\"")
    if not path:
        path = _latest_application_file(".docx")
        if not path:
            print("   No .docx files found in applications/.")
            return
        print(f"   Converting: {os.path.basename(path)}")
    if not os.path.isfile(path):
        print(f"   File not found: {path}")
        return
    if not path.lower().endswith(".docx"):
        print(f"   Expected a .docx file, got: {path}")
        return
    pdf = docx_to_pdf(path, os.path.dirname(os.path.abspath(path)))
    if pdf:
        print(f"   Saved: {pdf}")


def _cmd_status(args, ctx):
    try:
        n = len(load_jobs_from_csv())
    except Exception:
        n = 0
    print(f"   jobs.csv: {n} saved job(s)")
    if not ctx.session:
        print("   Active job: none — use `set <#|@name|url>` or paste a description.")
    else:
        s = ctx.session
        source = "URL" if s["url"] else ("csv job" if s["job"] else "pasted text")
        words = len(s["role_desc"].split())
        print(f"   Active job: {s['label']}")
        print(f"   Source: {source} · description {words} word(s)")
    if ctx.last_gen:
        print(f"   Last generation: {ctx.last_gen['kind']} (use /retry or /redo)")


def _cmd_retry(args, ctx):
    if not ctx.last_gen:
        print("   Nothing to retry yet — run a generation first.")
        return
    g = ctx.last_gen
    _gen(
        ctx, g["kind"], g["job"], g["url"], g["role_desc"], extra=g["extra"], question=g["question"]
    )


def _cmd_redo(args, ctx):
    if not ctx.last_gen:
        print("   Nothing to redo yet — run a generation first.")
        return
    note = args.strip()
    g = ctx.last_gen
    extra = f"{g['extra']} {note}".strip() if g["extra"] else note
    _gen(ctx, g["kind"], g["job"], g["url"], g["role_desc"], extra=extra, question=g["question"])


def _cmd_help(args, ctx):
    name = args.strip().lower().lstrip("/")
    if name:
        cmd = COMMANDS.get(ALIASES.get(name, name))
        if not cmd:
            print(f"   No such command: {name}")
            return
        print(f"\n   /{cmd.name} — {cmd.summary}")
        print(f"   usage: {cmd.usage}")
        if cmd.aliases:
            print(f"   aliases: {', '.join('/' + a for a in cmd.aliases)}")
        print()
        return
    _print_help_table()


def _cmd_exit(args, ctx):
    return False


# --- small helpers used by handlers ------------------------------------------


def _os_open(path):
    """Open a file/dir with the OS default app; fall back to printing the path."""
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        subprocess.Popen([opener, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (FileNotFoundError, OSError):
        print(f"   {os.path.abspath(path)}")


def _latest_application_file(ext: str | None = None) -> str | None:
    """Most-recently-modified file in applications/, optionally filtered by extension."""
    apps = os.path.join(os.path.dirname(os.path.abspath(__file__)), "applications")
    if not os.path.isdir(apps):
        return None
    files = [
        os.path.join(apps, f) for f in os.listdir(apps) if ext is None or f.lower().endswith(ext)
    ]
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def _run_shell(command):
    if not command:
        print("   Usage: !<shell command>   e.g.  !ls -t applications | head")
        return
    try:
        subprocess.run(command, shell=True)
    except Exception as e:  # KeyboardInterrupt (BaseException) propagates to the loop
        print(f"   Shell error: {e}")


def _suggest(verb):
    names = sorted(set(list(COMMANDS) + list(ALIASES)))
    match = difflib.get_close_matches(verb, names, n=1, cutoff=0.6)
    if match:
        print(f"   Unknown command '{verb}'. Did you mean /{match[0]}?  (type /help for all)")
    else:
        print("   Unknown command. Type /help for the command list.")


# --- registry ----------------------------------------------------------------


def _build_registry():
    cmds = [
        Command(
            "cl",
            _cmd_cl,
            "Write a cover letter",
            "cl [#|@name|url] [- extra]",
            ("cover", "coverletter"),
            True,
        ),
        Command(
            "drunk",
            _cmd_drunk,
            "Cover letter as a drunk draft (what pearls get)",
            "drunk [#|@name|url] [- extra]",
            ("dcl", "drunk-cl"),
            True,
        ),
        Command(
            "resume",
            _cmd_resume,
            "Tailor your resume to a role",
            "resume [#|@name|url] [- extra]",
            ("res", "tailor"),
            True,
        ),
        Command(
            "apply",
            _cmd_apply,
            "Cover letter + tailored resume",
            "apply [#|@name|url] [- extra]",
            (),
            True,
        ),
        Command(
            "applied",
            _cmd_applied,
            "Mark a job as applied (stored + synced); next in queue, or exit",
            "applied [#|@name|url]",
            ("did",),
            True,
        ),
        Command(
            "salary",
            _cmd_salary,
            "Estimate the salary for a role",
            "salary [#|@name|url]",
            ("sal",),
            True,
        ),
        Command(
            "answer",
            _cmd_answer_run,
            "Answer an application question",
            "answer [#|url]: <q>   ·   aq: <q>",
            ("aq",),
            True,
        ),
        Command("jobs", _cmd_jobs, "List saved jobs from jobs.csv", "jobs", ("list", "ls", "show")),
        Command("liked", _cmd_liked, "List liked jobs not yet applied to; queue them", "liked"),
        Command("next", _cmd_next, "Step to the next liked job", "next", ("n",)),
        Command("set", _cmd_set, "Set the active session job", "set <#|@name|url>"),
        Command("unset", _cmd_unset, "Clear the active session job", "unset"),
        Command("paste", _cmd_paste, "Paste a job description (END to finish)", "paste"),
        Command("open", _cmd_open, "Open the last generated docx", "open [docx_path]", ("o",)),
        Command(
            "url", _cmd_url, "Open the job URL in the browser", "url [#|@name|url]", ("u",), True
        ),
        Command(
            "pdf",
            _cmd_pdf,
            "Convert a docx to pdf (default: latest)",
            "pdf [docx_path]",
            ("to-pdf", "topdf"),
        ),
        Command("status", _cmd_status, "Show the active job + jobs.csv count", "status", ("st",)),
        Command("retry", _cmd_retry, "Re-run the last generation", "retry"),
        Command("redo", _cmd_redo, "Re-run last generation with extra guidance", "redo <extra>"),
        Command("help", _cmd_help, "Show this help", "help [command]", ("?", "h")),
        Command("exit", _cmd_exit, "Exit interactive mode", "exit", ("quit", "q")),
    ]
    commands = {c.name: c for c in cmds}
    aliases = {a: c.name for c in cmds for a in c.aliases}
    job_args = {c.name for c in cmds if c.takes_job}
    return commands, aliases, job_args


COMMANDS, ALIASES, _JOB_ARG_COMMANDS = _build_registry()


# --- readline: history + Tab completion --------------------------------------

_completion_cache: list[str] = []


def _complete_candidates(text: str) -> list[str]:
    buf = readline.get_line_buffer()
    begin = readline.get_begidx()
    if buf[:begin].strip() == "":
        # Completing the command verb itself.
        has_slash = text.startswith("/")
        word = (text[1:] if has_slash else text).lower()
        names = sorted(set(list(COMMANDS) + list(ALIASES)))
        matches = [n for n in names if n.startswith(word)]
        return [("/" + n) if has_slash else n for n in matches]
    # Completing an argument — offer job numbers for commands that take a job.
    first = buf[:begin].strip().lstrip("/").split()
    verb = first[0].lower().rstrip(":") if first else ""
    verb = ALIASES.get(verb, verb)
    if verb in _JOB_ARG_COMMANDS:
        try:
            count = len(load_jobs_from_csv())
        except Exception:
            count = 0
        return [str(i) for i in range(1, count + 1) if str(i).startswith(text)]
    return []


def _completer(text, state):
    if state == 0:
        _completion_cache.clear()
        _completion_cache.extend(_complete_candidates(text))
    try:
        return _completion_cache[state]
    except IndexError:
        return None


def _save_history():
    try:
        readline.write_history_file(INTERACTIVE_HISTORY_FILE)
    except OSError:
        pass


def _setup_readline():
    try:
        readline.read_history_file(INTERACTIVE_HISTORY_FILE)
    except (FileNotFoundError, OSError):
        pass
    readline.set_history_length(1000)
    atexit.register(_save_history)
    readline.set_completer(_completer)
    readline.set_completer_delims(" \t\n")
    if "libedit" in (getattr(readline, "__doc__", "") or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")


# --- dispatch + loop ---------------------------------------------------------


def _dispatch(raw, ctx):
    """Route one line of input. Returns False to exit the shell, else True."""
    raw = raw.strip()
    if not raw:
        return True
    if raw.startswith("!"):  # shell escape
        _run_shell(raw[1:].strip())
        return True

    # A leading '/' is optional: `/cl 5` and `cl 5` are identical.
    line = raw[1:].lstrip() if raw.startswith("/") else raw
    if not line:
        return True

    # Application-question commands keep their dedicated colon-aware parser.
    if re.match(r"^(?:answer|aq)(?:[:\s]|$)", line, re.IGNORECASE):
        _cmd_answer(line, ctx)
        return True

    # Collapse legacy two-word forms (set job / list jobs / ...) to one verb.
    for pat, repl in _MULTIWORD:
        new = re.sub(pat, repl, line, count=1, flags=re.IGNORECASE)
        if new != line:
            line = new
            break

    verb, _, args = line.partition(" ")
    verb = verb.lower().rstrip(":")
    args = args.strip()
    cmd = COMMANDS.get(ALIASES.get(verb, verb))
    if cmd:
        return cmd.run(args, ctx) is not False

    _suggest(verb)
    return True


def _absorb_paste(first_line, ctx):
    """If extra lines are buffered in stdin (a paste), capture them as the active job."""
    extra = []
    try:
        while select.select([sys.stdin], [], [], 0)[0]:
            line = sys.stdin.readline()
            if not line:
                break
            extra.append(line.rstrip("\r\n"))
    except Exception:
        pass
    if not extra:
        return False
    if extra[-1].strip().upper() == "END":
        extra.pop()
    full = (first_line + "\n" + "\n".join(extra)).strip()
    company, title = extract_job_meta(full)
    job = _register_pasted_job(full, company, title)
    ctx.session = {"job": job, "role_desc": full, "url": "", "label": f"{company} - {title}"}
    print(f"\n   Detected pasted job description ({len(full.split())} words).")
    print(f"   Active job set: {ctx.session['label']}")
    if job:
        print("   Saved to jobs.csv (synced) — /applied will remember it across devices.")
    print("   Use /cl, /resume, /apply, or /salary to generate documents.")
    return True


def _print_help_table():
    print("\n  Commands — a leading '/' is optional (`cl 5` == `/cl 5`):\n")
    for cmd in COMMANDS.values():
        aliases = f"  ({', '.join('/' + a for a in cmd.aliases)})" if cmd.aliases else ""
        print(f"    /{cmd.name:<7} {cmd.summary}{aliases}")
    print("\n    !<cmd>      run a shell command (e.g. !ls -t applications | head)")
    print("    @name       fuzzy-pick a saved job by title/company (anywhere a # works)")
    print("    - extra     trailing ' - text' adds guidance, e.g.  cl 5 - mention HN")
    print("\n    Tab: complete commands & job numbers  ·  ↑ / Ctrl+R: history (saved across runs)")
    print("    Ctrl+C: cancel a running generation  ·  Ctrl+C at an empty prompt: exit\n")


def _print_banner():
    print("\nEntering interactive mode. Type /help for commands, /exit to quit.")
    print(
        "  · Tab completes commands & job numbers; ↑ and Ctrl+R recall history (saved across runs)."
    )
    print(
        "  · Set an active job with `set <#|@name|url>` or paste a description, then /cl /resume /apply."
    )
    print("  · A leading '/' is optional — `cl 5` and `/cl 5` are identical.\n")


def _run_interactive(liked_queue: list | None = None, initial_job: dict | None = None):
    """Run the interactive document-generation shell.

    `initial_job` (a job dict, e.g. the card the TUI's apply key was pressed on)
    becomes the active session job; exiting the shell returns to the caller.
    """
    ctx = _Ctx()
    if liked_queue:
        ctx.queue = liked_queue
    _setup_readline()
    _print_banner()
    if ctx.queue:
        print(f"   ♥ {len(ctx.queue)} liked job(s) queued from the TUI — type /next to start.\n")
    if initial_job is not None:
        ctx.from_tui = True
        _job, role_desc = _resolve_job_context({"job": initial_job})
        company, title = extract_job_meta(role_desc, initial_job)
        ctx.session = {
            "job": initial_job,
            "role_desc": role_desc,
            "url": "",
            "label": f"{company} - {title}",
        }
        print(f"   Active job: {ctx.session['label']}")
        print(
            "   /cl /resume /apply /salary use it · /applied marks it done"
            " · /exit returns to the deck.\n"
        )

    while True:
        try:
            label = f" [{ctx.session['label']}]" if ctx.session else ""
            raw = input(f"\n🌱{label} You: ").strip()
        except KeyboardInterrupt:
            print("\n\nGoodbye! Good luck with your climate tech job search! 🌍")
            break
        except EOFError:
            break

        if not raw:
            continue

        # A pasted multi-line blob becomes the active job (never parsed as a command).
        if _absorb_paste(raw, ctx):
            continue

        try:
            if not _dispatch(raw, ctx):
                print("\nGoodbye! Good luck with your climate tech job search! 🌍")
                break
        except KeyboardInterrupt:
            print("\n   (cancelled — back at the prompt; Ctrl+C again at an empty prompt exits)")
        except Exception as e:
            print(f"\n   Error: {e}")


_HELP = """\
Climate & Green Tech Job Finder

Usage:
  jobfinder "Job Title"               search for jobs
  jobfinder [options] JOB_NUM|'URL'   generate documents for a job

Search:
  job_titles                    comma-separated titles, e.g. "PM,Engineer"
  --tui                         full-screen TUI: search (profile or role, with
                                location history), swipe through results with
                                ←/→ (u = undo), then write applications for the
                                liked jobs in interactive mode
  --profile                     search all target roles from your profile
                                (profile.json — run --setup to create one)
                                (Head of Product, CTO, founding engineer, EiR, ...)
  -l / --location  LOCATION     filter by location
  -t / --job-type  TYPE         full-time | part-time | contract | remote
  -e / --experience LEVEL       entry | mid | senior
  -i / --interactive            enter interactive mode (after search, or alone)

Interactive mode (enter with -i): a slash-command shell. A leading '/' is
optional, so `cl 5` and `/cl 5` are identical. Type /help inside for the full
list; Tab completes commands & job numbers, ↑/Ctrl+R recall history (persisted).
  liked              list liked-but-not-applied jobs and queue them
  next               step to the next liked job (loads earlier swipes if no queue)
  set <#|@name|url>  set the active job — commands then default to it
  paste              paste a job description directly (type END when done;
                     identified postings are saved to jobs.csv + synced)
  cl / resume / apply / salary [#|@name|url] [- extra]   generate documents
  drunk [#|@name|url]  cover letter as a drunk draft (what pearls get by default)
  applied [#|@name|url]  mark a job as applied (stored + synced); queue → next, else exit
  answer [#]: <q>    answer an application question      aq: <q>   (no job context)
  retry / redo <x>   re-run the last generation (redo appends extra guidance)
  pdf [docx_path]    redo the docx → pdf conversion (default: latest docx)
  open [docx_path]   open the last generated docx        url [#]  open the job URL
  status             show session
  jobs               numbered job list                   !<cmd>   run a shell command

Application:
  -c / --cover-letter  JOB|URL  generate a cover letter
  -r / --resume        JOB|URL  generate tailored resume
  -s / --salary        JOB|URL  estimate salary
  -a / --apply         JOB|URL  cover letter + tailored resume
  -x / --extra  'TEXT'          extra context / tailoring notes for -c / -r / -s / -a

Utilities:
  --setup                       first-time setup: import your resume, build your
                                search profile (profile.json) and style guide
  -p / --to-pdf  [DOCX_PATH]    convert docx → pdf (omit path for last modified in applications/)
  -h / --help                   show this help

Notes:
  • Always quote URLs with single quotes (URLs often contain & which bash splits on):
      jobfinder -a 'https://example.com/job?ref=x&utm=y'
  • Extra context is a separate flag, not a positional argument:
      jobfinder -a 'https://example.com/job' -x 'I found this via HN'

Examples:
  jobfinder --profile -l Switzerland    # search all your target roles
  jobfinder "Software Engineer"
  jobfinder "Data Scientist" -l Switzerland
  jobfinder "PM,Engineer" -l Germany -i
  jobfinder -c 5
  jobfinder -s 5
  jobfinder -a 5 -x 'Mention I found this via HN'
  jobfinder -a 'https://example.com/job?ref=x&utm=y' -x 'Extra context'
  jobfinder -p                          # convert last modified docx in applications/
  jobfinder -p 'applications/CL_Ionity - PM.docx'
"""


def main():
    """Run the climate job finder with CLI arguments."""
    parser = argparse.ArgumentParser(add_help=False)

    parser.add_argument("job_titles", nargs="?", default="")
    parser.add_argument("-l", "--location", default="")
    parser.add_argument(
        "-t", "--job-type", default="", choices=["", "full-time", "part-time", "contract", "remote"]
    )
    parser.add_argument("-e", "--experience", default="", choices=["", "entry", "mid", "senior"])
    parser.add_argument("-i", "--interactive", action="store_true")
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--tui", action="store_true")
    parser.add_argument("--setup", action="store_true")
    parser.add_argument(
        "-c", "--cover-letter", "--cl", metavar="JOB|URL", default=None, dest="cover_letter"
    )
    parser.add_argument("-r", "--resume", "--tailor", metavar="JOB|URL", default=None)
    parser.add_argument("-s", "--salary", metavar="JOB|URL", default=None)
    parser.add_argument("-a", "--apply", metavar="JOB|URL", default=None)
    parser.add_argument("-x", "--extra", metavar="TEXT", default="")
    parser.add_argument("-p", "--to-pdf", metavar="DOCX_PATH", nargs="?", const="", default=None)
    parser.add_argument("-h", "--help", action="store_true", default=False)

    args = parser.parse_args()

    if args.help:
        print(_HELP)
        return

    # --- First-time setup wizard ---
    if args.setup:
        from setup_wizard import run_setup

        try:
            run_setup()
        except (KeyboardInterrupt, EOFError):
            print("\nSetup aborted.")
        return

    # Offer setup automatically on a fresh install: no profile config AND no resume.
    from candidate_profile import PROFILE_EXISTS

    if not PROFILE_EXISTS:
        from resume_data import RESUME_JSON_PATH, RESUME_SOURCE_CANDIDATES

        has_resume = any(os.path.exists(p) for p in RESUME_SOURCE_CANDIDATES + [RESUME_JSON_PATH])
        if not has_resume:
            try:
                answer = input("No profile found — run first-time setup? [Y/n]: ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                print()
                return
            if answer in ("", "y", "yes"):
                from setup_wizard import run_setup

                try:
                    run_setup()
                except (KeyboardInterrupt, EOFError):
                    print("\nSetup aborted.")
                return

    # --- TUI: search → swipe → interactive mode over the liked jobs ---
    if args.tui:
        from tui import run_tui

        liked = run_tui()
        if liked:
            _run_interactive(liked_queue=liked)
        return

    # --- Docx to PDF conversion ---
    if args.to_pdf is not None:
        from resume_tailor import docx_to_pdf

        path = args.to_pdf
        if not path:
            # No path given — find last modified docx in applications/
            path = _latest_application_file(".docx")
            if not path:
                print("Error: no .docx files found in applications/")
                return
            print(f"Converting: {path}")
        if not os.path.isfile(path):
            print(f"Error: file not found: {path}")
            return
        if not path.lower().endswith(".docx"):
            print(f"Error: expected a .docx file, got: {path}")
            return
        pdf = docx_to_pdf(path, os.path.dirname(os.path.abspath(path)))
        if pdf:
            print(f"Saved: {pdf}")
        return

    # --- Standalone action modes ---
    standalone_val = args.apply or args.resume or args.salary or args.cover_letter
    standalone_mode = (
        "apply"
        if args.apply
        else (
            "resume"
            if args.resume
            else "salary" if args.salary else "cover-letter" if args.cover_letter else None
        )
    )

    if standalone_mode:
        # Detect the common mistake of passing extra text as a positional arg instead of -x
        if args.job_titles:
            flag = {"apply": "-a", "resume": "-r", "salary": "-s", "cover-letter": "-c"}[
                standalone_mode
            ]
            print(
                f"\nError: unexpected positional argument '{args.job_titles}'.\n"
                f"\nDid you mean to pass extra context? Use -x:\n"
                f"  jobfinder {flag} '{standalone_val}' -x '{args.job_titles}'\n"
                f"\nNote: URLs containing '&' must be quoted with single quotes to prevent\n"
                f"bash from splitting them. Example:\n"
                f"  jobfinder -a 'https://example.com/job?ref=x&utm=y' -x 'Extra context'"
            )
            return

        val = standalone_val
        cmd = _parse_standalone_val(val)
        if cmd is None:
            print(
                f"Invalid value for -{standalone_mode[0]}: expected a job number or URL, got '{val}'"
            )
            return

        cmd["extra_context"] = args.extra

        if standalone_mode == "apply":
            _handle_apply_command(cmd)
        elif standalone_mode == "resume":
            _handle_resume_command(cmd)
        elif standalone_mode == "salary":
            job = None
            if cmd.get("job_number"):
                job = get_job_by_number(cmd["job_number"])
                if not job:
                    print(f"Job #{cmd['job_number']} not found in jobs.csv.")
                    print(list_jobs_brief())
                    return
            estimate_salary(job=job, url=cmd.get("url") or "")
        else:
            job = None
            if cmd.get("job_number"):
                job = get_job_by_number(cmd["job_number"])
                if not job:
                    print(f"Job #{cmd['job_number']} not found in jobs.csv.")
                    print(list_jobs_brief())
                    return
            generate_cover_letter(job, extra_context=args.extra, url=cmd.get("url") or "")
        return

    # --- Profile search: deterministic pipeline over all target roles ---
    if args.profile or args.job_titles.strip().lower() == "profile":
        print("=" * 70)
        print("🌱 Climate & Green Tech Job Finder — target-role profile search")
        print("=" * 70)
        query = "" if args.job_titles.strip().lower() in ("", "profile") else args.job_titles
        run_profile_search(location=args.location, query=query)
        if args.interactive:
            _run_interactive()
        return

    if not args.job_titles and not args.interactive:
        print(_HELP)
        return

    # --- Interactive-only mode (no search) ---
    if args.interactive and not args.job_titles:
        _run_interactive()
        return

    # Parse comma-separated job titles
    job_titles = [title.strip() for title in args.job_titles.split(",")]

    print("=" * 70)
    print("🌱 Climate & Green Tech Job Finder")
    print("=" * 70)
    print(f"\nRoles to search: {', '.join(job_titles)}")
    if args.location:
        print(f"Location: {args.location}")
    print("Results will be saved/appended to: jobs.csv")
    print("\n" + "=" * 70)

    # Run the agent for each job title
    try:
        for job_title in job_titles:
            run_search(job_title, args.location, args.job_type, args.experience)

        print("\n" + "=" * 70)
        print("✅ All searches complete! Check jobs.csv for results.")
        print("=" * 70)

        if args.interactive:
            _run_interactive()

    except Exception as e:
        print(f"\n❌ Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
