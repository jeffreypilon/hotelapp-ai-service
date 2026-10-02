"""Render the Markdown corpus to PDF.

The Markdown under corpus/ is the reviewable source of truth; these PDFs are the artifact the
ingestion pipeline actually reads, because that is the form a hotel would hand over. Keeping the
source as Markdown means a policy change shows up as a readable diff rather than a 40 KB binary,
which matters because the corpus is held to the same consistency rules as the implementations --
see stacks/ai-service/testing-standards.md, "Policy consistency".

The output is deliberately NOT a pristine one-column render. Real documents carry letterhead,
running headers, page numbers, tables and figures, and the text extracted from them arrives in a
messier order than from a clean render. A corpus that is too tidy would make the ingestion
pipeline look more robust than it is.

Figures are embedded, and every figure in the corpus carries a caption, because pypdf extracts
text and not images: anything stated only in a picture is invisible to retrieval.

Usage:  python scripts/render_corpus.py
Output: corpus/pdf/<slug>.pdf  (gitignored -- build artifacts)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from fpdf import FPDF
from fpdf.enums import XPos, YPos

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "corpus"
FIGURES = CORPUS / "figures"
OUT = CORPUS / "pdf"

REVISION = "Revision 2026.10 - Guest Services"

LOGO_FOR = {
    "Harborview Grand": "logo-harborview.png",
    "Lakeside Inn": "logo-lakeside.png",
    None: "logo-brand.png",
}

# Core PDF fonts are latin-1 only. Prefer a real TTF so em dashes and middle dots survive;
# fall back to transliteration rather than failing on a machine without one.
TTF_CANDIDATES = [
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf"),
]

TRANSLITERATE = {
    "\u2014": "-", "\u2013": "-", "\u00b7": "-", "\u2019": "'", "\u2018": "'",
    "\u201c": '"', "\u201d": '"', "\u2026": "...", "\u00a0": " ",
}


@dataclass
class Doc:
    path: Path
    title: str
    property: str | None
    blocks: list[tuple[str, object]] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return self.path.stem


def parse(path: Path) -> Doc:
    raw = path.read_text(encoding="utf-8")

    title, prop = path.stem, None
    if raw.startswith("---"):
        fm, _, raw = raw[3:].partition("---")
        for line in fm.strip().splitlines():
            key, _, value = line.partition(":")
            value = value.strip()
            if key.strip() == "title":
                title = value
            elif key.strip() == "property":
                prop = None if value in ("null", "") else value

    blocks: list[tuple[str, object]] = []
    para: list[str] = []
    table: list[list[str]] = []

    def flush_para() -> None:
        if para:
            blocks.append(("p", " ".join(para)))
            para.clear()

    def flush_table() -> None:
        if table:
            blocks.append(("table", [r[:] for r in table]))
            table.clear()

    for line in raw.splitlines():
        s = line.rstrip()
        if s.startswith("|"):
            flush_para()
            cells = [c.strip() for c in s.strip("|").split("|")]
            if not all(set(c) <= set("-: ") for c in cells):  # skip the ---|--- separator
                table.append([inline(c) for c in cells])
            continue
        flush_table()

        if not s.strip():
            flush_para()
        elif s.startswith("!["):
            flush_para()
            m = re.match(r"!\[(.*?)\]\((.*?)\)", s)
            if m:
                blocks.append(("figure", (m.group(2), m.group(1))))
        elif s.startswith("### "):
            flush_para()
            blocks.append(("h3", inline(s[4:])))
        elif s.startswith("## "):
            flush_para()
            blocks.append(("h2", inline(s[3:])))
        elif s.startswith("# "):
            flush_para()
            blocks.append(("h1", inline(s[2:])))
        elif s.startswith("- "):
            flush_para()
            blocks.append(("li", inline(s[2:])))
        elif s.startswith("> "):
            flush_para()
            blocks.append(("quote", inline(s[2:])))
        elif s.strip() == "---":
            flush_para()
            blocks.append(("hr", ""))
        else:
            para.append(inline(s.strip()))

    flush_para()
    flush_table()
    return Doc(path=path, title=title, property=prop, blocks=blocks)


def inline(text: str) -> str:
    text = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", text)
    text = text.replace("**", "").replace("`", "")
    return re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", text)


class CorpusPDF(FPDF):
    def __init__(self, doc: Doc, unicode_ok: bool) -> None:
        super().__init__(format="Letter", unit="mm")
        self.doc = doc
        self.unicode_ok = unicode_ok
        self.family = "Body" if unicode_ok else "Helvetica"
        self.set_auto_page_break(auto=True, margin=22)
        self.set_margins(20, 18, 20)

    def t(self, s: str) -> str:
        if self.unicode_ok:
            return s
        for a, b in TRANSLITERATE.items():
            s = s.replace(a, b)
        return s.encode("latin-1", "replace").decode("latin-1")

    def header(self) -> None:
        if self.page_no() == 1:
            return
        self.set_font(self.family, "", 8)
        self.set_text_color(130)
        label = self.doc.property or "HotelApp Hotels"
        self.cell(0, 5, self.t(f"{label}  |  {self.doc.title}"), align="L",
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_draw_color(200)
        self.line(20, 24, 196, 24)
        self.ln(6)
        self.set_text_color(0)

    def footer(self) -> None:
        self.set_y(-15)
        self.set_font(self.family, "", 7.5)
        self.set_text_color(140)
        self.cell(0, 5, self.t(REVISION), align="L")
        self.set_x(20)
        self.cell(0, 5, self.t(f"Page {self.page_no()} of {{nb}}"), align="R")
        self.set_text_color(0)


def render(doc: Doc, unicode_ok: bool) -> Path:
    pdf = CorpusPDF(doc, unicode_ok)
    if unicode_ok:
        regular, bold = next(p for p in TTF_CANDIDATES if Path(p[0]).exists())
        pdf.add_font("Body", "", regular)
        pdf.add_font("Body", "B", bold)
    pdf.add_page()

    logo = FIGURES / LOGO_FOR.get(doc.property, "logo-brand.png")
    if logo.exists():
        pdf.image(str(logo), x=20, y=14, w=86)
        pdf.set_y(44)

    w = pdf.w - 40

    for kind, payload in doc.blocks:
        if kind == "h1":
            pdf.set_font(pdf.family, "B", 19)
            pdf.multi_cell(w, 8.5, pdf.t(str(payload)), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(3)
        elif kind == "h2":
            pdf.ln(3)
            pdf.set_font(pdf.family, "B", 13)
            pdf.multi_cell(w, 6.5, pdf.t(str(payload)), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(1.5)
        elif kind == "h3":
            pdf.ln(2)
            pdf.set_font(pdf.family, "B", 11)
            pdf.multi_cell(w, 5.5, pdf.t(str(payload)), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(1)
        elif kind == "p":
            pdf.set_font(pdf.family, "", 10)
            pdf.multi_cell(w, 5.2, pdf.t(str(payload)), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(2)
        elif kind == "li":
            pdf.set_font(pdf.family, "", 10)
            pdf.cell(5)
            pdf.multi_cell(w - 5, 5.2, pdf.t("- " + str(payload)),
                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        elif kind == "quote":
            pdf.set_font(pdf.family, "B", 10)
            pdf.set_fill_color(243, 246, 250)
            pdf.multi_cell(w, 5.4, pdf.t(str(payload)), fill=True,
                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(2)
        elif kind == "hr":
            pdf.ln(2)
            pdf.set_draw_color(215)
            pdf.line(20, pdf.get_y(), 196, pdf.get_y())
            pdf.ln(4)
        elif kind == "table":
            rows = payload  # type: ignore[assignment]
            pdf.set_font(pdf.family, "", 9)
            with pdf.table(line_height=5.4, text_align="LEFT", padding=1.6) as tbl:
                for i, row in enumerate(rows):  # type: ignore[arg-type]
                    r = tbl.row()
                    for cell in row:
                        r.cell(pdf.t(cell))
                    if i == 0:
                        pdf.set_font(pdf.family, "", 9)
            pdf.ln(3)
        elif kind == "figure":
            src, caption = payload  # type: ignore[misc]
            img = CORPUS / src
            if img.exists():
                pdf.ln(1)
                pdf.image(str(img), x=20, w=w)
                pdf.ln(1.5)
                pdf.set_font(pdf.family, "", 8.5)
                pdf.set_text_color(110)
                pdf.multi_cell(w, 4.6, pdf.t(f"Figure: {caption}"),
                               new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.set_text_color(0)
                pdf.ln(3)

    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"{doc.slug}.pdf"
    pdf.output(str(out))
    return out


def main() -> None:
    unicode_ok = any(Path(p[0]).exists() and Path(p[1]).exists() for p in TTF_CANDIDATES)
    if not unicode_ok:
        print("No TTF found - falling back to core fonts with transliteration.")

    sources = sorted(CORPUS.rglob("*.md"))
    sources = [p for p in sources if "pdf" not in p.parts and p.name != "README.md"]

    total = 0
    for path in sources:
        doc = parse(path)
        out = render(doc, unicode_ok)
        size = out.stat().st_size // 1024
        total += size
        print(f"  {out.name:<42} {size:>4} KB   {doc.property or 'brand-wide'}")
    print(f"\n{len(sources)} documents, {total} KB total -> {OUT}")


if __name__ == "__main__":
    main()
