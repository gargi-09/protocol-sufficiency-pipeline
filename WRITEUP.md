# Protocol sufficiency analysis — writeup

**DRAFT.** Two placeholders marked `TODO` need filling before submission: time
spent, and a decision on whether to wire a live model client.

---

## 1. What was built, and the one decision everything follows from

`00_START_HERE.md` states the pivot plainly: *"You must start with a list of the
items that must be present. Then you must check each item against the text. This
direction is the most important design decision in the project."*

The system inverts control accordingly. `analyze.py` iterates
`pack.applicable(profile)` — the checklist drives the loop and the text is
queried per item. Absence is a first-class outcome, not an error: `validate_field(None, spec)`
is a supported call. A pipeline that parses text and reports what it found has a
structural blind spot, because absence emits no tokens; two thirds of the gold
labels are `GAP_ABSENT`, so that architecture caps near zero on the majority
class regardless of extractor quality.

The second requirement — *"the model proposes and the code decides"* — is
implemented as an **invariant rather than a discipline**. The spec states it as a
rule to follow. We make it structural: the model's entire output is an *index
into a list the code built* (`node_select.select_node`). It cannot emit a value,
a unit, a verdict, or a span. Worst case it picks the wrong existing subsection —
a ranking error, never a fabricated fact. `git grep model.complete` returns one
file. The model is also never reached on the absence decision, which is settled
in `validate.py` before selection runs — the structural defence against the
failure the build spec names: *"It tells you that the spin was about 16,000 × g…
The model has now erased the gap."* It cannot erase a gap it is never shown.

Two additions the spec does not ask for:

**A document tree** (`sections.py`), inspired by PageIndex. The indexing half
transfers cleanly: the unit is a natural section, not a fixed chunk, and the
structure comes from layout with no model. The *retrieval* half does not — they
search a tree for content that exists, whereas our hardest case is a field that
is absent and therefore leaves nothing to retrieve. So here the tree is not a
search index; it is an **address space for absences**, naming the places where a
missing item ought to have been stated so a gap can be given a location at all.
The anchor ladder is literally `for level in node.walk_up()`.

**Typed, scoped triggers** (`vagueness.py`), borrowed from NegEx/ConText in
clinical NLP. Their insight is that a trigger defines a *scope*, not a hit.
Applied here it needs a second gate: a trigger also has an implied dimension.
`"overnight"` can fill a duration slot; it cannot fill an antibody-identity slot,
however close it sits. Details in §4.

---

## 2. Results

From `scores.txt` (reproduce: `python resolve_gold_anchors.py` then
`python -X utf8 score.py --pred predictions/ --gold gold_resolved/ --pack fields/oncobiology_v0.yaml`).

| slice | P | R | F1 | n |
|---|---|---|---|---|
| ABSENT | 0.200 | 0.231 | 0.214 | 13 |
| **PRESENT_BUT_UNUSABLE** | **0.250** | **0.200** | **0.222** | 5 |

Four of eighteen gold labels matched, three of them at **span delta 0**:

| field | P | R | F1 | delta |
|---|---|---|---|---|
| `animal.sex` | 1.000 | 1.000 | 1.000 | 0 |
| `treatment.vehicle` | 1.000 | 1.000 | 1.000 | 0 |
| `qpcr.reference_genes` | 1.000 | 1.000 | 1.000 | 0 |
| `treatment.concentration` | 1.000 | 0.500 | 0.667 | 1 |

99 findings across 9 papers. Determinism verified (identical `finding_id`s on
repeat runs); zero zero-width spans.

**The absolute numbers are low and we are not going to dress that up.** Gold is
one paper and eighteen labels — the only labelled paper supplied — so every
figure above rests on a very small denominator. What we would ask you to weigh
instead is the diagnosis in §3: for each miss we can say which stage failed and
by how many characters, which is the thing that makes the next iteration cheap.

**`model=None` is the shipped configuration.** Every prediction in
`predictions/` comes from the deterministic layer alone, so the primary result
and the ablation are the same run. The model path is built, exercised through
`mock_client.py`, and costs 31 calls across all nine papers at ~1.6 kB per
prompt, with a verified zero calls on any repeat run through
`CachedModelClient`. `01_README.md` says the deterministic floor "is not a
failure condition", and we took that at face value. **TODO:** decide whether to
wire a live client before submitting.

