"""Resume tailoring: minimal edits to match a role description, output as PDF.

Uses Claude to suggest minimal, natural-sounding edits to the resume text,
then rebuilds the docx with the original styling and converts to PDF.
"""

import json
import os
import re
import subprocess
import sys

import anthropic
import trafilatura
from cover_letter import extract_job_meta, make_output_stem
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from dotenv import load_dotenv

load_dotenv()

_project_root = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Anthropic client
# ---------------------------------------------------------------------------

_client = None


def _get_client():
    global _client
    if _client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            print("Error: ANTHROPIC_API_KEY not set.")
            sys.exit(1)
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


# ---------------------------------------------------------------------------
# Resume data structure
# ---------------------------------------------------------------------------

# The base resume text, parsed into editable sections.
# Claude will return a modified version of this structure.

BASE_RESUME = {
    "name": "Milo de Vries",
    "contact": "Wütoschingen, Germany | +31 6 3976 5259 | milo@xdh.nl | LinkedIn: linkedin.com/in/milod",
    "professional_statement": (
        "Over the last decade I've gone from founding and scaling a climate-tech startup to shaping "
        "product strategy at South Pole, using code, data and design to turn ideas into tools with "
        "measurable impact. Skilled at shaping SaaS and data-driven platforms, integrating AI/ML "
        "where it makes sense and weaving security and identity into products used by enterprise "
        "clients worldwide. Driven by pushing tech-for-good to its limits and telling the story."
    ),
    "core_competencies": (
        "Product Strategy & Roadmapping \u2022 SaaS & B2B Platforms \u2022 ClimateTech Solutions "
        "\u2022 AI/ML Integrations \u2022 Market Analysis \u2022 Stakeholder Management \u2022 "
        "Fundraising & Investor Relations \u2022 Team Leadership & Mentorship \u2022 "
        "Agile / Lean Product Development \u2022 User Research / UX Collaboration"
    ),
    "technical_skills": (
        "Cloud & DevOps (AWS, GCP, Terraform) \u2022 SaaS Architecture & Integrations "
        "(REST/GraphQL APIs) \u2022 Data & Analytics (SQL, BI tools) \u2022 Python \u2022 "
        "JS/TS \u2022 Security & Identity Management (IAM) \u2022 ML / AI Integrations"
    ),
    "experiences": [
        {
            "title": "Product Lead \u2014 South Pole BV, Amsterdam (Apr 2022 \u2013 Present)",
            "subtitle": "South Pole helps organisations realise deep decarbonisation pathways across industries.",
            "scope": [
                "Coordinated South Pole\u2019s Accelerator for six early-stage climate-tech applications, defining business cases, assessing PoCs and guiding roadmaps.",
                "Directed product and technical strategy across multiple SaaS initiatives spun out of the Accelerator (Climate Risk Tool, CSRD Gap Tool), taking them from ideation through PoC, MVP and scale.",
                "Led the technical side of the SME->Enterprise Climate Solution: drove architecture, UX design, and user testing. Contributing to market analysis, product strategy, design and evaluation of AI integrations.",
                "Co-developed an AI-assisted PCF (product carbon footprinting) tool.",
                "Conceived an offensive PR effort to restore South Pole\u2019s reputation in nature based offsetting. For this devised and scoped an open-source forest-impact calculator using satellite imagery and ML",
            ],
            "achievements": [
                "Took multiple SaaS products from concept to launch, collectively serving 30+ enterprise clients and realising seven-figure revenue within 24 months.",
                "Delivered the SME Climate Solution MVP in 6 months with full UX/user-testing cycles.",
                "Built and launched a no-code double-materiality-assessment tool in 4 months, reducing manual work for consultants.",
                "Led IAM/SSO integrations across two enterprise tools, including the first integration to go live under new security requirements.",
                "Delivered a 10\u00d7 performance improvement on the Climate Risk tool.",
                "Architected and realised the first AI-assisted PCF prototype.",
            ],
        },
        {
            "title": "Founder & CEO \u2014 CO\u2082ok, Utrecht (Apr 2017 \u2013 Mar 2022)",
            "subtitle": "CO\u2082ok enables consumers to make their shopping climate neutral",
            "scope": [
                "Founded and grew CO\u2082ok from concept to acquisition; led strategy, product, development, fundraising and team building.",
                "Designed and launched a seamless, often tailored, integration product for online retailers across multiple e-commerce platforms.",
                "Developed sales and marketing strategies and oversaw onboarding and support.",
                "Brought a second product from concept to market and made a data-driven decision to sunset it and focus on the core product.",
                "Negotiated an acquihire by South Pole.",
            ],
            "achievements": [
                "Onboarded 50+ merchants and enabled tens of thousands of compensated transactions, making climate-neutral shopping easy for consumers.",
                "Built and led a multidisciplinary team of 10+, secured seed funding and partnerships.",
                "Personally landed first major client through a tailored evidence-based pitch.",
                "Created and launched a second product, grew it to 100+ users.",
                "Delivered a successful founder exit while ensuring continued growth of the product and team.",
            ],
        },
    ],
    "earlier_roles": [
        "Senior Systems Engineer, e-Village (2015\u20132016)",
        "Senior DevOps Engineer, Unitt (2014\u20132015)",
        "DevOps Developer, Springest (2014)",
        "System Engineer / Security Lieutenant, Info.nl (2007\u20132013)",
    ],
    "education": [
        "MITx \u2013 Introduction to CS & Data Science (2016)",
        "Stanford \u2013 Startup Engineering (2013)",
        "Architecting on AWS (2014)",
        "Technical University Eindhoven \u2013 Computer Science / Electrical Engineering (partly completed)",
    ],
    "languages": "Dutch (native) \u2022 English (fluent) \u2022 German (fluent)",
    "interests": (
        "Designing (climate-tech) startups \u2022 Adventures with my daughter Nova "
        "Skiing/snowboarding \u2022 Technology \u2022 IT security/digital privacy"
    ),
}


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


