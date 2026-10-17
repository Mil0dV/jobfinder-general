"""Resume tailoring: minimal edits to match a role description, output as PDF.

Uses Claude to suggest minimal, natural-sounding edits to the resume text,
then rebuilds the docx with the original styling and converts to PDF.
"""

import copy
import json
import os
import re
import subprocess
import sys
from io import BytesIO

import trafilatura
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from dotenv import load_dotenv

import jobfinder.llm.client as llm
from jobfinder.config.paths import PROJECT_ROOT
from jobfinder.documents.cover_letter import extract_job_meta, make_output_stem
from jobfinder.documents.resume_data import RESUME_DOCX_PATH, get_resume_data

load_dotenv()

_project_root = str(PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Anthropic client
# ---------------------------------------------------------------------------

_client = None


def _get_client():
    global _client
    if _client is None:
        if not llm.api_key():
            print(f"Error: {llm.api_key_env()} not set.")
            sys.exit(1)
        _client = llm.get_client()
    return _client


# ---------------------------------------------------------------------------
# Resume data
# ---------------------------------------------------------------------------
#
# Loading/derivation lives in resume_data: Resume_base.docx is the source of
# truth, parsed into resume.json (regenerated when the docx changes).
# get_base_resume() is kept as a thin alias for backwards compatibility.

get_base_resume = get_resume_data


# ---------------------------------------------------------------------------
# Docx builder — matches the PDF styling
# ---------------------------------------------------------------------------

# Colors from the original PDF
_NAME_COLOR = RGBColor(0x17, 0x36, 0x5D)
_HEADING_COLOR = RGBColor(0x36, 0x60, 0x91)
_SUBHEADING_COLOR = RGBColor(0x4F, 0x81, 0xBD)
_BLACK = RGBColor(0x00, 0x00, 0x00)


def _set_spacing(paragraph, before=0, after=0, line=None):
    """Set paragraph spacing in points."""
    pf = paragraph.paragraph_format
    pf.space_before = Pt(before)
    pf.space_after = Pt(after)
    if line is not None:
        pf.line_spacing = Pt(line)


def _add_horizontal_rule(doc):
    """Add a horizontal rule after the last paragraph."""
    p = doc.paragraphs[-1]
    pPr = p._element.get_or_add_pPr()
    pBdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "12")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "17365D")
    pBdr.append(bottom)
    pPr.append(pBdr)


def _add_heading(doc, text, level="section"):
    """Add a styled heading. level: 'section' (large blue) or 'sub' (smaller blue)."""
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.bold = True
    if level == "section":
        run.font.size = Pt(14)
        run.font.color.rgb = _HEADING_COLOR
        _set_spacing(p, before=12, after=4)
    else:
        run.font.size = Pt(11)
        run.font.color.rgb = _SUBHEADING_COLOR
        _set_spacing(p, before=6, after=2)
    run.font.name = "Calibri"
    return p


def _add_body(doc, text, size=11, italic=False, color=None):
    """Add a body text paragraph."""
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.font.size = Pt(size)
    run.font.name = "Calibri"
    run.font.color.rgb = color or _BLACK
    run.italic = italic
    _set_spacing(p, before=1, after=1, line=14)
    return p


def _add_bullet(doc, text, size=11):
    """Add a bullet point paragraph."""
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.font.size = Pt(size)
    run.font.name = "Calibri"
    run.font.color.rgb = _BLACK
    _set_spacing(p, before=1, after=1, line=14)
    # Bullet formatting
    pPr = p._element.get_or_add_pPr()
    numPr = OxmlElement("w:numPr")
    ilvl = OxmlElement("w:ilvl")
    ilvl.set(qn("w:val"), "0")
    numId = OxmlElement("w:numId")
    numId.set(qn("w:val"), "1")
    numPr.append(ilvl)
    numPr.append(numId)
    pPr.append(numPr)
    # Indent
    ind = OxmlElement("w:ind")
    ind.set(qn("w:left"), "720")
    ind.set(qn("w:hanging"), "360")
    pPr.append(ind)
    return p