**Study classifier:** 51 of 54 feature decisions agree with the supplied R3
annotations (94%). Of the three disagreements, two are cases where we would argue
our answer is better — one of them flagged as inconsistent by the annotator's own
note (see `defects_found.md` E1–E2).

**One metric is not interpretable.** `score.py`'s false-positive rate on
non-blocking findings reads 1.000, but the supplied gold carries no `blocking`
flag, so every label is treated as non-blocking and the metric pins at 1.000
regardless of accuracy. Reporting it as a result would mislead in both
directions. See `defects_found.md` D1.

---

## 3. Failure analysis

Four failures, covering all three causes the README asks for — our extractor
(§3.1, §3.2), our validator (§3.3), the field specification (§3.4), your gold
label (§3.5). Two further notes are included because they were instructive
rather than because they are failures: a fix we measured and dropped (§3.4b), and
a scope decision with a known cost (§3.6).

### 3.1 Our extractor — ambiguity it cannot resolve

`treatment.duration`, wang2015. Gold: `GAP_VAGUE` @39133 on *"Reporter cells
were assessed after overnight incubation."* We emit `GAP_VAGUE` @37257 on
*"Right-brain hemispheres were fixed in 4% PFA overnight."*

Right field, right code, wrong sentence by 1,876 characters. wang2015's Methods
contains `"overnight"` twice and **both are genuinely vague durations** — a PFA
fixation and a reporter incubation. Nothing in the text marks which one is "the
treatment". We take the first occurrence in document order.

This is not a bug to patch; it is the retrieval layer lacking information it
cannot derive. Choosing between two topically valid candidates is exactly the
job the closed-set model ranker exists for, and it is the clearest argument in
the submission for that component. **Cause: our extractor** (specifically,
retrieval, not Layer 2).

### 3.2 Our extractor — a relevance gate that suppresses a real gap

`animal.strain`, wang2015. Gold: `GAP_DEFERRED_TO_REF` @36625 on *"Trem2–/– mice
were generated as previously described."* We emit `GAP_ABSENT` @36820 on *"All
mice were bred and housed in the same animal facility."*

`find_candidate` returns **no candidate at all** for this field. The relevance
markers in `relevance.py` require a strain-shaped token (`BALB/c`, `C57BL`,
`nude`, or a hyphenated designation), and the deferral sentence contains none —
which is the point of the gap. The gate designed to stop wrong-context matches
also blocks the sentence where the strain *should* have been named.

Then the absence path anchors 195 characters away, so we miss on span as well as
code. **Cause: our extractor.** The fix is to treat a deferral cue as sufficient
evidence of relevance for an identifier field — the same insight that made
trigger-driven retrieval work for vagueness (§4).

### 3.3 Our validator — a permissive default that discarded perfect locations

The most instructive failure, because the extractor was already right.

`check_relevance` returns `True` for any field with no marker set, and
`validate_text` / `validate_identifier` read that as `FIELD_OK`. Markers existed
for four fields, so **23 of 27 could not produce a finding at all**, whatever
their candidate sentence said. Two consequences on wang2015, both at span
**delta 0** — located perfectly, then thrown away:

- `treatment.vehicle` → `FIELD_OK` on *"Reporter activity (%) is defined as
  %GFP+ cells subtracted from background (vehicle controls)."* A vehicle is
  mentioned; it is never composed. Gold: `GAP_ABSENT`.
- `animal.ethics_approval` → `FIELD_OK` on *"All mice were bred and housed in the
  same animal facility."* No approval statement in it at all.

The design error is that "we have no way to check this field" was encoded as
"this field is fine". That is the unsafe direction: it converts missing coverage
into silent passes rather than into visible gaps.

Fixed by writing marker sets for the uncovered fields — standard domain
vocabulary (solvent names, statistical test names, housekeeping genes, IACUC
terms), not anything drawn from these papers. Coverage went from 4 to 11 of the
13 gated fields, and the dev-set effect was the largest of any single change:
ABSENT recall 0.077 → 0.231, and both `treatment.vehicle` and
`qpcr.reference_genes` went to 1.000 / 1.000 / 1.000.

