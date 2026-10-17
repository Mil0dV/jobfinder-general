"""Plain-text extraction from user-supplied documents (.docx, .pdf, .txt, .md)."""

import os

SUPPORTED_EXTENSIONS = (".docx", ".pdf", ".txt", ".md")
SUPPORTED_LABEL = ", ".join(SUPPORTED_EXTENSIONS[:-1]) + ", or " + SUPPORTED_EXTENSIONS[-1]


def _extract_docx_text(path: str) -> str:
    """Pull the visible text out of a .docx, one paragraph per line."""
    from docx import Document

    doc = Document(path)
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def _extract_pdf_text(path: str) -> str:
    """Pull the text out of a PDF, page by page."""
    from pypdf import PdfReader

    reader = PdfReader(path)
    return "\n".join(page.extract_text() or "" for page in reader.pages).strip()


def extract_text(path: str) -> str:
    """Extract plain text from a .docx, .pdf, or plain-text file (by extension)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        return _extract_docx_text(path)
    if ext == ".pdf":
        return _extract_pdf_text(path)
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()