def _ensure_bullet_numbering(doc):
    """Ensure the document has a bullet list numbering definition."""
    numbering_part = doc.part.numbering_part
    # Access the numbering XML
    numbering = numbering_part._element
    # Check if numId 1 already exists
    for num in numbering.findall(qn("w:num")):
        if num.get(qn("w:numId")) == "1":
            return
    # Add abstract numbering for bullets
    abstract = OxmlElement("w:abstractNum")
    abstract.set(qn("w:abstractNumId"), "0")
    lvl = OxmlElement("w:lvl")
    lvl.set(qn("w:ilvl"), "0")
    fmt = OxmlElement("w:numFmt")
    fmt.set(qn("w:val"), "bullet")
    lvl.append(fmt)
    lvl_text = OxmlElement("w:lvlText")
    lvl_text.set(qn("w:val"), "\u2022")
    lvl.append(lvl_text)
    lvl_jc = OxmlElement("w:lvlJc")
    lvl_jc.set(qn("w:val"), "left")
    lvl.append(lvl_jc)
    abstract.append(lvl)
    numbering.insert(0, abstract)

    num = OxmlElement("w:num")
    num.set(qn("w:numId"), "1")
    abstract_ref = OxmlElement("w:abstractNumId")
    abstract_ref.set(qn("w:val"), "0")
    num.append(abstract_ref)
    numbering.append(num)


def _copy_floating_images(src_path: str, dst_doc):
    """Copy floating (anchored) images — e.g. a photo — from src docx into dst_doc.

    The rebuilt resume is generated from text only, so any image in the source docx
    would otherwise be lost. We deep-copy each anchored drawing, re-register its image
    blob in the new package (fixing the relationship id), and align it flush to the
    right margin with text wrapping on the left for a clean single-column layout.
    """
    try:
        src = Document(src_path)
    except Exception:
        return 0
    if not dst_doc.paragraphs:
        return 0

    copied = 0
    for drawing in src.element.body.findall(".//" + qn("w:drawing")):
        if drawing.find(".//" + qn("wp:anchor")) is None:
            continue  # inline images have no place in the regenerated layout
        blip = drawing.find(".//" + qn("a:blip"))
        if blip is None:
            continue
        embed = blip.get(qn("r:embed"))
        if embed not in src.part.related_parts:
            continue
        rId, _ = dst_doc.part.get_or_add_image(BytesIO(src.part.related_parts[embed].blob))

        new_drawing = copy.deepcopy(drawing)
        new_drawing.find(".//" + qn("a:blip")).set(qn("r:embed"), rId)

        pos_h = new_drawing.find(".//" + qn("wp:positionH"))
        if pos_h is not None:
            pos_h.set("relativeFrom", "margin")
            for child in list(pos_h):
                pos_h.remove(child)
            align = OxmlElement("wp:align")
            align.text = "right"
            pos_h.append(align)
        wrap = new_drawing.find(".//" + qn("wp:wrapSquare"))
        if wrap is not None:
            wrap.set("wrapText", "left")

        dst_doc.paragraphs[0].add_run()._r.append(new_drawing)
        copied += 1
    return copied


def build_resume_docx(data: dict, output_path: str, photo_src: str | None = None):
    """Build a styled resume docx from structured data.

    If photo_src points to a docx containing floating image(s) (e.g. a photo), they
    are carried over into the generated resume.
    """
    doc = Document()

    # Page margins
    for section in doc.sections:
        section.top_margin = Inches(0.8)
        section.bottom_margin = Inches(0.6)
        section.left_margin = Inches(0.9)
        section.right_margin = Inches(0.9)

    # Name
    p = doc.add_paragraph()
    run = p.add_run(data["name"])
    run.font.size = Pt(26)
    run.font.color.rgb = _NAME_COLOR
    run.font.name = "Calibri"
    _set_spacing(p, before=0, after=2)

    # Horizontal rule
    _add_horizontal_rule(doc)

    # Contact
    _add_body(doc, data["contact"], size=10)

    # Professional Statement
    _add_heading(doc, "Professional Statement")
    _add_body(doc, data["professional_statement"])

    # Core Competencies
    _add_heading(doc, "Core Competencies")
    _add_body(doc, data["core_competencies"])

    # Technical Skills
    _add_heading(doc, "Technical Skills")
    _add_body(doc, data["technical_skills"])

    # Professional Experience
    _add_heading(doc, "Professional Experience")

    for exp in data["experiences"]:
        # Job title
        p = doc.add_paragraph()
        run = p.add_run(exp["title"])
        run.bold = True
        run.font.size = Pt(11)
        run.font.color.rgb = _SUBHEADING_COLOR
        run.font.name = "Calibri"
        _set_spacing(p, before=8, after=1)

        # Company description
        _add_body(doc, exp["subtitle"], size=10, italic=True)

        # Scope
        _add_heading(doc, "Scope & Responsibilities", level="sub")
        for bullet in exp["scope"]:
            _add_bullet(doc, bullet)

        # Achievements
        _add_heading(doc, "Key Achievements", level="sub")
        for bullet in exp["achievements"]:
            _add_bullet(doc, bullet)

    # Earlier Roles
    _add_heading(doc, "Earlier Roles (selected):", level="sub")
    for role in data["earlier_roles"]:
        _add_bullet(doc, role)

    # Education
    _add_heading(doc, "Education")
    for item in data["education"]:
        _add_bullet(doc, item)

    # Languages
    _add_heading(doc, "Languages")
    _add_body(doc, data["languages"])

    # Interests
    _add_heading(doc, "Interests")
    _add_body(doc, data["interests"])

    if photo_src and os.path.exists(photo_src):
        _copy_floating_images(photo_src, doc)

    doc.save(output_path)
    return output_path


