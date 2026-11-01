# WitnessForge

**Generate the smallest test dataset that provably catches broken data-mapping rules in your ETL pipeline.**

WitnessForge reads a COBOL copybook and a source-to-target mapping specification, generates realistic
faults for every mapping rule, and builds a tiny set of copybook-conformant EBCDIC records
("witnesses") that detects each fault. It then reports, rule by rule, which faults your test data
would catch, and can gate your CI/CD pipeline on that score.

```
$ witnessforge generate examples/claims/claim.cpy examples/claims/mapping.yaml -o out
WitnessForge 0.1.0: 4 witness records detect 47/47 mapping faults (score 100%).
Output written to out/
```

Four records, every fault caught, no production data.

---

## The problem

Teams migrating mainframe data (fixed-width EBCDIC files described by COBOL copybooks) into modern
stores such as JSON documents usually test their transformation jobs in one of two ways:

| Approach | Problem |
|---|---|
| Copy of production data | Millions of records, slow CI runs, high compute cost, and real personal or health data in test environments |
| Hand-made test files | Fast, but nobody knows whether they exercise the tricky cases |

Either way, there is no **evidence of coverage**. A test suite can be green while a broken rule ships,
because no test record happens to trigger it.

Example: `BILLED-AMT PIC S9(7)V99 COMP-3` should become `amounts.billed = 12345.67`. A job that ignores
the implied decimal produces `1234567`. If every test record has an amount of zero, the broken job and
the correct one produce identical output, and the bug ships.

## How it works

1. **Parse** the copybook into a byte-level layout (DISPLAY text, zoned decimal, COMP-3, OCCURS).
2. **Generate mutants**: for every mapping rule, create realistic faulty versions of the pipeline
   (see the operator table below). Each mutant contains exactly one fault in exactly one rule.
3. **Build candidate records** that are valid for the copybook by construction (they are produced by
   the encoder) and valid for the specification (the reference transformation accepts them).
4. **Execute** the reference pipeline and every mutant on every candidate at the byte level. A candidate
   *kills* a mutant when the outputs differ.
5. **Minimize** with greedy set cover: keep the fewest records that kill every killable mutant.
6. **Report and gate**: per-rule mutation score, undetected faults, and a non-zero exit code when the
   score is below your threshold.

Then feed `witnesses.dat` to your real pipeline (AWS Glue, Spark, anything) and run
`witnessforge verify` on its output. Any difference is reported against the mapping rule that owns it.

## Mutation operators

| Operator | Fault it simulates |
|---|---|
| `drop_step` | A transformation step is missing (no strip, no date conversion, no lookup) |
| `case_flip` | `upper` and `lower` swapped |
| `date_swap` | Day and month swapped in the input date format |
| `truncate` | Value cut one character short |
| `scale_loss` | Implied decimal (`V`) ignored, so 123.45 becomes 12345 |
| `sign_loss` | Sign of a signed numeric dropped |
| `offset_shift` | Field read one byte before or after its copybook offset |
| `wrong_source` | Rule reads a neighbouring field of the same type |
| `blank_as_empty` | Blank values become `""` instead of `null` |
| `map_default` | Every code falls through to the lookup default |
| `array_drop_last` / `array_first_only` | OCCURS elements lost |
| `keep_blanks` | Blank OCCURS elements not removed |

A fault that no valid input can expose is reported as *undetected*. It is either an equivalent mutant
(for example, a redundant step) or a sign that the specification has a gap. Both are worth a look.

## Install

```bash
pip install witnessforge          # once published to PyPI
# or from source
git clone https://github.com/skavuri143/witnessforge.git
cd witnessforge
pip install -e ".[dev]"
```

Requires Python 3.9+ and PyYAML.

## Quick start

```bash
# 1. See which faults will be checked
witnessforge mutants examples/claims/claim.cpy examples/claims/mapping.yaml

# 2. Build the witness dataset and coverage report
witnessforge generate examples/claims/claim.cpy examples/claims/mapping.yaml -o out

# 3. Run your real pipeline on out/witnesses.dat, write one JSON document per line, then:
witnessforge verify examples/claims/claim.cpy examples/claims/mapping.yaml \
    --input out/witnesses.dat --actual pipeline-output.jsonl
```