**Cause: our validator.** Two fields still default to permissive
(`assay.protocol_parameters`, `antibody.identifier`) because we could not write
an honest evidence pattern for them; that is stated rather than hidden.

### 3.4 The field specification — a disjunctive pseudo-dimension

`animal.age_or_weight` declares `dimension: time_or_mass`. No single unit check
can validate it, and the pack's own header states the principle this violates:
*"each published checklist item has been decomposed into atomic assertions: one
fact, one value, one type."* ARRIVE 2.0 item 2a is decomposed into four fields
elsewhere; age and weight should be two.

The practical consequence is that we validate it with a union of the time and
mass patterns, so **either reading satisfies the field**: a paper stating weight
but never age passes, and vice versa. Both are separately required by ARRIVE 2a,
and the pack cannot express that. **Cause: the field specification.**

### 3.4b A precision fix we proposed, measured, and dropped

Worth recording because the measurement reversed the conclusion.

`centrifugation.force` fires on 9 of 9 papers, and 6 of those describe no
centrifugation step anywhere in their methods. That looked like a clear
applicability defect — a field firing for a step the paper never performs — and
we designed a step-presence gate to suppress it.

Measuring against gold first showed the opposite. wang2015 has **zero** spin
mentions in its Methods, and gold nevertheless labels `centrifugation.force /
GAP_ABSENT` there, with the rationale *"No spin conditions anywhere for the
sequential PBS / Triton-X / guanidine fractionation. The soluble-versus-insoluble
Ab split in Fig 1C-E is operationally defined by that spin."* The pack says the
same: the field was earned partly **by** *"the absence of any spin conditions…
in Wang 2015."*

A paper that does biochemistry and never states its spin conditions is exactly
what the field is for. The gate would have deleted a gold-labelled finding on the
paper the field was designed around. Not implemented. The same reasoning applies
to `stats.replicate_type` firing on all nine papers — the pack states it is
*"present in almost no papers"*, so that is designed behaviour, not noise.

### 3.5 Your gold label — a verdict/code pair the contract rejects

`07_example_labels.json` records two labels with `verdict: FIELD_UNRESOLVED` and
`code: GAP_ABSENT` — `animal.ethics_approval` and `treatment.vehicle`.
`contract.Finding._verdict_matches_code` raises `ValueError` for exactly that
pair. A contract-compliant system **cannot emit what the gold file records.**

Both rationales are reasonable and the intent is clear: *"Approving body named,
protocol number absent"* and *"named as a control but never composed"* are
genuinely *partially specified*, which is what `FIELD_UNRESOLVED` means. The gap
is that no `GapCode` expresses partial specification, so `GAP_ABSENT` was the
closest available. Suggested fix: add `GAP_INCOMPLETE`, or permit `GAP_ABSENT`
under `FIELD_UNRESOLVED`.

Scoring impact is nil, since `score.py` matches on code and ignores verdict — but
a candidate validating output against gold verdicts will chase a phantom bug.
**Cause: your gold label.**

### 3.6 A scope decision, stated rather than hidden

`treatment.concentration` / `GAP_DIMENSION_MISMATCH` is anchored in gold at
offset 23286 — inside **Results**. That is the flagship example
`02_BUILD_SPEC.md` walks through end to end (the LCM dose-response). We declared
main-text-Methods-only scope, and under it this label is unreachable by
construction: `'graded concentrations'` sits 13,291 characters before
`'EXPERIMENTAL PROCEDURES'`.

We are naming it as a scope decision with a known cost rather than leaving it as
an unexplained miss. Widening to the whole document was measured and is worse:
before the Methods boundary was fixed, six of nine papers searched the entire
text and produced findings anchored on journal mastheads and bibliography
entries.

---

## 4. Layer 2: the line between lexicon and classifier

`02_BUILD_SPEC.md` poses this as open: *"A lexicon alone does not find the
phrases that are not in the list. A classifier alone gives findings on correct
qualitative text. To find the line between the two is a real part of this
exercise."*

Our answer is **neither** — it is scope-based trigger propagation, adapted from
NegEx/ConText. A trigger does not flag itself; it defines a bounded region its
meaning applies to. Two gates:

**Type gate.** Each trigger carries the set of slots it can stand in for.
`"overnight"` fills `{time, time_or_mass}`; `"high speed"` fills
`{relative_centrifugal_force}`. A trigger only fires on a field whose declared
dimension or type it could actually occupy. This gate alone removed two measured
false positives: Sun's `antibody.identifier` was reported `GAP_VAGUE` because its
candidate sentence ends *"…incubated overnight at 4 °C"*, and Yadav's
`treatment.vehicle` fired on `"Briefly,"` — a discourse marker, not a quantity.

**Scope gate.** Of the triggers that could fill the slot, only those whose clause
contains the field's own content count. Skipped when the field has no locatable
target, since containment is then undefined and refusing to fire would lose real
gaps.

Two refinements came out of measurement rather than design:

*The two deferral kinds are not alike.* A deferral to a **paper** points at a
procedure — *"the culture was generated as previously described"* defers the
derivation, not a scalar the paper omitted — so it cannot fill a quantity slot.
A deferral to a **display** is a direct slot filler: *"at indicated
concentration"* says the number itself lives in the figure. Your own gold draws
exactly this line: `animal.strain` and `assay.protocol_parameters` are
`GAP_DEFERRED_TO_REF`, while `culture.temperature` and `culture.co2_fraction` —
quantities in a sentence containing *"as previously described"* — are
`GAP_ABSENT`. Encoding that split is what took `PRESENT_BUT_UNUSABLE` off zero.

*Spans go on the clause, not the trigger token.* We first read *"put the span on
the incorrect phrase"* as the trigger itself. Measurement said otherwise: every
present-but-unusable gold label anchors a clause or a full sentence.
`treatment.duration` is anchored 34 characters before `"overnight"` — outside the
20-character tolerance. Narrowing to the token moves *away* from gold. The clause
is also the truer reading of ConText, since the scope *is* the clause. With `or`
added to the boundary set, the clause containing *"at indicated concentration"*
begins at *"plated onto"* — gold's anchor, **delta 1**.

**Trigger-driven retrieval.** Keyword retrieval needs the field's own vocabulary,
and the canonical vagueness cases do not contain it. So the typed table drives
retrieval as well as validation: for a `time` field, any sentence containing a
time-filling trigger is a candidate. Restricted to `GAP_VAGUE` triggers only —
allowing deferrals took `GAP_DEFERRED_TO_REF` from 1 to 27 across nine papers,
because every identifier field with no keyword hit grabbed the first *"as
previously described"* sentence in Methods. A vagueness trigger names its slot; a
deferral is a property of the sentence.

---

## 5. Hardest fields

**`centrifugation.force`** — three independent failure modes stacked. It is
gold-labelled and we emit the right field and code, but anchor 934 characters
away (§3.4b). Retrieval misses the real value because
`field_keywords` yields `"centrifugation"` while Sun writes *"centrifuging at
15,000 × g"* — no stemming. And Yadav's genuine `30,000 × g` is stored as
`30,000 /H11003g`, an Adobe glyph-name leak. Every layer fails on this one field.

**`stats.replicate_type`** — always applicable and, as the pack itself says,
*"present in almost no papers"*. It fires on all nine. We initially read that as
over-firing; on the pack's own account it is correct behaviour (§3.4b).
`stats.test_named` is the opposite case and now resolves correctly on papers that
name a test, once test-name markers existed.

**The four `enum` fields** — `validate_enum` originally compared a whole sentence
against `enum_values`, which can never be true, so all four emitted
`GAP_UNPARSEABLE` on 100% of papers. `animal.sex` was located at **delta 0** and
still scored as a false positive purely from the wrong code. Fixed with a
surface-form map plus absent-on-no-match; that single change produced our
cleanest field result.

**`culture.temperature`** — the pack flags it as almost always absent and almost
always recoverable, and it behaves exactly so. Also the field most exposed to
character corruption: Dos et al states `37◦C` with `U+25E6` WHITE BULLET, which
defeats the `°C` pattern and produces a false `GAP_DIMENSION_MISMATCH` on a
correctly reported value.

**`assay.protocol_parameters`** — requires knowing which readout is load-bearing.
We retrieve the Gene Expression sentence; gold wants the ELISA sentence, because
the ELISA underpins the paper's central claim. That is a judgement about
experimental importance, not a text-matching problem.