# ---------------------------------------------------------------------------
# Claude-powered tailoring
# ---------------------------------------------------------------------------


def _load_style_guide() -> str:
    from jobfinder.documents.cover_letter import _load_style_guide as _cl_style_guide

    return _cl_style_guide()


TAILOR_SYSTEM_PROMPT = """\
You are a resume editor for {name}. Your job is to make MINIMAL, targeted edits \
to their resume so it better matches a specific role description.

RULES — follow these strictly:
1. Make as few changes as possible. Most sections should stay IDENTICAL.
2. NEVER fabricate experience, skills, or achievements. Only use what's already there.
3. NEVER add buzzwords, corporate-speak, or generic filler. Keep the candidate's direct, plain voice.
4. Allowed edits:
   - Reword the Professional Statement to emphasize relevant experience (keep same \
     length). Keep the candidate's phrasing where it already fits; favor moving, \
     dropping or adding a clause over rewriting whole sentences.
   - Reorder bullet points to put the most relevant ones first
   - Slightly rephrase a bullet to highlight a relevant aspect (keep factual)
   - Core Competencies: select ONLY the 6-8 most relevant items for this role and drop
     the rest, most-relevant first. Keep the same "•"-separated single-string format.
     Do not invent new competencies — only choose from those already listed.
   - Reorder items in Technical Skills to lead with the most relevant
   - Add a skill to Technical Skills ONLY if it's clearly implied by their experience
5. Do NOT touch: name, contact, dates, job titles, company names, education, languages, interests.
6. Do NOT lengthen the resume. If you add a word somewhere, remove one elsewhere.
7. Mirror the role's emphasis: lead with the right Core Competencies and Technical \
   Skills items, reorder bullets, and reword the Professional Statement where it \
   helps. Never stuff keywords the role did not ask for.
8. The result must read like a person typed it in one sitting. Several short plain \
   sentences beat one long stacked one: no semicolon chains, no em-dash interruptions, \
   no rule-of-three lists, no "not just X but Y", no grandiose verbs ("woven", \
   "spearheaded"), no self-descriptions that sound written ("fluent X and fluent Y"). \
   A human reviewer should not detect AI involvement.

STYLE REFERENCE (for tone):
{style_guide}

OUTPUT FORMAT:
Return ONLY a JSON object containing the fields you actually changed — omit \
unchanged fields entirely. When a change touches an experiences entry, include \
that entry COMPLETE (all subfields) with the identical title. If nothing should \
change, return {{}}. No commentary."""


# JSON schema enforced via output_config.format so the API guarantees the
# response is the resume structure itself (no prose preamble to parse around).
_STR_ARRAY = {"type": "array", "items": {"type": "string"}}
RESUME_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "contact": {"type": "string"},
        "professional_statement": {"type": "string"},
        "core_competencies": {"type": "string"},
        "technical_skills": {"type": "string"},
        "experiences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "subtitle": {"type": "string"},
                    "scope": _STR_ARRAY,
                    "achievements": _STR_ARRAY,
                },
                "required": ["title", "subtitle", "scope", "achievements"],
                "additionalProperties": False,
            },
        },
        "earlier_roles": _STR_ARRAY,
        "education": _STR_ARRAY,
        "languages": {"type": "string"},
        "interests": {"type": "string"},
    },
    "required": [
        "name",
        "contact",
        "professional_statement",
        "core_competencies",
        "technical_skills",
        "experiences",
        "earlier_roles",
        "education",
        "languages",
        "interests",
    ],
    "additionalProperties": False,
}


