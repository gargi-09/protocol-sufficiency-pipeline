"""
extract_pdf_text.py -- converts a folder of PDFs into the papers/*.txt
files analyze() actually consumes. One-time utility, not part of the
pipeline itself (analyze() never touches a PDF directly).
"""
import os
import pdfplumber

SOURCE_DIR = "papers_source"   # put raw PDFs here
DEST_DIR = "papers"            # extracted .txt goes here, this is what analyze() reads

os.makedirs(DEST_DIR, exist_ok=True)

for filename in os.listdir(SOURCE_DIR):
    if not filename.lower().endswith(".pdf"):
        continue

    pdf_path = os.path.join(SOURCE_DIR, filename)
    txt_filename = os.path.splitext(filename)[0] + ".txt"
    txt_path = os.path.join(DEST_DIR, txt_filename)

    with pdfplumber.open(pdf_path) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(full_text)

    print(f"{filename} -> {txt_filename} ({len(full_text)} chars)")