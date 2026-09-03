# Defects found in the contract, the field pack, and the supplied labels

`00_START_HERE.md`: *"We wrote the contract and the field pack quickly, and both
have known defects. We found and corrected three defects during the annotation.
There are more. If you find a defect, tell us. That is a good result and not a
complaint."*

This is a running log, added to as things surfaced during implementation. Every
entry carries the evidence that produced it. **Bugs in our own code are not
listed here** — those belong in the failure analysis in `WRITEUP.md`, and mixing
the two would obscure both.

Ordered by how much each one costs a candidate who does not notice it.

---

## A. Contract / label consistency

### A1. Two gold labels use a verdict/code pair the frozen contract rejects

`07_example_labels.json` (and the identical `gold/wang2015_trem2_cell.json`)
contains:

| field | verdict | code |
|---|---|---|
| `animal.ethics_approval` | `FIELD_UNRESOLVED` | `GAP_ABSENT` |
| `treatment.vehicle` | `FIELD_UNRESOLVED` | `GAP_ABSENT` |

`contract.Finding._verdict_matches_code` raises `ValueError` for exactly this
pair — `_VERDICT_CODES["FIELD_UNRESOLVED"]` does not include `GAP_ABSENT`. So a
contract-compliant system **cannot emit** what the gold file records.

Both rationales make the intent clear and both are reasonable readings:
`animal.ethics_approval` is *"Approving body named, protocol number absent"* and
`treatment.vehicle` is *"named as a control but never composed"*. Those are
genuinely "present but incomplete", which is what `FIELD_UNRESOLVED` means. The
gap is that no `GapCode` expresses *partially specified*. `GAP_ABSENT` is the
closest available and it is illegal with that verdict.

**Impact:** low on scoring, because `score.py` matches on `(field_id, code,
span)` and ignores `verdict` — so these rows stay reachable. But it means gold
and contract disagree, and a candidate who validates their output against gold
verdicts will chase a phantom bug.

**Suggested fix:** either add a `GAP_INCOMPLETE` code, or permit `GAP_ABSENT`
under `FIELD_UNRESOLVED` to mean "a required sub-part is absent".

### A2. Gold ships `anchor_text`; `score.py` requires `span`

`score.py:score_document` dereferences `g["span"]` unconditionally, and
`spans_match` reads `a["start"]`. The supplied gold has no `span` key — it
carries `anchor_text`. Running the scorer as documented raises `KeyError`.

`07_example_labels.json` acknowledges this in `pack_coverage_notes`: *"A separate
script resolves anchors to offsets against canonicalized text."* That script is
not in the bundle.

**Impact:** high, and easy to miss. Deliverable 3 is *"a table of scores from
06_score.py on the dev set, with the scores of your model=None ablation"*, and it
cannot be produced from the supplied files without first writing the missing
resolver. A candidate who assumes the scorer runs out of the box has no
self-measurement at all.

### A3. At least one gold anchor does not resolve against canonicalized text

`treatment.concentration` / `GAP_DEFERRED_TO_DISPLAY` anchors on:

> `"plated onto high-absorbance flat-bottom plate coated with various lipids at indicated concentration"`

In `papers/wang2015_trem2_cell.txt` the paper reads `"...flat-bottom plate
coated\nwith various lipids..."` — a newline between `coated` and `with`.
`contract.canonicalize` collapses runs of spaces but **preserves newlines**
(`re.sub(r" *\n", "\n", t)`), so `canonical_text.find(anchor)` returns `-1`.

**Impact:** any naive resolver silently drops this label. We only caught it
because our resolver reported unresolved anchors as a count rather than skipping
them quietly. Recommend the official resolver match on whitespace-insensitive
patterns, and that it fail loudly on a miss.

---

## B. Field pack (`04_oncobiology_v0.yaml`)

### B1. `centrifugation.force` applies to every wet-lab paper, spin or no spin

`applies_when: [has_wet_lab]` with no step-level condition. Measured across the
R3 dev set: **4 of 7 papers (Du, Guo, Shein, Xiong) describe no centrifugation
step anywhere in their methods.** For those the field is applicable, unstated,
and therefore a guaranteed finding — with no subsection that is even the right
place to anchor it.

This is the same expressiveness gap that earned `applies_when_any` in contract
0.2: applicability is being asked to express "this paper performs step X", and
`StudyFeature` only has assay-class granularity.