---

## 6. Generalization

The held-out set is a different assay class, so we audited what the system's
behaviour actually rests on.

**Zero paper-specific literals in the inference path.** Comments and docstrings
stripped, every inference module grepped for identifiers from these nine papers —
cell lines, genes, vendors, compounds, strains, author names. Result: 0 in
`analyze.py`, `extract.py`, `validate.py`, `sections.py`, `relevance.py`,
`vagueness.py`, `node_select.py`. The only two hits are file paths inside
`classify_study.py`'s `__main__` demo block.

**Ablating every hand-written list changes the dev score by roughly zero.**
Reverting all category-keyword tuning, or dropping the vagueness lexicon
entirely, leaves TP/FP/FN identical. That is the reassuring reading. The honest
counter-reading is that the lists are therefore not carrying measured weight, and
the low score reflects structural gaps rather than missing vocabulary. It is also
a weak test: 18 labels on one paper may simply lack the resolution to detect a
change.

**Three components remain exposed, and we would rather name them than have you
find them:**

- `_NON_DRUG_REAGENT_TOKENS` (`classify_study.py`) — literally reagents observed
  in Hosseini. **It already failed to generalise within the dev set**, missing
  `colcemid` in Dos et al. This is the weakest thing in the repository.
- `_CATEGORY_HEADER_KEYWORDS` (`sections.py`) — ten hand-written keyword lists.
  Already demonstrably incomplete: `"Ex Vivo Microglia Cultures"` did not match
  the `culture` category until `"culture"` was added as a stem.
- The flow-cytometry cues `annexin V` / `propidium iodide` — added because Du
  describes the assay without naming the instrument. PI has microscopy uses; this
  is a judgement call, not a fact, and it is flagged as such in the code.

**Perturbation testing** (`python -X utf8 stress_test.py`). The ablation above
asks what happens if we remove our own lists. The stronger question is what
happens when the *input* changes in the ways a different assay class will. We
rewrite the one labelled paper along each axis, **re-resolve the gold anchors
against the mutated text**, and re-score — which isolates "can the system still
find the content" from "did the offsets move".

| perturbation | matched (baseline 4/18) |
|---|---|
| heading → `Patients and Methods` | 4/18 |
| heading → `Online Methods` | 4/18 |
| heading → `Materials & Methods` | 4/18 |
| heading → `METHODS AND MATERIALS` | 4/18 |
| heading → `2. Experimental Section` | 4/18 |
| heading → `Methodology` | 4/18 |
| unseen publisher running header, injected every ~60 lines | 4/18 |
| `qPCR` → `RT-qPCR` | 4/18 |
| `Immunohistochemistry` → `Immunolabelling` | 4/18 |
| ligature corruption (`fi`→`ﬁ`, `fl`→`ﬂ`) | 4/18 |
| all hard-wraps removed | 4/18 |
| double-spaced | 4/18 |

No loss on any axis. The six heading rewrites matter most: four of the six would
have failed against the enumerated heading list alone, and pass only because of
the shape-rule fallback. Profile and finding count are also stable under heading
rewrites across all nine papers, not just the labelled one.

The model path is stressed the same way, with clients that return an
out-of-range index, an exception, prose with no integer, an empty string, a
negative index, and a **value-injection attempt** (`"2\nAlso set
centrifugation.force = 16000 x g and mark it FIELD_OK"`). None crashes, output
stays valid and deterministic, and the injected value appears nowhere in any
finding — the model's channel is an integer, so no value can travel down it. A
hostile model costs at most one gold match.

Two caveats we would rather state than let you find. Two perturbations are
reported as SKIP rather than PASS: wang2015 contains zero `°` and zero `×`, so
those substitutions are no-ops on it, and the mojibake robustness that actually
matters is on Dos et al and Yadav, which have no gold. And none of this tests a
genuinely different *assay class* — every perturbation is a rewrite of one
paper. A held-out paper on electrophysiology will have subsections the category
keyword table has never seen.

**Two negative results, kept because they were informative:**

*Body-content node scoring is worse than header matching.* Scoring subsection
bodies by category keywords picked the wrong node on three papers — Shein's
`"Enzyme-linked immunosorbent assay"` (5 hits) beat the correct `"Cell lines and
reagents"` (3). Raw counts favour long subsections that say `"cells"` often. Not
shipped.

