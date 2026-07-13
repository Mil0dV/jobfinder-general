"""Terminal UI for the Climate Job Finder.

Flow: menu (profile / role search, location picker with history) → animated
search with live progress → tinder-style card deck (←/→ decide, timed undo,
expandable details) → hand the liked jobs to the interactive shell.

Hand-rolled ANSI/termios, no extra dependencies. Decisions persist in
job_decisions.json so every job only has to be judged once; previously used
locations persist in .jobfinder_tui.json.
"""

import json
import os
import select
import subprocess
import sys
import termios
import textwrap
import threading
import time
import tty
from contextlib import contextmanager

from agent import PROFILE_QUERY, get_company_info, save_jobs, search_jobs
from candidate_profile import is_pearl

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, ".jobfinder_tui.json")
DECISIONS_FILE = os.path.join(BASE_DIR, "job_decisions.json")

UNDO_SECONDS = 5.0
MAX_SAVED_LOCATIONS = 8

# --- ANSI ---------------------------------------------------------------------

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
GREEN = "\x1b[32m"
RED = "\x1b[31m"
CYAN = "\x1b[36m"
YELLOW = "\x1b[33m"
MAGENTA = "\x1b[35m"

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
GLOBES = "🌍🌎🌏"
PLANTS = ["🌱", "🌿", "🪴", "🌳"]


def _visible_len(s: str) -> int:
    """Length without ANSI escapes (emoji width imprecision is tolerated)."""
    out, i = 0, 0
    while i < len(s):
        if s[i] == "\x1b":
            while i < len(s) and s[i] != "m":
                i += 1
            i += 1
        else:
            out += 1
            i += 1
    return out


# --- persistence ----------------------------------------------------------------


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def norm_url(u: str) -> str:
    return (u or "").split("?")[0].strip().rstrip("/")


def load_decisions() -> dict:
    return _load_json(DECISIONS_FILE, {})


def _push_decision(key, record):
    """Best-effort cross-device push of one swipe (record=None on undo)."""
    try:
        from sync import push_decision_async

        push_decision_async(key, record)
    except Exception:
        pass


def save_decisions(decisions: dict):
    _save_json(DECISIONS_FILE, decisions)


# --- terminal ---------------------------------------------------------------------


class Term:
    """Raw-ish terminal on the alternate screen; library prints go to devnull."""

    KEYMAP = {"[A": "UP", "[B": "DOWN", "[C": "RIGHT", "[D": "LEFT", "OA": "UP", "OB": "DOWN"}

    def __enter__(self):
        self.fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(self.fd)
        self.out = sys.stdout
        self._devnull = open(os.devnull, "w")
        sys.stdout = self._devnull  # swallow pipeline prints while the TUI owns the screen
        tty.setcbreak(self.fd)
        self.out.write("\x1b[?1049h\x1b[?25l")  # alt screen, hide cursor
        self.out.flush()
        return self

    def __exit__(self, *exc):
        self.out.write("\x1b[?25h\x1b[?1049l")  # show cursor, leave alt screen
        self.out.flush()
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        sys.stdout = self.out
        self._devnull.close()
        return False

    @contextmanager
    def suspend(self):
        """Temporarily hand the normal terminal back (for the interactive shell)."""
        self.out.write("\x1b[?25h\x1b[?1049l")  # show cursor, leave alt screen
        self.out.flush()
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        sys.stdout = self.out
        try:
            yield
        finally:
            sys.stdout = self._devnull
            tty.setcbreak(self.fd)
            self.out.write("\x1b[?1049h\x1b[?25l")
            self.out.flush()

    def size(self):
        try:
            s = os.get_terminal_size()
            return (s.columns or 80), (s.lines or 24)
        except OSError:
            return 80, 24

    def read_key(self, timeout=None):
        """One key: 'UP'/'DOWN'/'LEFT'/'RIGHT'/'ENTER'/'ESC'/'BACKSPACE' or a char."""
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return None
        ch = os.read(self.fd, 1).decode("utf-8", "ignore")
        if ch == "\x1b":
            seq = ""
            while len(seq) < 2 and select.select([self.fd], [], [], 0.02)[0]:
                seq += os.read(self.fd, 1).decode("utf-8", "ignore")
            return self.KEYMAP.get(seq, "ESC")
        if ch in ("\r", "\n"):
            return "ENTER"
        if ch in ("\x7f", "\x08"):
            return "BACKSPACE"
        if ch == "\x03":
            raise KeyboardInterrupt
        return ch

    def render(self, lines):
        cols, rows = self.size()
        buf = ["\x1b[H"]
        for line in lines[: rows - 1]:
            buf.append(line[: cols + (len(line) - _visible_len(line))] + "\x1b[K\n")
        buf.append("\x1b[J")
        self.out.write("".join(buf))
        self.out.flush()