**Impact:** high. It manufactures one finding per paper on the majority of
papers, in the code family the README says is adoption-critical. This is the
single most costly pack defect we found.

### B2. `animal.age_or_weight` declares a disjunctive pseudo-dimension

`dimension: time_or_mass`. No single unit check can validate it, and the pack's
own header states the compilation principle it violates: *"each published
checklist item has been decomposed into atomic assertions: one fact, one value,
one type."* ARRIVE 2.0 item 2a is decomposed into four fields elsewhere; age and
weight should be two.

**Impact:** moderate. We work around it with a union of the time and mass unit
patterns, which means either reading satisfies the field — so a paper stating
weight but not age passes, and vice versa.

### B3. `culture.temperature` mandates a unit conversion the build spec forbids elsewhere

`dimension: temperature`, `canonical_unit: K`, `plausible_range: ["293.15",
"315.15"]`. Every paper in the corpus states °C. `02_BUILD_SPEC` is emphatic in
the adjacent case: *"You cannot convert rpm to ×g without the radius of the
rotor... A system that converts the value makes a number that was never in the
record."* °C→K is lossless and unambiguous, unlike rpm→×g, so converting is
probably intended — but the policy is nowhere stated, and the pack gives no
signal about which conversions are sanctioned.

**Impact:** low, but it forces a judgement call the spec elsewhere warns against
making silently.

### B4. `stats.replicate_type` and `stats.test_named` apply to computational-only papers

`applies_when: []` means always applicable. `contract.py`'s own comment on
`has_wet_lab` gives the rationale for why that is wrong: *"Without it, purely
computational papers accumulate false gaps on every wet-lab field."* The same
argument applies to reporting fields on a paper with no experiments.

**Impact:** low on this corpus (all nine papers are wet-lab), but it is exactly
the held-out failure mode the comment describes.

### B5. 13 of 27 fields carry no `description`

`animal.sex`, `animal.age_or_weight`, `animal.sample_size_justification`,
`xenograft.cell_number`, `xenograft.injection_site`, `cellline.name`,
`cellline.mycoplasma`, `cellline.passage_number`, `culture.co2_fraction`,
`treatment.concentration`, `qpcr.primer_sequences`, `qpcr.reference_genes`,
`stats.test_named`.

**Impact:** moderate and non-obvious. `description` is the only prose in the pack
explaining what a field means. Where it is absent, the field is a dotted
identifier and a type. Measured: 14 of 42 model prompts (33%) initially carried
no field meaning at all. Mitigated on our side by synthesising a description from
`dimension`, `canonical_unit`, `enum_values` and `provenance`, but that is
recovering information the pack could have stated.

### B6. No field types the supplement deferrals

The pack's own `pack_coverage_notes` says it: *"No field covers the Extended
Experimental Procedures deferrals (IHC, microarray, in vitro assays)."* Recorded
here because it is load-bearing for wang2015, where three method subsections
consist solely of `"For detailed procedures, see Extended Experimental
Procedures."`

---

## C. Vagueness lexicon (`05_vagueness.yaml`)

### C1. `briefly` is a false-positive generator

Listed under `qualitative_quantity`. In methods prose `"Briefly,"` is almost
always a discourse marker introducing a condensed protocol, not a qualitative
substitute for a number.

Measured: it produced a false `GAP_VAGUE` on Yadav `treatment.vehicle` from
`"Briefly, the cells were spun down after treatment, washed with PBS"`.

Note the irony — `"Briefly,"` usually *follows* `"as described previously"`, so it
is a decent **deferral** cue. It is just not a vague quantity. Recommend moving
it, or dropping it.

### C2. The file states a per-field rule the format cannot express

The maintainer note says of `room temperature` and `on ice`: *"Decide per field,
not globally."* And of `according to the manufacturer`: *"not automatically a gap
when the product is identified by catalogue number... This coupling between the
deferral and the identifier field is worth handling."*

Both are correct, and the YAML provides no per-field or conditional hook — the
categories are flat term lists. A consumer must either apply them globally
(wrong, per the note) or invent its own scoping layer outside the file.

**Impact:** moderate. This is the file telling you its own schema is
insufficient for its own stated semantics.

---

## D. Scoring harness (`06_score.py`)

### D0. `Path.read_text()` without an encoding — the scorer crashes on Windows

`score.py:180-181`:

