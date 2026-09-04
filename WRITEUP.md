# Protocol sufficiency analysis — writeup

Ordered by the sequence of importance `01_README.md` gives. Full working record,
including the end-to-end architecture and three further failure cases, is in
**[`DESIGN_NOTES.md`](DESIGN_NOTES.md)**; every defect with evidence is in
**[`defects_found.md`](defects_found.md)**.

**The one decision everything follows from.** `analyze.py` loops the checklist,
not the text — `for spec in pack.applicable(profile)`, with
`validate_field(None, spec)` a supported call. A gap emits no tokens, so a
text-driven pipeline is structurally blind to it, and 13 of 18 gold labels are
`GAP_ABSENT`: that architecture caps near zero on the majority class however good
its extractor is.

*"The model proposes and the code decides"* is an **invariant, not a discipline**.
The model's entire output is an index into a list the code built; it cannot emit a
value, unit, verdict or span, and never sees the absence decision — settled in
`validate.py` first. It cannot erase a gap it is never shown.

**The document tree, and where I left PageIndex behind.** PageIndex builds a tree
from a document and searches it. I took the indexing half — the unit is a natural
section rather than a fixed-size chunk, derived from layout with no model — and
dropped the retrieval half, because they search for content that *exists* and my
hardest case is content that is absent and therefore leaves nothing to retrieve.
So the tree is not a search index here; it is an **address space for absences**,
naming the places a missing item should have been stated so that a gap can be
given a location at all. The ladder is a walk up that chain: `node.walk_up()`
yields the matching subsection, then Methods, then the document, and the first
level holding a usable sentence wins (`extract.py:473`).

---

## 1. Failure analysis

Four failures, covering all three causes named in `01_README.md`: my own code
(§1.1 the validator, §1.2 the extractor), the field specification (§1.3), your
gold label (§1.4).

### 1.1 My validator — a permissive default that discarded perfect locations

The most instructive, because the extractor was already right.

`check_relevance` returned `True` for any field with no marker set, and
`validate_text` / `validate_identifier` read that as `FIELD_OK`. Markers existed
for four fields, so **9 of the 13 gated fields could not produce a finding at
all**, whatever their candidate sentence said — the quantity, enum and sequence
validators never consult `check_relevance`, so the ceiling is the gated 13, not
all 27. Two consequences on wang2015, both located at span
**delta 0** and then thrown away:

- `treatment.vehicle` → `FIELD_OK` on *"Reporter activity (%) is defined as %GFP+
  cells subtracted from background (vehicle controls)."* A vehicle is mentioned;
  it is never composed. Gold: `GAP_ABSENT`.
- `animal.ethics_approval` → `FIELD_OK` on *"All mice were bred and housed in the
  same animal facility."* No approval statement in it at all.

The design error: *"we have no way to check this field"* was encoded as *"this
field is fine"*. That is the unsafe direction — it converts missing coverage into
silent passes rather than visible gaps.

Fixed by writing marker sets from standard domain vocabulary (solvent names,
statistical test names, housekeeping genes, IACUC terms), nothing drawn from
these papers. Coverage 4 → 11 of 13 gated fields; largest single effect measured:
**ABSENT recall 0.077 → 0.231**. Two fields still default permissive
(`assay.protocol_parameters`, `antibody.identifier`) because I could not write an
honest evidence pattern for them. **Cause: my validator.**

**The class, not just the instance.** The same inversion survives in
`validate_quantity`, which I found by looking for it rather than by tripping over
it: any value of the right *dimension* anywhere in the candidate sentence returns
`FIELD_OK`, whether or not it belongs to the field. So Sun's *"cancer cells
suspended in 800 μl PBS"* satisfies `animal.humane_endpoint`, and Guo's *"RIPA
buffer containing 1 nM PMSF"* satisfies `treatment.concentration`. Each one
suppresses a gap, which is the direction that costs recall silently.

Deliberately not fixed: the honest remedy is a target-position check tying the
value to the field's own noun, which is week-two work in §5. Naming it as one
class — "cannot check" encoded as "is fine", across three validators — is more
useful than three separate entries.

### 1.2 My extractor — ambiguity it cannot resolve

