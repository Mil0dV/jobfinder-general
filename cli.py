#!/usr/bin/env python3
"""CLI interface for the Climate Job Finder Agent."""

import argparse
import os
import readline  # noqa: F401 — enables arrow keys, Home/End, Ctrl+Left/Right in input()
import select
import sys
from agent import root_agent
from cover_letter import (
    parse_command,
    list_jobs_brief,
    get_job_by_number,
    generate_cover_letter,
    answer_application_question,
    estimate_salary,
    fetch_job_description,
    extract_job_meta,
)
from resume_tailor import tailor_resume, generate_base_resume


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


def _parse_action_command(text: str, prefixes: tuple[str, ...]) -> dict | None:
    """Parse '<prefix> <job#|url> [- extra context]' commands."""
    import re

    pattern = r"^(?:" + "|".join(prefixes) + r")\s+(.+)"
    m = re.match(pattern, text.strip(), re.IGNORECASE)
    if not m:
        return None
    rest = m.group(1).strip()

    # Split off optional extra context after " - "
    extra_context = ""
    ctx_match = re.match(r"^(\S+)\s+-\s+(.+)", rest)
    if ctx_match:
        rest = ctx_match.group(1).strip()
        extra_context = ctx_match.group(2).strip()

    if rest.isdigit():
        return {"job_number": int(rest), "url": None, "extra_context": extra_context}
    if rest.startswith("http://") or rest.startswith("https://"):
        return {"job_number": None, "url": rest, "extra_context": extra_context}
    return None


def _resolve_job_context(cmd: dict) -> tuple[dict | None, str]:
    """Resolve a command's job_number/url into (job_dict, role_description).

    Returns (job, role_desc). job may be None for URL-only commands.
    role_desc is empty string on failure.
    """
    job = None
    role_desc = ""
    if cmd.get("job_number"):
        job = get_job_by_number(cmd["job_number"])
        if not job:
            print(f"Job #{cmd['job_number']} not found.")
            return None, ""
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
    elif cmd.get("url"):
        print(f"   Fetching job description from {cmd['url']}...")
        role_desc = fetch_job_description(cmd["url"])
        if not role_desc:
            print("   Could not extract text from URL.")
            return None, ""

    return job, role_desc


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