def build_resume_docx(data: dict, output_path: str):
    """Build a styled resume docx from structured data."""
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

    doc.save(output_path)
    return output_path


# ---------------------------------------------------------------------------
# Claude-powered tailoring
# ---------------------------------------------------------------------------


def _load_style_guide() -> str:
    path = os.path.join(_project_root, "Cover-letter styleguide.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


TAILOR_SYSTEM_PROMPT = """\
You are a resume editor for Milo de Vries. Your job is to make MINIMAL, targeted edits \
to his resume so it better matches a specific role description.

RULES — follow these strictly:
1. Make as few changes as possible. Most sections should stay IDENTICAL.
2. NEVER fabricate experience, skills, or achievements. Only use what's already there.
3. NEVER add buzzwords, corporate-speak, or generic filler. Keep Milo's direct, plain voice.
4. Allowed edits:
   - Reword the Professional Statement to emphasize relevant experience (keep same length)
   - Reorder bullet points to put the most relevant ones first
   - Slightly rephrase a bullet to highlight a relevant aspect (keep factual)
   - Reorder items in Core Competencies or Technical Skills to lead with the most relevant
   - Add a skill to Technical Skills ONLY if it's clearly implied by his experience
5. Do NOT touch: name, contact, dates, job titles, company names, education, languages, interests.
6. Do NOT lengthen the resume. If you add a word somewhere, remove one elsewhere.
7. The result should read naturally — a human reviewer should not detect AI involvement.

STYLE REFERENCE (for tone):
{style_guide}

OUTPUT FORMAT:
Return ONLY valid JSON matching the exact structure of the input resume data. \
Do not wrap in markdown code fences. Do not add commentary. \
Return the complete structure — unchanged sections included verbatim."""


def get_tailored_resume(role_description: str) -> dict:
    """Send resume + role description to Claude, get back tailored resume data."""
    client = _get_client()
    style_guide = _load_style_guide()

    system = TAILOR_SYSTEM_PROMPT.format(style_guide=style_guide)

    user_msg = f"""Here is the current resume data (JSON):

{json.dumps(BASE_RESUME, indent=2, ensure_ascii=False)}

Here is the role description to tailor for:

{role_description}

Return the tailored resume as JSON."""

    print("   Tailoring resume with Claude...")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )

    text = response.content[0].text.strip()

    # Strip markdown fences if present
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"Error: Could not parse Claude's response as JSON: {e}")
        print("Raw response (first 500 chars):")
        print(text[:500])
        sys.exit(1)


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
                1 for o, n in zip(orig, new)
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
    build_resume_docx(BASE_RESUME, docx_path)
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
    tailored = get_tailored_resume(full_description)

    # Show diff (informational only — no confirmation)
    has_changes = show_edits(BASE_RESUME, tailored)
    if not has_changes:
        tailored = BASE_RESUME

    # Build docx
    docx_path = os.path.join(output_dir, f"{output_name}.docx")
    build_resume_docx(tailored, docx_path)
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