# Diff-mode output: each pass returns only the fields it actually edited.
# Echoing the unchanged resume back out costs thousands of generation tokens at
# the rig's ~47 t/s for zero information.
DIFF_SCHEMA = {
    "type": "object",
    "properties": {
        k: v for k, v in RESUME_SCHEMA["properties"].items() if k not in ("name", "contact")
    },
    "required": [],
    "additionalProperties": False,
}


def _apply_patch(current: dict, patch: dict) -> dict:
    """Overlay a partial resume JSON onto current. Experience entries merge
    field-wise onto the entry with the same title; unknown titles are ignored."""
    out = dict(current)
    for key, value in patch.items():
        if key not in RESUME_SCHEMA["properties"] or key in ("name", "contact"):
            continue
        if key == "experiences" and isinstance(value, list):
            entries = list(out.get("experiences", []))
            by_title = {
                e.get("title", "").lower(): i for i, e in enumerate(entries) if isinstance(e, dict)
            }
            for entry in value:
                if not isinstance(entry, dict):
                    continue
                idx = by_title.get(entry.get("title", "").lower())
                if idx is not None:
                    entries[idx] = {**entries[idx], **entry}
            out["experiences"] = entries
        else:
            out[key] = value
    return out


_LIST_MIN, _LIST_MAX = 6, 8


def _enforce_list_selection(base_str: str, tailored_str: str) -> str:
    """Cap selected competencies/skills at _LIST_MAX, in the model's relevance order.

    Only genuine base items survive (invented or reworded ones are dropped); if the
    model selected fewer than _LIST_MIN, the remainder is backfilled in base order.
    """
    base_items = [x.strip() for x in base_str.split("•") if x.strip()]
    canonical = {b.lower(): b for b in base_items}
    items = []
    for raw in tailored_str.split("•"):
        item = canonical.get(raw.strip().lower())
        if item and item not in items:
            items.append(item)
    if len(items) > _LIST_MAX:
        items = items[:_LIST_MAX]
    for b in base_items:
        if len(items) >= _LIST_MIN:
            break
        if b not in items:
            items.append(b)
    return " • ".join(items) if items else base_str


def _stream_json(client, *, system: str, user_msg: str, schema: dict, thinking) -> str:
    """Run a JSON-schema call, echoing the model's reasoning (dim) and output live.

    Streaming is only so the user can read along; returns the final JSON text.
    """
    dim, reset = ("\033[2m", "\033[0m") if sys.stdout.isatty() else ("", "")
    state = {"thinking": False}

    def on_reasoning(chunk: str):
        if not state["thinking"]:
            print(f"   {dim}[thinking]{reset}")
            state["thinking"] = True
        print(f"{dim}{chunk}{reset}", end="", flush=True)

    parts = []
    model = llm.generation_model("claude-sonnet-4-6")
    print(f"   model: {llm.describe_model(model)}", flush=True)
    with client.messages.stream(
        model=model,
        max_tokens=8192,
        thinking=thinking,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    ) as stream:
        if hasattr(stream, "on_reasoning"):
            stream.on_reasoning = on_reasoning
        started = False
        for chunk in stream.text_stream:
            if not started:
                print(("\n" if state["thinking"] else "") + "   [output]")
                started = True
            parts.append(chunk)
            print(chunk, end="", flush=True)
    print()
    return llm._strip_json_fences("".join(parts))


