"""Convert docs/專案報告_v1.1.md to .docx (Word) and optionally .pdf. Version 1.1.

Uses pypandoc-binary — no external pandoc install required. Run:

    python scripts/convert_report.py                    # produce .docx only
    python scripts/convert_report.py --pdf              # also produce .pdf (needs LaTeX)
    python scripts/convert_report.py --input FILE.md    # convert another markdown

Mermaid caveat: pandoc does not render Mermaid diagrams natively — the code
blocks appear as monospace text in Word. To embed rendered diagrams, either
(a) view the .md on GitHub / VS Code preview and screenshot them, or
(b) install Node.js + `npm install -g @mermaid-js/mermaid-cli` and re-run
with --render-mermaid (implemented as a stub below).
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

import pypandoc


def convert_to_docx(md_path: Path, docx_path: Path):
    print(f"converting {md_path.name} -> {docx_path.name} ...")
    pypandoc.convert_file(
        str(md_path),
        to="docx",
        outputfile=str(docx_path),
        extra_args=[
            "--standalone",
            "--toc",
            "--toc-depth=2",
            f"--resource-path={md_path.parent}",
        ],
    )
    size = docx_path.stat().st_size
    print(f"  wrote {docx_path}  ({size:,} bytes)")


def convert_to_pdf(md_path: Path, pdf_path: Path):
    print(f"converting {md_path.name} -> {pdf_path.name} ...")
    try:
        pypandoc.convert_file(
            str(md_path),
            to="pdf",
            outputfile=str(pdf_path),
            extra_args=[
                "--standalone",
                "--toc", "--toc-depth=2",
                "--pdf-engine=xelatex",  # needed for CJK
                "-V", "CJKmainfont=Microsoft JhengHei",
                f"--resource-path={md_path.parent}",
            ],
        )
        print(f"  wrote {pdf_path}  ({pdf_path.stat().st_size:,} bytes)")
    except Exception as e:
        print(f"  PDF conversion failed: {e}")
        print(f"  (PDF via pandoc needs a LaTeX distribution with xelatex + CJK fonts.")
        print(f"   Convert the .docx to PDF via Word 'Save As' instead — same result.)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--input", default="docs/專案報告_v1.1.md",
                    help="markdown input (default: docs/專案報告_v1.1.md)")
    ap.add_argument("--outdir", default="docs",
                    help="output directory (default: docs/)")
    ap.add_argument("--pdf", action="store_true",
                    help="also produce PDF (needs LaTeX)")
    args = ap.parse_args()

    md = Path(args.input)
    if not md.exists():
        sys.exit(f"input not found: {md}")

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    base = md.stem  # e.g. "專案報告_v1.0"

    convert_to_docx(md, outdir / f"{base}.docx")
    if args.pdf:
        convert_to_pdf(md, outdir / f"{base}.pdf")


if __name__ == "__main__":
    main()