```python
preds = {p.stem: json.loads(p.read_text()) for p in args.pred.glob("*.json")}
golds = {p.stem: json.loads(p.read_text()) for p in args.gold.glob("*.json")}
```

`pathlib.Path.read_text()` with no `encoding=` uses `locale.getencoding()`. On a
Windows install with a cp1252 locale that raises:

```
UnicodeDecodeError: 'charmap' codec can't decode byte 0x81 in position 4937
```

Predictions on this corpus inevitably contain non-Latin-1 characters — `μ`, `–`,
`ª`, `°` all appear in quoted `raw_text`. So the scorer cannot read its own
input format on Windows.

Presumably invisible to the authors, since Linux and macOS default to UTF-8.
Workaround, without touching the frozen file — force UTF-8 mode with a flag,
which needs no environment variable and works in any shell:

```
python -X utf8 score.py --pred predictions/ --gold gold_resolved/ --pack fields/oncobiology_v0.yaml
```

(`PYTHONUTF8=1 python score.py ...` also works, but only in a POSIX shell. In
PowerShell — the default on Windows, which is where this bug bites — the
`VAR=value command` prefix form is not valid syntax and fails with
`CommandNotFoundException` before Python is even invoked. So the flag is the
portable answer.)

**Suggested fix:** `p.read_text(encoding="utf-8")` in both lines. Same for
`args.pack.read_text()` on line 177.

**Impact:** high for any Windows candidate, and confusing — the traceback points
at `pathlib`, not at anything the candidate wrote.

### D1. A correct finding on a non-blocking gap is counted as noise

`false_positive_rate_on_nonblocking` builds `blocking_spans` from labels where
`blocking` is true, then counts every emitted finding not near one of those as
noise. A finding that correctly matches a gold label marked **non-blocking**
therefore counts against you identically to a finding matching nothing at all.

The docstring describes this deliberately (*"correspond to gold labels the
annotator marked non-blocking, or to nothing at all"*), so it is likely
intentional. Recording it because the consequence is strong: the metric cannot
reach 0 for any system that reports non-blocking gaps, and the pack does not mark
which fields are non-blocking, so a candidate cannot suppress them by design.
Worth stating explicitly in the README if intended.

---

## E. Supplied R3 reference annotations — two disagreements

Not spec artifacts, but recorded for traceability since we measured against them.

### E1. Du `has_drug_treatment: true` — the annotator flags their own inconsistency

The note in the file reads: *"The only compounds dosed are the selection
antibiotics G418 and puromycin. I set this true, which conflicts with my call on
the Hosseini paper, where bisulphite and hydroquinone were judged not to be drug
treatment."*

We classify Du as `false`. Selection antibiotic for establishing stable lines is
not a drug-treatment experiment. The annotator's proposed rule — *"a compound
dosed..."* — should be written into the pack, as they suggest.

### E2. Yadav `has_ihc: false` — the methods say otherwise

`papers/Yadav.txt` methods: *"Immunohistochemistry was performed using
anti-..."*. We classify `true`.

---

## Text-fidelity observations (corpus, not spec)

Documented in full in `WRITEUP.md`; summarised here so the log is complete.
These are PDF-extraction artifacts in the supplied `papers/*.txt`, not defects in
the specification.

| class | example | affected |
|---|---|---|
| Degree sign as `U+25E6` WHITE BULLET | `37◦C` | Hosseini (7), Dos et al (15) |
| Adobe Type-1 glyph names leaked as ASCII | `30,000 /H11003g` = `30,000 × g` | Yadav (160 occurrences, 12 distinct tokens) |
| Symbol-font remap: `µ`→`m`, `×`→`3`, `β`→`b`, `©`→`ª` | `"Serial 40-mm coronal sections"` = 40 µm | wang2015 |
| Ligatures `ﬁ`/`ﬂ` | `bisulﬁte`, `Brieﬂy` | Hosseini (64), Sun (124) |
| Letter-space explosion | `N a C l , 0 . 0 5 % T r i t o n - X` | Sun (13 lines), Xiong (9) |
| Running headers/footers interleaved into body text | `Shien et al. Page 3` | 8 of 9 papers |
| Page break splitting a sentence | DOI + footer wedged mid-clause | Dos et al |

The `µ`→`m` case is the dangerous one: it produces a **syntactically valid** value
that is wrong by three orders of magnitude, so it passes unit parsing and then
fails a plausibility check — which would manufacture a `GAP_OUT_OF_RANGE` on a
correctly reported measurement.