def get_tailored_resume(role_description: str) -> dict:
    """Send resume + role description to Claude, get back tailored resume data.

    Two passes, mirroring cover_letter: tailor, then a skeptical-editor tell-check
    over the edited fields (the draft survives a failed tell-check).
    """
    base = get_base_resume()
    client = _get_client()
    style_guide = _load_style_guide()

    system = TAILOR_SYSTEM_PROMPT.format(
        name=base.get("name") or "the candidate", style_guide=style_guide
    )

    payload = {k: v for k, v in base.items() if k in RESUME_SCHEMA["properties"]}
    user_msg = f"""Here is the current resume data (JSON):

{json.dumps(payload, indent=2, ensure_ascii=False)}

Here is the role description to tailor for:

{role_description}

Return only the fields you changed, as a JSON patch (see OUTPUT FORMAT)."""

    print("   Tailoring resume...")

    text = _stream_json(
        client,
        system=system,
        user_msg=user_msg,
        schema=DIFF_SCHEMA,
        thinking=llm.generation_thinking(),
    )

    try:
        patch = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"Error: Could not parse Claude's response as JSON: {e}")
        print("Raw response (first 500 chars):")
        print(text[:500])
        sys.exit(1)

    tailored = _apply_patch(payload, patch)

    # Competencies/skills are plain selections (enforced below), not prose.
    changed = [
        k
        for k in _changed_fields(payload, tailored)
        if k not in ("core_competencies", "technical_skills")
    ]
    if changed:
        revised = _tellcheck(payload, tailored, changed)
        if revised:
            tailored = revised
    # Models reliably ignore the 6-8 selection rule; enforce it in code.
    for key in ("core_competencies", "technical_skills"):
        tailored[key] = _enforce_list_selection(payload.get(key, ""), tailored.get(key, ""))
    return tailored


_TELLCHECK_SYSTEM_PROMPT = """\
You are a skeptical resume editor. A previous pass tailored a resume and may have \
introduced AI-scented prose. Re-read the tailored resume. In the CHANGED fields only, \
hunt for: sentences glued together from stacked clauses with semicolons or em-dash \
interruptions; one sentence carrying three ideas; rule-of-three lists; "not just X \
but Y" constructions; buzzwords ("leverage", "impact-driven", "stakeholder", \
"fast-paced"); self-descriptions that sound written ("fluent X and fluent Y", \
"passionate about"); grandiose verbs ("woven", "spearheaded"); uniform bullet rhythm \
across a section.
Fix what reeks: short plain sentences, one idea per sentence, plain verbs. Rewrite, \
never remove: every substantive claim in the tailored draft stays, just expressed \
plainer, and the emphasis the tailor chose stays. NEVER add new skills, claims or \
keywords. Do NOT include fields that are not listed as changed — they are already \
correct.
You are given the exact LINES the tailor edited. Rewrite only those lines, and only \
where they carry a tell. Every other line, in the fields and entries you return, \
must be copied back character for character, in the same order. A line with no tell \
is returned unchanged.
Output ONLY a JSON object with the changed fields listed below. When an experiences \
entry is listed, return that entry COMPLETE with the identical title. No commentary."""


def _flat_lines(value) -> list[str]:
    """All strings inside a resume field (lists/dicts traversed)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [x for v in value.values() for x in _flat_lines(v)]
    if isinstance(value, list):
        return [x for v in value for x in _flat_lines(v)]
    return []


def _edited_lines(base: dict, tailored: dict, changed: list[str]) -> list[str]:
    """Lines in the changed fields that don't appear verbatim in the base resume."""
    known = set(_flat_lines(base))
    return [ln for k in changed for ln in _flat_lines(tailored.get(k)) if ln not in known]


def _restore_untouched_lines(base: dict, tailored: dict, revised: dict) -> None:
    """Undo tell-check rewrites of experience lines the tailor never edited."""
    known = set(_flat_lines(base))
    by_title = {e.get("title"): e for e in tailored.get("experiences", []) if isinstance(e, dict)}
    for entry in revised.get("experiences", []):
        src = by_title.get(entry.get("title"))
        if not isinstance(entry, dict) or src is None:
            continue
        if src.get("subtitle") in known:
            entry["subtitle"] = src["subtitle"]
        for key in ("scope", "achievements"):
            new, old = entry.get(key), src.get(key)
            if isinstance(new, list) and isinstance(old, list) and len(new) == len(old):
                entry[key] = [o if o in known else n for n, o in zip(new, old)]


def _changed_fields(base: dict, tailored: dict) -> list[str]:
    """Top-level fields the tailoring pass actually edited."""
    return [k for k in RESUME_SCHEMA["properties"] if base.get(k) != tailored.get(k)]


