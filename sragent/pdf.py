"""PDF -> text with page markers (so evidence locations read 's45 · Page 3').

Uses pypdf (pure Python). If the system has `pdftotext` (poppler), it is preferred because it keeps
reading order better in two-column layouts. Scanned PDFs without a text layer give almost no text;
the caller then falls back to the abstract and tells the user.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


def _clean(t: str) -> str:
    t = t.replace("\r", "")
    t = re.sub(r"-\n(?=[a-z])", "", t)              # de-hyphenate line breaks
    t = re.sub(r"(?<![.!?:;\n])\n(?!\n)", " ", t)     # join wrapped lines inside paragraphs
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def pdf_to_text(path: str | Path) -> str:
    path = Path(path)
    pages: list[str] = []
    if shutil.which("pdftotext"):
        try:
            out = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"],
                                 capture_output=True, timeout=120, check=True).stdout.decode("utf-8", "ignore")
            pages = out.split("\f")
        except (subprocess.SubprocessError, OSError):
            pages = []
    if not any(p.strip() for p in pages):
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        pages = [(pg.extract_text() or "") for pg in reader.pages]
    parts = []
    for i, p in enumerate(pages, 1):
        p = _clean(re.sub(r" {2,}", " ", p))
        if p:
            parts.append(f"## Page {i}\n{p}")
    return "\n\n".join(parts)
