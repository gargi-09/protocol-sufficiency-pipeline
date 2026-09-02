"""
diagnose_scoping.py -- for every FIELD_ABSENT result, shows what section
scoping chose, what it found (if anything), and what an unscoped search
of the whole Methods section finds instead. Updated to match sections.py's
find_relevant_subsections (plural) -- since section-scoping now searches
ALL matching subsections, not just one.
"""
import os
import yaml
from contract import FieldPack, canonicalize
from classify_study import classify_study
from extract import find_candidate, find_methods_section, split_sentences, field_keywords, _HAS_DIGIT
from validate import validate_field
from sections import find_subsections, find_relevant_subsections

with open("fields/oncobiology_v0.yaml", "r", encoding="utf-8") as f:
    pack_data = yaml.safe_load(f)
pack = FieldPack(**pack_data)


def best_candidate_anywhere_in_methods(canonical_text, text_sha, spec):
    m_start, m_end = find_methods_section(canonical_text)
    search_text = canonical_text[m_start:m_end]
    sentences = split_sentences(search_text, base_offset=m_start)
    weighted_keywords = field_keywords(spec.field_id)

    best, best_score = None, 0
    for sent_text, start, end in sentences:
        low = sent_text.lower()
        score = sum(w for kw, w in weighted_keywords if kw.lower() in low)
        if score == 0:
            continue
        if spec.type == "quantity" and not _HAS_DIGIT.search(sent_text):
            continue
        if score > best_score:
            best_score = score
            best = sent_text.strip()
    return best


mismatches = 0
for filename in sorted(os.listdir("papers")):
    if not filename.endswith(".txt"):
        continue
    doc_id = filename.replace(".txt", "")
    with open(f"papers/{filename}", "r", encoding="utf-8") as f:
        raw = f.read()
    canonical_text, text_sha = canonicalize(raw)
    profile = classify_study(canonical_text, text_sha)

    m_start, m_end = find_methods_section(canonical_text)
    methods_text = canonical_text[m_start:m_end]
    subsections = find_subsections(methods_text, m_start)

    for spec in pack.applicable(profile):
        scoped_obs = find_candidate(canonical_text, text_sha, spec)
        scoped_outcome = validate_field(scoped_obs, spec)

        if scoped_outcome["verdict"] != "FIELD_ABSENT":
            continue

        unscoped_best = best_candidate_anywhere_in_methods(canonical_text, text_sha, spec)
        if not unscoped_best:
            continue

        matching_subs = find_relevant_subsections(subsections, spec.field_id)
        chosen_sections = [s["header"] for s in matching_subs] if matching_subs else ["(no section matched)"]

        mismatches += 1
        print(f"\n{'-'*90}")
        print(f"[{doc_id}] {spec.field_id}")
        print(f"  Section scoping tried: {chosen_sections}")
        print(f"  Scoped search result:  FIELD_ABSENT (nothing found)")
        print(f"  Unscoped search found: {unscoped_best[:150]!r}")

print(f"\n\n{'='*90}")
print(f"TOTAL: {mismatches} fields still mismatched after the fix")