def _run_interactive():
    """Run the interactive cover letter / job search loop."""
    import re as _re

    # Session job: persists for the duration of this interactive session.
    # {"job": dict|None, "role_desc": str, "url": str, "label": str}
    session: dict | None = None

    def _set_session(target: str):
        nonlocal session
        cmd = _parse_standalone_val(target)
        if cmd is None:
            print("   Usage: set job <job#>  |  set job <url>")
            return
        job, role_desc = _resolve_job_context(cmd)
        if not role_desc:
            return
        company, title = extract_job_meta(role_desc, job)
        session = {
            "job": job,
            "role_desc": role_desc,
            "url": cmd.get("url") or "",
            "label": f"{company} - {title}",
        }
        print(f"   Session job set: {session['label']}")
        print("   Commands without a job# or URL will now use this job.")

    def _session_job_or(cmd_job, cmd_url):
        """Return (job, url) from the command, falling back to the session job."""
        if cmd_job or cmd_url:
            return cmd_job, cmd_url or ""
        if session:
            return session["job"], session["url"]
        return None, ""

    print("\nEntering interactive mode. Type 'exit' or 'quit' to end.")
    print("  set job <#|url>              — set active job for this session")
    print("  paste job                    — paste a job description directly")
    print("  unset job                    — clear active job")
    print("  cl [#|url] [- extra]         — cover letter")
    print("  resume [#|url] [- extra]     — tailored resume")
    print("  apply [#|url] [- extra]      — cover letter + tailored resume")
    print("  salary [#|url]               — salary estimate")
    print("  answer [#]: <question>       — answer application question")
    print("  aq: <question>               — answer question (no job context)")
    print("  list jobs                    — show numbered job list")
    print()
    print("  When a session job is set, omit #/url and it's used automatically.")
    print("  Extra context after ' - ' is passed to the model, e.g.:")
    print("    cl - I found this via HN and have used their product for years\n")

    while True:
        try:
            label = f" [{session['label']}]" if session else ""
            user_input = input(f"\n🌱{label} You: ").strip()

            if user_input.lower() in ["exit", "quit", "q"]:
                print("\nGoodbye! Good luck with your climate tech job search! 🌍")
                break

            if not user_input:
                continue

            # --- detect pasted multi-line content ---
            # When the user pastes a job description, input() reads only the first line;
            # the rest sits in stdin's buffer. select() with timeout=0 detects this immediately.
            extra_pasted = []
            try:
                while select.select([sys.stdin], [], [], 0)[0]:
                    line = sys.stdin.readline()
                    if not line:
                        break
                    extra_pasted.append(line.rstrip("\r\n"))
            except Exception:
                pass

            if extra_pasted:
                if extra_pasted[-1].strip().upper() == "END":
                    extra_pasted.pop()
                full_pasted = (user_input + "\n" + "\n".join(extra_pasted)).strip()
                company, title = extract_job_meta(full_pasted)
                session = {
                    "job": None,
                    "role_desc": full_pasted,
                    "url": "",
                    "label": f"{company} - {title}",
                }
                print(f"\n   Detected pasted job description ({len(full_pasted.split())} words).")
                print(f"   Session set: {session['label']}")
                print("   Use cl / resume / apply to generate documents.")
                continue

            # --- set job / unset job ---
            set_match = _re.match(r"^set\s+job\s*(.*)", user_input, _re.IGNORECASE)
            if set_match:
                target = set_match.group(1).strip()
                if target:
                    _set_session(target)
                else:
                    print("   Usage: set job <job#>  |  set job <url>")
                continue

            if _re.match(r"^unset\s+job$", user_input, _re.IGNORECASE):
                session = None
                print("   Session job cleared.")
                continue

            if _re.match(r"^paste\s+job$", user_input, _re.IGNORECASE):
                print("   Paste the job description below.")
                print("   Type END on its own line when done (or Ctrl+D):\n")
                lines = []
                while True:
                    try:
                        line = input()
                        if line.strip() == "END":
                            break
                        lines.append(line)
                    except EOFError:
                        break
                pasted = "\n".join(lines).strip()
                if not pasted:
                    print("   Nothing pasted.")
                else:
                    company, title = extract_job_meta(pasted)
                    session = {
                        "job": None,
                        "role_desc": pasted,
                        "url": "",
                        "label": f"{company} - {title}",
                    }
                    print(f"   Session job set: {session['label']}")
                    print("   Commands without a job# or URL will now use this job.")
                continue

            # --- session-aware bare commands (no job#/url required when session is set) ---
            # Syntax: <cmd> [--tailor] [- extra context]
            # e.g.  "cl", "cl - mention I found this via HN", "apply --tailor - focus on Python"
            def _parse_extra(text):
                """Extract optional '- extra context' suffix, return extra string (may be empty)."""
                m = _re.search(r"\s+-\s+(.+)$", text)
                return m.group(1).strip() if m else ""

            bare_apply = _re.match(
                r"^apply(?:\s+-\s+.+)?\s*$", user_input, _re.IGNORECASE
            )
            bare_resume = _re.match(
                r"^(?:resume|tailor)(?:\s+-\s+.+)?\s*$", user_input, _re.IGNORECASE
            )
            bare_cl = _re.match(
                r"^(?:cl|cover\s*letter)(?:\s+-\s+.+)?\s*$", user_input, _re.IGNORECASE
            )
            bare_salary = _re.match(r"^(?:salary|sal)\s*$", user_input, _re.IGNORECASE)

            # First try the full parsers (these handle explicit job#/url)
            apply_cmd = _parse_action_command(user_input, ("apply",))
            if apply_cmd:
                _handle_apply_command(apply_cmd)
                continue

            resume_cmd = _parse_action_command(user_input, ("resume", "tailor"))
            if resume_cmd:
                _handle_resume_command(resume_cmd)
                continue

            # Then fall back to session job for bare commands.
            # Use session data directly — avoids re-fetching and works for both
            # job# and URL sessions.
            if bare_apply:
                if not session:
                    print("   No session job set. Use: apply <job#|url>  or  set job <job#|url>")
                else:
                    extra = _parse_extra(user_input)
                    generate_cover_letter(
                        session["job"], extra_context=extra, url=session["url"],
                        description=session["role_desc"] if not session["url"] else "",
                    )
                    tailor_resume(session["role_desc"], extra_context=extra)
                continue

            if bare_resume:
                if not session:
                    print("   No session job set. Use: resume <job#|url>  or  set job <job#|url>")
                else:
                    tailor_resume(session["role_desc"], extra_context=_parse_extra(user_input))
                continue

            if bare_cl:
                if not session:
                    print("   No session job set. Use: cl <job#|url>  or  set job <job#|url>")
                else:
                    generate_cover_letter(
                        session["job"], extra_context=_parse_extra(user_input),
                        url=session["url"],
                        description=session["role_desc"] if not session["url"] else "",
                    )
                continue

            if bare_salary:
                if not session:
                    print("   No session job set. Use: salary <job#|url>  or  set job <job#|url>")
                else:
                    estimate_salary(
                        job=session["job"], url=session["url"],
                        description=session["role_desc"] if not session["url"] else "",
                    )
                continue

            # --- cover letter / answer / salary / list (via parse_command) ---
            cmd = parse_command(user_input)
            if cmd:
                if cmd["action"] == "list":
                    print(list_jobs_brief())

                elif cmd["action"] == "generate":
                    job, url = _session_job_or(
                        get_job_by_number(cmd["job_number"]) if cmd.get("job_number") else None,
                        cmd.get("url"),
                    )
                    if cmd.get("job_number") and not job:
                        print(f"   Job #{cmd['job_number']} not found.")
                        continue
                    if not job and not url and not session:
                        print("   No session job set. Use: cl <job#|url>  or  set job <job#|url>")
                        continue
                    generate_cover_letter(job, extra_context=cmd.get("extra_context", ""), url=url)

                elif cmd["action"] == "answer":
                    job, url = _session_job_or(
                        get_job_by_number(cmd["job_number"]) if cmd.get("job_number") else None,
                        cmd.get("url"),
                    )
                    if cmd.get("job_number") and not job:
                        print(f"   Job #{cmd['job_number']} not found.")
                        continue
                    answer_application_question(cmd["question"], job=job, url=url)

                elif cmd["action"] == "salary":
                    job, url = _session_job_or(
                        get_job_by_number(cmd["job_number"]) if cmd.get("job_number") else None,
                        cmd.get("url"),
                    )
                    if cmd.get("job_number") and not job:
                        print(f"   Job #{cmd['job_number']} not found.")
                        continue
                    if not job and not url and not session:
                        print("   No session job set. Use: salary <job#|url>  or  set job <job#|url>")
                        continue
                    estimate_salary(job=job, url=url)
                continue

            # Otherwise pass to the OpenAI agent
            response = root_agent.run(user_input)
            print(f"\n🤖 Agent: {response}")

        except KeyboardInterrupt:
            print("\n\nGoodbye! Good luck with your climate tech job search! 🌍")
            break
        except EOFError:
            break