`treatment.duration`, wang2015. Gold: `GAP_VAGUE` @39133 on *"Reporter cells were
assessed after overnight incubation."* We emit `GAP_VAGUE` @37257 on *"Right-brain
hemispheres were fixed in 4% PFA overnight."*

Right field, right code, wrong sentence by 1,876 characters. wang2015's Methods
contains `"overnight"` twice and **both are genuinely vague durations**. Nothing
in the text marks which is "the treatment"; we take the first in document order.

Not a bug to patch — the retrieval layer lacks information it cannot derive.
Choosing between two topically valid candidates is exactly what the closed-set
model ranker exists for, and the clearest argument in this submission for that
component. **Cause: my extractor** (retrieval, not the vagueness layer).

### 1.3 The field specification — a disjunctive pseudo-dimension

`animal.age_or_weight` declares `dimension: time_or_mass`. No single unit check
can validate it, and the pack's own header states the principle this violates:
*"one fact, one value, one type."* ARRIVE 2.0 item 2a is decomposed into four
fields elsewhere; age and weight should be two.

Consequence: we validate with a union of the time and mass patterns, so **either
reading satisfies the field** — weight without age passes, and vice versa. ARRIVE
2a requires both and the pack cannot express it. **Cause: the field
specification.**

### 1.4 The gold label — a verdict/code pair the contract rejects

`07_example_labels.json` records two labels with `verdict: FIELD_UNRESOLVED` and
`code: GAP_ABSENT` (`animal.ethics_approval`, `treatment.vehicle`).
`contract.Finding._verdict_matches_code` raises `ValueError` for exactly that
pair. A contract-compliant system **cannot emit what the gold file records.**

Both rationales are sound — *"Approving body named, protocol number absent"* is
genuinely *partially specified*, which is what `FIELD_UNRESOLVED` means. No
`GapCode` expresses partial specification, so `GAP_ABSENT` was closest. Fix: add
`GAP_INCOMPLETE`, or permit `GAP_ABSENT` under `FIELD_UNRESOLVED`. Scoring impact
nil (`score.py` matches on code), but anyone validating against gold verdicts
chases a phantom bug. **Cause: your gold label.**

---

## 2. Results

| slice | P | R | F1 | n |
|---|---|---|---|---|
| **PRESENT_BUT_UNUSABLE** | **0.250** | **0.200** | **0.222** | 5 |
| ABSENT | 0.200 | 0.231 | 0.214 | 13 |

`model=None`, the shipped configuration. With the model confined to node
selection, ABSENT rises to **0.286** (0.267 / 0.308) — 30 of 101 spans differ and
no code does, so it is a genuine second configuration and the model's remit is
visible in the diff. Four of eighteen gold labels matched, three at **span delta
0**. 101 findings across nine papers; determinism verified byte-for-byte across
separate processes; zero zero-width spans.

**The numbers are low and I will not dress that up.** Gold is one paper and
eighteen labels, so a single label moves F1 by ~0.07. What I would ask you to
weigh instead is §1: each miss is attributed to a stage with a character delta,
which is what makes the next iteration cheap.

**On false positives — your third criterion.** Not computable on the supplied
gold: no label carries a `blocking` flag, so the rate pins at 1.000 whatever the
accuracy (`defects_found.md` D1). On R3 it **is** — 32 of 49 labels are blocking —
and reads **1.000** shipped, 0.964 with the model. No finding lands within the
scorer's 20-character tolerance of a blocking R3 span.

That is the same fact as the 0.000 R3 F1, not an independent one, and the cause
is specific: **25 of the 49 labels (51%) are `assay.protocol_parameters`**, one of
the two fields still lacking relevance markers, and we emit nothing for it on 8 of
9 papers. Half that reference is unreachable by construction — a coverage gap, not
a ranking failure. On volume, the other half of the criterion: **11 findings per
paper**, not 40.

**Does the deterministic layer do real work?** It does all of it except one
choice. `predictions/` is produced with no model at all; the live path made 19
calls across nine papers, index-only, worth +0.072 ABSENT F1. Its cache ships, so
`build_all.py --with-model` reproduces that panel with no API key.

---

## 3. Hardest fields