# The tell-check sometimes reintroduces the very tells its prompt forbids.
# Catch that in code and reject its patch rather than laundering AI tells.
_TELL_WORDS = (
    "—",
    "leverage",
    "spearhead",
    "impact-driven",
    "stakeholder",
    "fast-paced",
    "seamless",
    "passionate",
)
# What the style guide bans is the CONSTRUCTION ("not just X but Y"), not the
# bare words: "actual performance, not just the hype" is fine English.
_TELL_CONSTRUCTION = re.compile(r"not\s+(?:just|only)\b[^.…;]*\bbut\b", re.IGNORECASE)
# Flattening symptom seen live: the tell-check shreds a statement into a chain
# of "I verb …" sentences. A human statement varies its openings; this doesn't.
_I_OPENINGS = re.compile(r"(?:^|(?<=[.!?])\s)I\s")

_STOPWORDS = frozenset(
    """a an and are as at be but by for from has have i in is it its my of on or our
    so that the their them this to we you your with not just only""".split()
)


def _content_words(value) -> set[str]:
    """Lowercased content words of a resume field (lists/dicts traversed)."""
    if isinstance(value, str):
        return {w for w in re.findall(r"[a-z0-9'\-]+", value.lower()) if w not in _STOPWORDS}
    if isinstance(value, (list, dict)):
        items = value.values() if isinstance(value, dict) else value
        out: set[str] = set()
        for v in items:
            out |= _content_words(v)
        return out
    return set()


def _fact_recall(draft: dict, revised: dict, patch: dict) -> float:
    """Share of the draft's content words that survived the tell-check patch.
    Style-agnostic enforcement of 'rewrite, never remove': any flattening that
    shreds claims lowers this, whatever its phrasing looks like."""
    total = lost = 0
    for key in patch:
        if key not in draft:
            continue
        kept = _content_words(draft[key])
        total += len(kept)
        lost += len(kept - _content_words(revised.get(key)))
    return 1.0 - (lost / total if total else 0.0)


_MIN_FACT_RECALL = 0.9


def _has_tells(value, skip_keys: tuple = ()) -> bool:
    if isinstance(value, str):
        low = value.lower()
        if any(w in low for w in _TELL_WORDS) or _TELL_CONSTRUCTION.search(value):
            return True
        return len(_I_OPENINGS.findall(value)) >= 4
    if isinstance(value, list):
        return any(_has_tells(v, skip_keys) for v in value)
    if isinstance(value, dict):
        # Experience titles legitimately use an em dash as separator.
        return any(_has_tells(v, skip_keys) for k, v in value.items() if k not in skip_keys)
    return False


def _changed_entry_titles(base: dict, tailored: dict) -> list[str]:
    """Titles of experience entries that differ between two resume dicts."""

    def by_title(resume: dict) -> dict:
        return {
            e.get("title", ""): json.dumps(e, sort_keys=True, ensure_ascii=False)
            for e in resume.get("experiences", [])
            if isinstance(e, dict)
        }

    b = by_title(base)
    return [title for title, blob in by_title(tailored).items() if b.get(title) != blob]


def _tellcheck_schema(changed: list[str], base: dict, tailored: dict) -> dict:
    """Schema for the tell-check patch: only the changed fields; for experiences,
    only the entries that actually differ (enforced via title enum)."""
    props = {k: RESUME_SCHEMA["properties"][k] for k in changed if k not in ("name", "contact")}
    if "experiences" in props:
        titles = _changed_entry_titles(base, tailored)
        if not titles:
            del props["experiences"]
        else:
            item = dict(RESUME_SCHEMA["properties"]["experiences"]["items"])
            item["properties"] = {**item["properties"], "title": {"enum": titles}}
            props["experiences"] = {"type": "array", "items": item}
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