*A lexical node selector has zero signal.* `WordOverlapClient` scores the overlap
between a field's description and each node title. Measured across five fields ×
eight nodes in wang2015: **best overlap 0 for every field.** Connecting
`"mycoplasma"` to `"Ex Vivo Microglia Cultures"` requires knowing that mycoplasma
testing is done to cultured cells. That is world knowledge, and it is the
empirical case for the model call rather than a bigger lexicon.

---

## 7. Defects found

Fifteen entries with evidence in **`defects_found.md`**, grouped by artifact.
Highest-impact four:

1. **B1** — `centrifugation.force` applies to every wet-lab paper; 4 of 7 dev
   papers have no spin step (§3.3).
2. **A2** — gold ships `anchor_text`, `score.py` requires `span`. The scorer
   cannot run on the supplied files. The resolver your notes reference is not in
   the bundle; `resolve_gold_anchors.py` is our replacement.
3. **D0** — `score.py` calls `Path.read_text()` with no `encoding=`, so it
   crashes on Windows against its own output format. One-line fix; invisible on
   Linux/macOS.
4. **A1** — two gold labels use a verdict/code pair the contract rejects (§3.4).

Also: `A3` an anchor that does not resolve verbatim, `B5` 13 of 27 fields with no
`description`, `C1` `"briefly"` as a false-positive generator, `C2` the lexicon
stating a per-field rule its own schema cannot express.

---

## 8. Plan for three more weeks

**Week 1 — anchoring, which is where both slices now lose.** We emit the right
field and code far more often than we match, because the span lands in the wrong
subsection: `centrifugation.force` on wang2015 is 934 characters off, and each
such miss costs a false positive *and* a false negative. Write evidence patterns
for the last two permissive fields, then add a character-normalisation pass, built
**class-based** rather than instance-based: Unicode confusables for `°`, a
generic Adobe glyph-name decoder for `/H####`, and for lossy corruption
(`µ`→`m`) *detect and suppress* rather than correct — a value that parses but
fails plausibility by three orders of magnitude in the direction of a lost SI
prefix should be withheld, not reported as out-of-range.

**Week 2 — retrieval, the current ceiling.** Three of the four remaining
present-but-unusable misses are retrieval, not Layer 2 (§3.1, §3.2). Build
per-field cue lexicons distinct from `field_keywords`, since an absent field's
own vocabulary is by definition not in the text. Wire the closed-set model ranker
to sentence selection as well as node selection — §3.1 is precisely the
two-valid-candidates case it exists for. Add stemming so `"centrifuging"` reaches
a `"centrifugation"` field.

**Week 3 — criticality and multiplicity.** Implement the distinction the README
describes but does not require: `culture.temperature` and `centrifugation.force`
are both absent quantities, and only one is consequential. Order output by
recoverability rather than emitting a flat list. Then multi-instance findings per
field (four `treatment.concentration` gaps for four compounds), which is
deliberately deferred until precision is high enough that multiplying findings is
not multiplying noise.

**Not planned, and why:** a live model for anything but ranking. The closed-set
contract is the property we want to keep, and widening the model's remit
sacrifices it for a metric we have not yet exhausted deterministically.

---

## 9. Time, and what was cut

**TODO — time spent:** _[fill in: total hours, and roughly how they split across
architecture, debugging text fidelity, and measurement]_

Deliberately not built, each a scope decision rather than an oversight:

- **A live model run.** `model=None` is shipped. The path is built and mocked;
  the README says the deterministic floor is not a failure condition.
- **Supplement / resource-table parsing.** Declared out of scope. Named cost:
  wang2015's `qpcr.reference_genes` is probably in Table S1, and three of its
  method subsections consist solely of a pointer to Extended Experimental
  Procedures.
- **Multi-instance findings per field.** Would multiply the current
  false-positive rate by N.
- **A learned criticality model.** The README says it will look at our *output
  design* for this, not at a model. Ordering by `sensitivity` is the intended
  scope and remains week 3.
- **Character normalisation.** Worth an estimated 1.5 findings; deferred once
  range validators landed without needing it. Artifact classes are catalogued in
  `defects_found.md`.