**`centrifugation.force`** — three failure modes stacked. Right field and code,
anchored 934 characters off. Retrieval yields `"centrifugation"` while Sun writes
*"centrifuging at 15,000 × g"* (no stemming). And Yadav's genuine `30,000 × g` is
stored as `30,000 /H11003g`, an Adobe glyph-name leak, so we report `GAP_ABSENT`
on a paper that states the force. Every layer fails on this one field.

**The four `enum` fields** — `validate_enum` compared a whole sentence against
`enum_values`, which can never be true, so all four emitted `GAP_UNPARSEABLE` on
every paper. `animal.sex` sat at delta 0 and still scored as a false positive
purely from the wrong code. Fixed with a surface-form map plus
absent-on-no-match; my cleanest field result.

**`assay.protocol_parameters`** — requires knowing which readout is load-bearing.
We retrieve the Gene Expression sentence; gold wants the ELISA sentence, because
the ELISA underpins the central claim. A judgement about experimental importance,
not a text-matching problem.

---

## 4. Defects found

Fifteen entries with evidence in `defects_found.md`. Highest-impact four:

1. **A2** — gold ships `anchor_text`, `score.py` requires `span`. **The scorer
   cannot run on the supplied files.** The resolver your notes reference is not in
   the bundle; `resolve_gold_anchors.py` is my replacement.
2. **D0** — `score.py` calls `Path.read_text()` with no `encoding=`, so it crashes
   on Windows against its own output format. One-line fix, invisible on
   Linux/macOS.
3. **A1** — two gold labels use a verdict/code pair the contract rejects (§1.4).
4. **B1** — `centrifugation.force` applies to every wet-lab paper. I proposed a
   step-presence gate, then measured it against gold and **dropped it**: wang2015
   has zero spin mentions and gold labels the field there anyway. The gate would
   have deleted a gold-labelled finding on the paper the field was designed
   around.

Also `A3` an anchor that does not resolve verbatim, `B3` a unit conversion the
build spec forbids elsewhere, `B5` 13 of 27 fields with no `description`, `C1`
`"briefly"` as a false-positive generator.

---

## 5. Plan for three more weeks

**Week 1 — anchoring, where both slices lose.** We emit the right field and code
far more often than we match, because the span lands in the wrong subsection, and
each such miss costs a false positive *and* a false negative. Evidence patterns
for the last two permissive fields, then character normalisation built
**class-based** rather than instance-based: Unicode confusables for `°`, a
generic `/H####` glyph decoder, and for lossy corruption (`µ`→`m`) *detect and
suppress* rather than correct — a value that fails plausibility by three orders
of magnitude in the direction of a lost SI prefix should be withheld, not
reported.

**Week 2 — retrieval, the current ceiling.** The detector record now on every
finding shows **40% of observations come from the unscoped whole-Methods
fallback**, which violates the build spec's own rule that a span sit inside the
region where the field is expected. Build per-field cue lexicons distinct from
`field_keywords` — an absent field's own vocabulary is by definition not in the
text. Wire the ranker to sentence selection as well as node selection (§1.2 is
precisely that case). Add stemming.

**Week 3 — criticality and multiplicity.** Implement the distinction the README
describes but does not require: `culture.temperature` and `centrifugation.force`
are both absent quantities and only one is consequential. Order output by
recoverability rather than emitting a flat list. Then multi-instance findings,
deferred until precision is high enough that multiplying findings is not
multiplying noise.

**Not planned:** a model for anything but ranking. The closed-set contract is the
property worth keeping, and widening its remit trades it for a metric not yet
exhausted deterministically.

---

## 6. Time, and what was cut

**Time spent:** 3-4 days starting September 1st 2026 ending at 4 September 2026

Cut deliberately: supplement and resource-table parsing (named cost —
wang2015's `qpcr.reference_genes` is probably in Table S1); multi-instance
findings; a learned criticality model, since the README says it will look at
output *design* for this; and main-text-Methods-only scope, which makes the gold
`treatment.concentration` label at offset 23286 unreachable — it sits inside
Results. Widening was measured and is worse: before the boundary was fixed, six
of nine papers anchored findings on journal mastheads and bibliography entries.
