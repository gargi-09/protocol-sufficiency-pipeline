# from contract import canonicalize
# from extract import find_methods_section
# from sections import find_subsections

# with open("papers/Dos et al.txt", "r", encoding="utf-8") as f:
#     raw = f.read()
# canonical_text, text_sha = canonicalize(raw)

# m_start, m_end = find_methods_section(canonical_text)
# methods_text = canonical_text[m_start:m_end]
# subsections = find_subsections(methods_text, m_start)

# for s in subsections:
#     print(repr(s["header"]))

from contract import canonicalize, FieldPack
import yaml
from extract import find_candidate

with open("papers/Shein.txt", "r", encoding="utf-8") as f:
    raw = f.read()
canonical_text, text_sha = canonicalize(raw)

with open("fields/oncobiology_v0.yaml", "r", encoding="utf-8") as f:
    pack_data = yaml.safe_load(f)
pack = FieldPack(**pack_data)

spec = pack.by_id("cellline.passage_number")
result = find_candidate(canonical_text, text_sha, spec)
print(result)