def _tellcheck(base: dict, tailored: dict, changed: list[str]) -> dict | None:
    """Strip AI tells from the edited fields; returns None if the pass fails."""
    print("   Checking the edits for AI tells...")
    try:
        text = _stream_json(
            _get_client(),
            system=_TELLCHECK_SYSTEM_PROMPT,
            user_msg=(
                "Original resume (JSON):\n"
                + json.dumps(base, indent=2, ensure_ascii=False)
                + "\n\nTailored resume (JSON):\n"
                + json.dumps(tailored, indent=2, ensure_ascii=False)
                + "\n\nFields the tailor changed: "
                + ", ".join(changed)
                + "\n\nLines the tailor edited (the only lines you may rewrite):\n"
                + "\n".join(f"- {ln}" for ln in _edited_lines(base, tailored, changed))
                + "\n\nReturn the tell-checked patch: only these fields."
            ),
            schema=_tellcheck_schema(changed, base, tailored),
            thinking=llm.tellcheck_thinking(),
        )
        patch = json.loads(text)
        if any(_has_tells(v, skip_keys=("title", "subtitle")) for v in patch.values()):
            print("   Tell-check reintroduced banned phrasing; keeping the tailored draft.")
            return None
        revised = _apply_patch(tailored, patch)
        _restore_untouched_lines(base, tailored, revised)
        recall = _fact_recall(tailored, revised, patch)
        if recall < _MIN_FACT_RECALL:
            print(
                f"   Tell-check dropped {100 - recall * 100:.0f}% of the draft's wording;"
                " keeping the tailored draft."
            )
            return None
    except Exception as e:
        print(f"   Tell-check failed ({e}); using the tailored draft as-is.")
        return None
    # The tell-check must not silently re-touch fields the tailor left alone.
    for key, value in tailored.items():
        if key not in changed:
            revised[key] = value
    return revised


# ---------------------------------------------------------------------------
# Diff display
# ---------------------------------------------------------------------------


def show_edits(original: dict, tailored: dict):
    """Show a summarized diff of what changed, grouped by section."""
    # Section display names
    _section_names = {
        "professional_statement": "Professional Statement",
        "core_competencies": "Core Competencies",
        "technical_skills": "Technical Skills",
        "earlier_roles": "Earlier Roles",
        "education": "Education",
        "languages": "Languages",
        "interests": "Interests",
    }

    # Collect changes per top-level section
    section_counts: dict[str, list[str]] = {}

    def _note(section: str, detail: str):
        section_counts.setdefault(section, []).append(detail)

    def _compare_strings(a: str, b: str) -> str | None:
        """Return a one-line description of how b differs from a, or None."""
        a, b = a.strip(), b.strip()
        if a == b:
            return None
        wa, wb = a.split(), b.split()
        if sorted(wa) == sorted(wb):
            return "reordered"
        if len(wb) > len(wa) * 1.3:
            return "expanded"
        if len(wb) < len(wa) * 0.7:
            return "shortened"
        return "rephrased"

    # Top-level string fields
    for key, name in _section_names.items():
        orig = original.get(key, "")
        new = tailored.get(key, "")
        if isinstance(orig, str) and isinstance(new, str):
            desc = _compare_strings(orig, new)
            if desc:
                _note(name, desc)
        elif isinstance(orig, list) and isinstance(new, list):
            n_changed = sum(
                1
                for o, n in zip(orig, new)
                if isinstance(o, str) and isinstance(n, str) and o.strip() != n.strip()
            )
            added = len(new) - len(orig)
            if n_changed:
                _note(name, f"{n_changed} bullet(s) changed")
            if added > 0:
                _note(name, f"{added} added")
            elif added < 0:
                _note(name, f"{-added} removed")

    # Experiences
    for i, (orig_exp, new_exp) in enumerate(
        zip(original.get("experiences", []), tailored.get("experiences", []))
    ):
        title = orig_exp.get("title", f"Experience {i + 1}")
        # Extract company name from title (up to the first —)
        company = title.split("\u2014")[0].strip() if "\u2014" in title else title.split()[0]

        for sub in ("scope", "achievements", "subtitle"):
            ov, nv = orig_exp.get(sub, ""), new_exp.get(sub, "")
            if isinstance(ov, list) and isinstance(nv, list):
                if ov == nv:
                    continue
                reordered = sorted(ov) == sorted(nv)
                n_changed = sum(1 for o, n in zip(ov, nv) if o.strip() != n.strip())
                if reordered:
                    _note(company, f"{sub}: reordered")
                elif n_changed:
                    _note(company, f"{sub}: {n_changed} bullet(s) changed")
            elif isinstance(ov, str) and isinstance(nv, str):
                desc = _compare_strings(ov, nv)
                if desc:
                    _note(company, f"{sub}: {desc}")

    if not section_counts:
        print("   No changes — resume already fits well.")
        return False

    total = sum(len(v) for v in section_counts.values())
    print(f"   {total} change(s):")
    for section, notes in section_counts.items():
        print(f"   • {section}: {', '.join(notes)}")

    return True