_HELP = """\
Climate & Green Tech Job Finder

Usage:
  jobfinder "Job Title"               search for jobs
  jobfinder [options] JOB_NUM|'URL'   generate documents for a job

Search:
  job_titles                    comma-separated titles, e.g. "PM,Engineer"
  -l / --location  LOCATION     filter by location
  -t / --job-type  TYPE         full-time | part-time | contract | remote
  -e / --experience LEVEL       entry | mid | senior
  -i / --interactive            enter interactive mode (after search, or alone)

Interactive mode commands (type after entering -i):
  set job <#|url>   set active job for the session — all commands default to it
  paste job         paste a job description directly (type END when done)
  unset job         clear active job
  cl [#|url]        cover letter      resume [#|url]   tailored resume
  apply [#|url]     cover letter + resume (add --tailor to tailor the resume)
  salary [#|url]    salary estimate
  answer [#]: <q>   answer application question (uses session job if no # given)
  aq: <q>           answer question without job context
  list jobs         show numbered job list

Application:
  -c / --cover-letter  JOB|URL  generate a cover letter
  -r / --resume        JOB|URL  generate tailored resume
  -s / --salary        JOB|URL  estimate salary
  -a / --apply         JOB|URL  cover letter + tailored resume
  -x / --extra  'TEXT'          extra context / tailoring notes for -c / -r / -s / -a

Utilities:
  -p / --to-pdf  [DOCX_PATH]    convert docx → pdf (omit path for last modified in applications/)
  -h / --help                   show this help

Notes:
  • Always quote URLs with single quotes (URLs often contain & which bash splits on):
      jobfinder -a 'https://example.com/job?ref=x&utm=y'
  • Extra context is a separate flag, not a positional argument:
      jobfinder -a 'https://example.com/job' -x 'I found this via HN'

Examples:
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
    parser.add_argument("-t", "--job-type", default="",
                        choices=["", "full-time", "part-time", "contract", "remote"])
    parser.add_argument("-e", "--experience", default="",
                        choices=["", "entry", "mid", "senior"])
    parser.add_argument("-i", "--interactive", action="store_true")
    parser.add_argument("-c", "--cover-letter", "--cl",
                        metavar="JOB|URL", default=None, dest="cover_letter")
    parser.add_argument("-r", "--resume", "--tailor",
                        metavar="JOB|URL", default=None)
    parser.add_argument("-s", "--salary",
                        metavar="JOB|URL", default=None)
    parser.add_argument("-a", "--apply",
                        metavar="JOB|URL", default=None)
    parser.add_argument("-x", "--extra", metavar="TEXT", default="")
    parser.add_argument("-p", "--to-pdf", metavar="DOCX_PATH",
                        nargs="?", const="", default=None)
    parser.add_argument("-h", "--help", action="store_true", default=False)

    args = parser.parse_args()

    if args.help:
        print(_HELP)
        return
    
    # --- Docx to PDF conversion ---
    if args.to_pdf is not None:
        from resume_tailor import docx_to_pdf
        path = args.to_pdf
        if not path:
            # No path given — find last modified docx in applications/
            apps_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "applications")
            docx_files = [
                os.path.join(apps_dir, f)
                for f in os.listdir(apps_dir)
                if f.endswith(".docx")
            ] if os.path.isdir(apps_dir) else []
            if not docx_files:
                print("Error: no .docx files found in applications/")
                return
            path = max(docx_files, key=os.path.getmtime)
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
        "apply" if args.apply
        else "resume" if args.resume
        else "salary" if args.salary
        else "cover-letter" if args.cover_letter
        else None
    )

    if standalone_mode:
        # Detect the common mistake of passing extra text as a positional arg instead of -x
        if args.job_titles:
            flag = {"apply": "-a", "resume": "-r", "salary": "-s", "cover-letter": "-c"}[standalone_mode]
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
            print(f"Invalid value for -{standalone_mode[0]}: expected a job number or URL, got '{val}'")
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
    print(f"Results will be saved/appended to: jobs.csv")
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
