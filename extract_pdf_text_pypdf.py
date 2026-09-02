"""
extract_pdf_text_pypdf.py -- same job as extract_pdf_text.py, different
library (pypdf instead of pdfplumber), to test whether the space-stripping
seen on 5 of 8 real dev-set papers is specific to pdfplumber's handling of
these PDFs' font encoding, or a deeper property of the files themselves.

Writes to a SEPARATE output folder (papers/) rather than overwriting
papers/, so both extractions can be compared side by side before deciding
which one to actually use.
"""
import os
from pypdf import PdfReader

SOURCE_DIR = "papers_source"
DEST_DIR = "papers"   # separate from papers/ on purpose, for comparison

os.makedirs(DEST_DIR, exist_ok=True)

for filename in os.listdir(SOURCE_DIR):
    if not filename.lower().endswith(".pdf"):
        continue

    pdf_path = os.path.join(SOURCE_DIR, filename)
    txt_filename = os.path.splitext(filename)[0] + ".txt"
    txt_path = os.path.join(DEST_DIR, txt_filename)

    reader = PdfReader(pdf_path)
    full_text = ""
    for page in reader.pages:
        text = page.extract_text()
        if text:
            full_text += text + "\n"

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(full_text)

    print(f"{filename} -> {txt_filename} ({len(full_text)} chars)")