`generate` writes:

| File | Contents |
|---|---|
| `witnesses.dat` | Fixed-length EBCDIC records (RECFM=F), ready to land in S3 or a mainframe dataset |
| `witnesses.jsonl` | The same records, decoded and human-readable |
| `expected.jsonl` | The documents a correct pipeline must produce |
| `report.json` / `report.md` | Mutation score per rule, undetected faults, which faults each record kills |

`verify` output looks like this when a job loses the implied decimal:

```
MISMATCH record 0 amounts.billed (rule R5): expected -2345678.67, got -234567867
MISMATCH record 1 amounts.billed (rule R5): expected -2345678.67, got -234567867
MISMATCH record 2 amounts.billed (rule R5): expected 2345678.67, got 234567867
MISMATCH record 3 amounts.billed (rule R5): expected 2345678.67, got 234567867
FAIL: 4 mismatches; rules involved: R5
```

## Mapping specification

```yaml
name: claim-header
key: R1                     # optional: the rule that identifies a document (see below)
rules:
  - id: R1
    source: CLAIM-ID        # copybook field name
    target: claimId         # dotted JSON path in the output document
    steps: [strip]

  - id: R4
    source: SERVICE-DATE
    target: service.date
    steps:
      - date: {in: "%Y%m%d", out: "%Y-%m-%d"}

  - id: R7
    source: DIAG-CODE       # an OCCURS field becomes a JSON array
    target: diagnoses
    steps: [strip, upper]
    drop_blank: true        # remove blank elements
```

Steps run in order:

| Step | Behaviour |
|---|---|
| `strip`, `rstrip`, `upper`, `lower` | String operations |
| `null_if_blank` | Blank string becomes `null` |
| `number` | Decimal to JSON number (float when the copybook has decimals) |
| `date: {in, out}` | Reformat a date string (`strftime` formats); blank becomes `null` |
| `map: {values, default}` | Code lookup |
| `default: value` | Replace `null` with a value |

**`key`**: when set, every witness record gets a unique value for that rule's field, so pipelines that
upsert by key (for example into DocumentDB) don't collapse witness records, and `verify` matches output
documents by that key instead of by position.

## Using it in CI/CD

```yaml
# GitHub Actions
- run: pip install witnessforge
- run: witnessforge generate mapping/claim.cpy mapping/claim.yaml -o witness --min-score 1.0
- run: ./run-etl-job.sh witness/witnesses.dat > actual.jsonl
- run: witnessforge verify mapping/claim.cpy mapping/claim.yaml --input witness/witnesses.dat --actual actual.jsonl
```

The same two commands work as AWS CodePipeline / CodeBuild steps. Exit codes: `0` pass, `1` score below
threshold or verification mismatch, `2` invalid input.

## Python API

```python
from witnessforge import load_copybook, load_mapping, forge

layout = load_copybook("claim.cpy")
mapping = load_mapping("mapping.yaml", layout)
result = forge(layout, mapping)

print(result.score, len(result.witnesses))
for witness in result.witnesses:
    witness.data        # EBCDIC bytes
    witness.expected    # expected output document
    witness.kills       # mutant ids this record detects
```

## Current limitations

Unsupported constructs fail loudly instead of being misread:

- `REDEFINES`, `OCCURS` on group items, `OCCURS DEPENDING ON`
- Binary (`COMP`, `COMP-4`, `COMP-5`) and floating point (`COMP-1`, `COMP-2`) usage
- Edited pictures (`ZZ9.99`)
- One source field per rule

## Roadmap

- REDEFINES and variable-length records
- Multi-field rules (concatenation, derived values)
- SMT-solver backend for witness search on complex conditional rules
- Optional AI-proposed domain mutants, filtered by a deterministic validity check
- Glue/PySpark adapter that runs `verify` directly against DocumentDB

## Development

```bash
pip install -e ".[dev]"
pytest
```

Contributions are welcome. Please open an issue to discuss larger changes first.

## License

Apache License 2.0. See [LICENSE](LICENSE).

## Citation

If you use WitnessForge in research, please cite it using [CITATION.cff](CITATION.cff).

Copyright 2026 Srikanth Kavuri.