# ---------------------------------------------------------------------------
# PDF conversion
# ---------------------------------------------------------------------------


def docx_to_pdf(docx_path: str, output_dir: str = None) -> str:
    """Convert a docx to PDF using LibreOffice."""
    if output_dir is None:
        output_dir = os.path.dirname(docx_path)

    result = subprocess.run(
        [
            "libreoffice",
            "--headless",
            "--convert-to",
            "pdf",
            docx_path,
            "--outdir",
            output_dir,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    if result.returncode != 0:
        print(f"Error converting to PDF: {result.stderr}")
        return ""

    pdf_name = os.path.splitext(os.path.basename(docx_path))[0] + ".pdf"
    return os.path.join(output_dir, pdf_name)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def generate_base_resume(role_description: str = "", output_name: str | None = None) -> str:
    """Build a PDF from the base (unmodified) resume. Returns path to PDF."""
    output_dir = os.path.join(_project_root, "applications")
    os.makedirs(output_dir, exist_ok=True)
    if not output_name:
        company, role = extract_job_meta(role_description)
        output_name = make_output_stem("Resume", company, role)
    docx_path = os.path.join(output_dir, f"{output_name}.docx")
    build_resume_docx(get_base_resume(), docx_path, photo_src=RESUME_DOCX_PATH)
    pdf_path = docx_to_pdf(docx_path, output_dir)
    print(f"   Saved resume: {pdf_path or docx_path}")
    return pdf_path or docx_path


def tailor_resume(
    role_description: str,
    extra_context: str = "",
    output_name: str | None = None,
    output_dir: str | None = None,
):
    """Tailor the resume for a role description and output as PDF.

    Args:
        role_description: The job posting or role description text.
        extra_context: Optional extra instructions (e.g. "emphasise terraform").
        output_name: Base name for output files (without extension).
        output_dir: Directory for output files. Defaults to project root.

    Returns:
        Path to the generated PDF, or empty string on failure.
    """
    if not output_dir:
        output_dir = os.path.join(_project_root, "applications")
        os.makedirs(output_dir, exist_ok=True)
    if not output_name:
        company, role = extract_job_meta(role_description)
        output_name = make_output_stem("Resume", company, role)

    full_description = role_description
    if extra_context:
        full_description += f"\n\nExtra context: {extra_context}"

    # Get tailored resume from Claude
    base = get_base_resume()
    tailored = get_tailored_resume(full_description)

    # Show diff (informational only — no confirmation)
    has_changes = show_edits(base, tailored)
    if not has_changes:
        tailored = base

    # Build docx
    docx_path = os.path.join(output_dir, f"{output_name}.docx")
    build_resume_docx(tailored, docx_path, photo_src=RESUME_DOCX_PATH)
    print(f"   Saved docx: {docx_path}")

    # Convert to PDF
    pdf_path = docx_to_pdf(docx_path, output_dir)
    if pdf_path:
        print(f"   Saved PDF:  {pdf_path}")
    else:
        print("   PDF conversion failed. The docx file is still available.")

    return pdf_path or docx_path


# ---------------------------------------------------------------------------
# URL / job fetching (reuse from cover_letter.py)
# ---------------------------------------------------------------------------


def fetch_job_description(url: str) -> str:
    """Extract text from a job posting URL."""
    try:
        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            return trafilatura.extract(downloaded) or ""
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# CLI usage (standalone)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python resume_tailor.py <role_description_or_url>")
        print("       python resume_tailor.py --url <job_posting_url>")
        sys.exit(1)

    if sys.argv[1] == "--url":
        if len(sys.argv) < 3:
            print("Error: --url requires a URL argument.")
            sys.exit(1)
        url = sys.argv[2]
        print(f"   Fetching job description from {url}...")
        desc = fetch_job_description(url)
        if not desc:
            print("   Error: Could not extract text from URL.")
            sys.exit(1)
        tailor_resume(desc)
    else:
        desc = " ".join(sys.argv[1:])
        tailor_resume(desc)