def _center(s, cols):
    pad = max(0, (cols - _visible_len(s)) // 2)
    return " " * pad + s


# --- splash screen --------------------------------------------------------------


def _splash_lines():
    """The startup card: a big-hatted job witch holding up her verdict on a notepad."""
    D, G, B, C, R = DIM, GREEN, BOLD, CYAN, RESET
    pad = [  # left column: the spiral notepad (plain, text bold)
        "      /\\ /\\ /\\ /\\ /\\ /\\  ",
        "     (  (  (  (  (  (  ) ",
        "    ┌────────────────────┐",
        "    │                    │",
        f"    │   {B}You are a{R}        │",
        "    │                    │",
        f"    │   {B}climate-{R}         │",
        "    │                    │",
        f"    │   {B}job-{R}             │",
        "    │                    ├",
        f"    │   {B}seeker{R}           │",
        "    │                    │",
        "    └────────────────────┘",
    ]
    return [
        f" {C}▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄{R}",
        f" {C}█ JOBFINDER ███████{R}                                  {D}~~~~~~~{R}",
        f" {C}█ CLIMATE TUESDAY █{R}          {D}~~~~~~~~{R}                       {D}~~~~{R}",
        f" {C}▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀{R}                        {D}▄▄▄▄▄▄▄▄▄{R}",
        f"                                       {D}▄▄███████████████▄▄{R}",
        f"                                    {D}▄███████████████████████▄{R}",
        f"                                 {D}▄██████████████████████████████▄{R}",
        f"                             {D}▄▄███████████████████████████████████▄▄{R}",
        f"{pad[0]} {D}▀▀▀▀▀▀▀▀▀▀▀███████████████▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀{R}",
        f"{pad[1]}             {D}███{G}█████████{D}███{R}",
        f"{pad[2]}          {D}██{G}█ ▄▄▄   ▄▄▄ █{D}██{R}",
        f"{pad[3]}          {D}██{G}█ ▀▄▀   ▀▄▀ █{D}██{R}",
        f"{pad[4]}          {D}██{G}█           █{D}██{R}",
        f"{pad[5]}           {D}█{G}██  ▄███▄  ██{D}█{R}",
        f"{pad[6]}            {G}▀██▄ ▀▀▀ ▄██▀{R}",
        f"{pad[7]}   {G}▄██▄      ▀███████▀{R}",
        f"{pad[8]}  {G}██▀▀██▄▄▄   {D}███████{R}",
        f"{pad[9]}{G}▄▄██     ▀▀█▄▄{D}██▀   ▀██{R}",
        f"{pad[10]} {G}▀▀{R}           {D}██     ██{R}",
        f"{pad[11]}              {D}██     ██{R}",
        f"{pad[12]}             {D}███     ███{R}",
    ]


def splash_screen(term):
    """Show the startup card until a key is pressed. Skipped on tiny terminals."""
    cols, rows = term.size()
    art = _splash_lines()
    if cols < 72 or rows < len(art) + 4:
        return
    left = max(0, (cols - 70) // 2)
    lines = [""] + [" " * left + line for line in art]
    lines += ["", _center(f"{DIM}— press any key —{RESET}", cols)]
    term.render(lines)
    term.read_key()


# --- generic widgets ----------------------------------------------------------------


def menu_select(term, title, items, footer="↑/↓ move · Enter select · q back", selected=0):
    """Arrow-key menu. Returns the chosen index, or None on q/ESC."""
    while True:
        cols, _ = term.size()
        lines = ["", _center(f"{BOLD}{title}{RESET}", cols), ""]
        for i, item in enumerate(items):
            marker = f"{GREEN}▸ " if i == selected else "  "
            style = BOLD if i == selected else DIM
            lines.append(
                _center(f"{marker}{style}{item}{RESET}" + ("  " if i != selected else ""), cols)
            )
        lines += ["", _center(f"{DIM}{footer}{RESET}", cols)]
        term.render(lines)
        key = term.read_key()
        if key == "UP":
            selected = (selected - 1) % len(items)
        elif key == "DOWN":
            selected = (selected + 1) % len(items)
        elif key == "ENTER":
            return selected
        elif key in ("q", "ESC"):
            return None


def text_input(term, title, prompt, initial=""):
    """Single-line text entry. Returns the string, or None on ESC."""
    value = initial
    while True:
        cols, _ = term.size()
        lines = [
            "",
            _center(f"{BOLD}{title}{RESET}", cols),
            "",
            _center(f"{prompt}: {value}{CYAN}▌{RESET}", cols),
            "",
            _center(f"{DIM}Enter confirm · Esc cancel{RESET}", cols),
        ]
        term.render(lines)
        key = term.read_key()
        if key == "ENTER":
            return value.strip()
        if key == "ESC":
            return None
        if key == "BACKSPACE":
            value = value[:-1]
        elif key and len(key) == 1 and key.isprintable():
            value += key


def pick_location(term, state) -> str | None:
    """Location via history selection or free text. '' = no filter, None = cancel."""
    previous = state.get("locations", [])
    items = previous + ["(no location filter)", "(type a new location…)"]
    choice = menu_select(term, "Where should the search look?", items)
    if choice is None:
        return None
    if choice < len(previous):
        return previous[choice]
    if items[choice].startswith("(no location"):
        return ""
    typed = text_input(term, "Search location", "Location")
    return typed if typed else None


def remember_location(state, location):
    if not location:
        return
    locations = [location] + [x for x in state.get("locations", []) if x != location]
    state["locations"] = locations[:MAX_SAVED_LOCATIONS]
    _save_json(STATE_FILE, state)


# --- search screen -------------------------------------------------------------------

_PHASE_LABELS = {
    "sources": "Querying job boards",
    "verify": "Checking older postings are still online",
    "descriptions": "Fetching job descriptions",
    "validate": "Scoring with Claude",
    "done": "Done",
}


def _overall_percent(phase, done, total):
    total = max(total, 1)
    if phase == "sources":
        return 2 + int(done / total * 38)
    if phase == "verify":
        return 40 + int(done / total * 10)
    if phase == "descriptions":
        return 50 + int(done / total * 25)
    if phase == "validate":
        return 75 + int(done / total * 23)
    if phase == "done":
        return 100
    return 1


def search_screen(term, query, location):
    """Run search_jobs in a thread with an animated progress view.

    Returns the result jobs (with CSV row numbers attached), or None on cancel.
    """
    progress = {"phase": "starting", "done": 0, "total": 1, "detail": "", "sources": {}}
    holder = {}

    def cb(phase, done, total, detail):
        progress.update(phase=phase, done=done, total=total, detail=detail)
        if phase == "sources" and ":" in detail:
            name, count = detail.rsplit(":", 1)
            progress["sources"][name] = count

    def worker():
        try:
            result = search_jobs(query=query, location=location, _progress=cb)
            jobs = result["jobs"]
            save_jobs(jobs)
            _attach_csv_numbers(jobs)
            holder["jobs"] = jobs
            holder["offline_dropped"] = result.get("offline_dropped", 0)
        except Exception as e:
            holder["error"] = str(e)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    label = "your target-role profile" if query == PROFILE_QUERY else f"“{query}”"
    where = f" in {location}" if location else ""
    frame = 0
    while thread.is_alive():
        cols, _ = term.size()
        pct = _overall_percent(progress["phase"], progress["done"], progress["total"])
        bar_w = min(46, cols - 20)
        filled = int(bar_w * pct / 100)
        plant = PLANTS[min(len(PLANTS) - 1, pct * len(PLANTS) // 101)]
        bar = f"{GREEN}{'█' * filled}{RESET}{DIM}{'░' * (bar_w - filled)}{RESET}"
        spin = SPINNER[frame % len(SPINNER)]
        globe = GLOBES[(frame // 3) % len(GLOBES)]

        src_bits = []
        for fn in (
            "arbeitnow",
            "remoteok",
            "adzuna",
            "greenhouse",
            "lever",
            "climatetechlist",
            "climatebase",
            "jobsincarbon",
            "remoteimpact",
        ):
            if fn in progress["sources"]:
                count = progress["sources"][fn]
                mark = f"{RED}✗{RESET}" if count == "error" else f"{GREEN}✓{RESET}"
                src_bits.append(f"{mark} {fn}{DIM}({count}){RESET}")
            else:
                src_bits.append(f"{CYAN}{spin}{RESET} {DIM}{fn}{RESET}")
        src_lines = [
            _center("   ".join(src_bits[i : i + 3]), cols) for i in range(0, len(src_bits), 3)
        ]

        phase_label = _PHASE_LABELS.get(progress["phase"], "Warming up")
        detail = progress["detail"] if progress["phase"] != "sources" else ""
        counter = (
            f' {DIM}({progress["done"]}/{progress["total"]}){RESET}'
            if progress["phase"] in ("verify", "descriptions", "validate")
            else ""
        )
        lines = [
            "",
            _center(f"{globe}  {BOLD}Searching{RESET} for {label}{where}", cols),
            "",
            _center(f"{plant} {bar} {pct:3d}%", cols),
            "",
            *src_lines,
            "",
            _center(f"{CYAN}{spin}{RESET} {phase_label}{counter} {DIM}{detail}{RESET}", cols),
            "",
            _center(f"{DIM}q cancel{RESET}", cols),
        ]
        term.render(lines)
        frame += 1
        key = term.read_key(0.09)
        if key in ("q", "ESC"):
            return None

    if "error" in holder:
        term.render(
            [
                "",
                _center(f"{RED}Search failed:{RESET} {holder['error']}", term.size()[0]),
                "",
                _center(f"{DIM}press any key{RESET}", term.size()[0]),
            ]
        )
        term.read_key()
        return None
    return holder.get("jobs", [])


def _attach_csv_numbers(jobs):
    """Attach jobs.csv row numbers so the interactive shell's `cl <n>` etc. line up."""
    try:
        from cover_letter import load_jobs_from_csv

        num_by_url = {norm_url(row.get("url")): i for i, row in enumerate(load_jobs_from_csv(), 1)}
        for job in jobs:
            job["_num"] = num_by_url.get(norm_url(job.get("url")))
    except Exception:
        pass


# --- card deck -------------------------------------------------------------------------


def _stars(value) -> str:
    try:
        n = max(0, min(5, int(value)))
    except (TypeError, ValueError):
        return f"{DIM}n/a{RESET}"
    return f"{YELLOW}{'★' * n}{DIM}{'☆' * (5 - n)}{RESET}"


def _lazy_fetch(job, kind):
    """Fetch full description / company info in the background, cache on the job."""
    key = "_desc" if kind == "desc" else "_company"
    if key in job or job.get(f"{key}_loading"):
        return
    job[f"{key}_loading"] = True

    def work():
        try:
            if kind == "desc":
                text = ""
                if job.get("url"):
                    from cover_letter import fetch_job_description

                    text = fetch_job_description(job["url"]) or ""
                job[key] = text or job.get("snippet") or "(no description available)"
            else:
                info = get_company_info(job.get("company", ""))
                job[key] = (
                    info.get("about_snippets", "") + "\n\n" + info.get("wfh_snippets", "")
                ).strip() or "(nothing found)"
        except Exception as e:
            job[key] = f"(lookup failed: {e})"
        finally:
            job.pop(f"{key}_loading", None)

    threading.Thread(target=work, daemon=True).start()


def _card_lines(term, job, idx, total, expanded, scroll, undo_left, counts):
    cols, rows = term.size()
    w = min(cols - 4, 84)
    rule = f"{DIM}{'─' * w}{RESET}"

    def wrap(text, prefix="  "):
        return [prefix + line for line in textwrap.wrap(text, w - len(prefix) * 2) or [""]]

    liked, passed = counts
    header = (
        f"{DIM}job {idx + 1}/{total}   "
        f"{GREEN}♥ {liked}{RESET}{DIM} liked · {RED}✗ {passed}{RESET}{DIM} passed{RESET}"
    )
    num = f"  {DIM}#{job['_num']} in jobs.csv{RESET}" if job.get("_num") else ""
    lines = ["", "  " + header, "", rule]
    if is_pearl(job):
        lines += wrap(f"{MAGENTA}{BOLD}🦪 PEARL — supermatch{RESET}")
    lines += wrap(f"{BOLD}{job.get('title', '?')}{RESET}")
    lines += wrap(f"{CYAN}{job.get('company', '?')}{RESET}{num}")
    lines.append(rule)
    lines += wrap(f"📍 {job.get('location') or job.get('wfh_policy') or '?'}")
    salary = job.get("salary", "")
    if salary and salary != "Not listed":
        lines += wrap(f"💰 {salary}")
    posted = job.get("posted_date", "")
    bits = []
    if posted:
        bits.append(f"posted {posted}")
    if job.get("wfh_policy") and job.get("wfh_policy") != "Unknown":
        bits.append(job["wfh_policy"])
    if job.get("source"):
        bits.append(f"via {job['source']}")
    if bits:
        lines += wrap(f"{DIM}{' · '.join(bits)}{RESET}")
    lines += wrap(
        f"fit {_stars(job.get('fit_score'))}   startup {_stars(job.get('startup_score'))}"
        f"   climate {_stars(job.get('climate_score'))}"
    )
    if job.get("reason"):
        lines += wrap(f"{DIM}{job['reason']}{RESET}")
    lines.append(rule)

    if expanded:
        key = "_desc" if expanded == "desc" else "_company"
        title = "Role description" if expanded == "desc" else "Company"
        body = job.get(key)
        if body is None:
            body = "loading…"
        body_lines = []
        for para in body.splitlines():
            body_lines += textwrap.wrap(para, w - 4) or [""]
        room = max(4, rows - len(lines) - 6)
        scroll = max(0, min(scroll, max(0, len(body_lines) - room)))
        shown = body_lines[scroll : scroll + room]
        more = (
            f" {DIM}(↑/↓ scroll, {scroll + len(shown)}/{len(body_lines)}){RESET}"
            if len(body_lines) > room
            else ""
        )
        lines.append(f"  {BOLD}{title}{RESET}{more}")
        lines += ["  " + line for line in shown]
        lines.append(rule)
    else:
        # Preview of the role description — as many lines as the screen has room for.
        room = rows - len(lines) - 7  # hint + rule + blank + footer + undo + margin
        if room >= 3:
            desc = job.get("_desc") or job.get("snippet") or ""
            if desc:
                body_lines = []
                for para in desc.splitlines():
                    body_lines += textwrap.wrap(para, w - 4) or [""]
                while body_lines and not body_lines[0].strip():
                    body_lines.pop(0)
                shown = body_lines[:room]
                lines += ["  " + line for line in shown]
                if len(body_lines) > len(shown):
                    lines.append(f"  {DIM}… [d] for the full description{RESET}")
            elif job.get("_desc_loading"):
                lines.append(f"  {DIM}loading description…{RESET}")
        lines.append(
            f"  {DIM}[d] full description · [c] company info · [o] open · [a] apply{RESET}"
        )
        lines.append(rule)

    footer = (
        f"{RED}← pass{RESET}   {GREEN}→ like{RESET}   "
        f"{DIM}a apply · d/c details · o open · q finish{RESET}"
    )
    lines += ["", "  " + footer]
    if undo_left is not None:
        lines.append(f"  {MAGENTA}↶ u to undo last choice ({undo_left:.0f}s){RESET}")
    return lines, scroll


def swipe_screen(term, deck, decisions):
    """Tinder-style deck (pearls first). Returns the jobs liked in this session."""
    deck = sorted(deck, key=lambda j: not is_pearl(j))  # stable: keeps order within groups
    if not deck:
        cols = term.size()[0]
        term.render(
            [
                "",
                _center("Nothing new to review — every job here is already decided. 🎉", cols),
                "",
                _center(f"{DIM}press any key{RESET}", cols),
            ]
        )
        term.read_key()
        return []

    i, liked = 0, []
    expanded, scroll = None, 0
    undo = None  # {"index", "key", "job", "deadline"}

    def decide(job, choice):
        nonlocal undo, i, expanded, scroll
        key = norm_url(job.get("url")) or f"{job.get('company')}|{job.get('title')}"
        decisions[key] = {
            "decision": choice,
            "title": job.get("title", ""),
            "company": job.get("company", ""),
            "ts": time.strftime("%Y-%m-%d %H:%M"),
        }
        save_decisions(decisions)
        _push_decision(key, decisions[key])
        if choice == "yes":
            liked.append(job)
        undo = {"index": i, "key": key, "job": job, "deadline": time.monotonic() + UNDO_SECONDS}
        i += 1
        expanded, scroll = None, 0

    while i < len(deck):
        job = deck[i]
        for upcoming in deck[i : i + 3]:  # warm the card previews (and the disk cache)
            if upcoming.get("url"):
                _lazy_fetch(upcoming, "desc")
        undo_left = None
        if undo:
            undo_left = undo["deadline"] - time.monotonic()
            if undo_left <= 0:
                undo, undo_left = None, None
        lines, scroll = _card_lines(
            term,
            job,
            i,
            len(deck),
            expanded,
            scroll,
            undo_left,
            (len(liked), i - len(liked)),
        )
        term.render(lines)

        key = term.read_key(0.15)
        if key is None:
            continue
        if key == "RIGHT":
            decide(job, "yes")
        elif key == "LEFT":
            decide(job, "no")
        elif key == "u" and undo:
            decisions.pop(undo["key"], None)
            save_decisions(decisions)
            _push_decision(undo["key"], None)
            if undo["job"] in liked:
                liked.remove(undo["job"])
            i = undo["index"]
            undo, expanded, scroll = None, None, 0
        elif key == "d":
            expanded, scroll = (None, 0) if expanded == "desc" else ("desc", 0)
            if expanded:
                _lazy_fetch(job, "desc")
        elif key == "c":
            expanded, scroll = (None, 0) if expanded == "company" else ("company", 0)
            if expanded:
                _lazy_fetch(job, "company")
        elif key == "UP" and expanded:
            scroll = max(0, scroll - 3)
        elif key == "DOWN" and expanded:
            scroll += 3
        elif key == "o" and job.get("url"):
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen(
                [opener, job["url"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        elif key == "a":
            with term.suspend():
                from cli import _run_interactive

                try:
                    _run_interactive(initial_job=job)
                except (KeyboardInterrupt, EOFError):
                    pass
            # back to the deck, same card
        elif key in ("q", "ESC"):
            break
    return liked


def summary_screen(term, liked):
    """Show the liked jobs. Returns True to continue into interactive mode."""
    cols, _ = term.size()
    lines = ["", _center(f"{BOLD}Review done{RESET}", cols), ""]
    if liked:
        lines.append(_center(f"{GREEN}♥ {len(liked)} job(s) liked:{RESET}", cols))
        lines.append("")
        for job in liked[:15]:
            num = f"#{job['_num']} " if job.get("_num") else ""
            pearl = f"{MAGENTA}🦪 {RESET}" if is_pearl(job) else ""
            lines.append(
                _center(
                    f"{pearl}{DIM}{num}{RESET}{job.get('title', '?')} — {CYAN}{job.get('company', '?')}{RESET}",
                    cols,
                )
            )
        if len(liked) > 15:
            lines.append(_center(f"{DIM}…and {len(liked) - 15} more{RESET}", cols))
        lines += [
            "",
            _center(
                f"{DIM}Enter: write applications in interactive mode (/next steps through them){RESET}",
                cols,
            ),
            _center(f"{DIM}q: quit{RESET}", cols),
        ]
    else:
        lines += [
            _center(f"{DIM}No jobs liked this round.{RESET}", cols),
            "",
            _center(f"{DIM}Enter: back to menu · q: quit{RESET}", cols),
        ]
    term.render(lines)
    while True:
        key = term.read_key()
        if key == "ENTER":
            return True
        if key in ("q", "ESC"):
            return False


# --- review flow (saved jobs from jobs.csv) ---------------------------------------------


def _undecided_from_csv(decisions):
    """Deck of undecided saved jobs from jobs.csv.

    Liveness is checked during search (verify_jobs_online in search_jobs),
    so the review deck opens instantly.
    """
    try:
        from cover_letter import load_jobs_from_csv

        rows = load_jobs_from_csv()
    except Exception:
        rows = []
    deck = []
    for n, row in enumerate(rows, 1):
        key = norm_url(row.get("url")) or f"{row.get('company')}|{row.get('title')}"
        if key in decisions:
            continue
        row["_num"] = n
        deck.append(row)
    return deck


# --- main -------------------------------------------------------------------------------


def run_tui():
    """Run the TUI. Returns the list of liked jobs (enter interactive mode), or None."""
    try:
        from sync import sync_down

        sync_down()  # pull the other devices' jobs + swipes before dealing the deck
    except Exception:
        pass
    state = _load_json(STATE_FILE, {})
    decisions = load_decisions()

    with Term() as term:
        splash_screen(term)
        while True:
            choice = menu_select(
                term,
                "🌱 Climate & Green Tech Job Finder",
                [
                    "Search my target-role profile",
                    "Search a specific role",
                    "Review undecided saved jobs",
                    "Quit",
                ],
                footer="↑/↓ move · Enter select · q quit",
            )
            if choice in (None, 3):
                return None

            if choice == 2:
                deck = _undecided_from_csv(decisions)
                liked = swipe_screen(term, deck, decisions)
                proceed = summary_screen(term, liked)
                if liked and proceed:
                    return liked
                if not proceed:
                    return None
                continue  # Enter with nothing liked → back to the menu

            query = PROFILE_QUERY
            if choice == 1:
                role = text_input(term, "Role search", "Job title")
                if not role:
                    continue
                query = role
            location = pick_location(term, state)
            if location is None:
                continue
            remember_location(state, location)

            jobs = search_screen(term, query, location)
            if jobs is None:
                continue
            deck = [
                j
                for j in jobs
                if (norm_url(j.get("url")) or f"{j.get('company')}|{j.get('title')}")
                not in decisions
            ]
            liked = swipe_screen(term, deck, decisions)
            proceed = summary_screen(term, liked)
            if liked and proceed:
                return liked
            if not proceed:
                return None
            # Enter with nothing liked → back to the menu


if __name__ == "__main__":
    liked_jobs = run_tui()
    if liked_jobs:
        print(f"Liked {len(liked_jobs)} job(s):")
        for j in liked_jobs:
            print(f"  - {j.get('title')} — {j.get('company')}  {j.get('url')}")
