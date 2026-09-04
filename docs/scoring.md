# How scoring works

Two scoring versions exist, v1 and v2. The active version is published in
network configuration — `scoring_version` in `/scoring/network-config` — and
applied consistently by every validator. It is fixed for each round: a
validator resolves it once when it starts scoring a round and uses it for every
miner in that round.

At the time of writing the published version is `v2`; check
`/scoring/network-config` for the current value.

**Why the version is published centrally.** v1 and v2 are different scales —
several points apart on the same callset — so a round scored partly by each
would not produce a meaningful ranking. Publishing one value that every
validator reads keeps all scores in a round on the same scale.

## How a validator resolves the version

Before scoring a round, the validator fetches `/scoring/network-config`
(`neurons/validator.py`, `_scoring_version`). `utils/scoring_version.py`
`resolve` then decides:

- **Network configuration advertises `v1` or `v2`** — matched
  case-insensitively, with surrounding whitespace ignored. That version is used
  and written to the state file. If it differs from the one last used, the
  change is logged as a warning: scores either side of it are not comparable.
- **Nothing usable was advertised** — key missing, value unrecognised, or the
  fetch failed. The validator keeps the version it last used, read back from
  the state file. An unrecognised value is *not* an instruction to use v1.
- **Nothing was ever resolved on this machine** — v1.

The state file is `~/.minos/scoring_version.json` unless
`MINOS_SCORING_VERSION_STATE` points elsewhere. It is written through a temp
file and `os.replace`, so a crash mid-write cannot leave a truncated file that
would read as "never resolved". A validator that cannot write it still scores
the round; it just cannot remember the version across a restart.

Keeping the last-used version when network configuration cannot be read is
what keeps a validator on the same scale as the rest of the network during a
transient outage.

The resolved version is held **for the round**. The next round resolves again,
so a change to `scoring_version` reaches a running validator at its next round
boundary rather than waiting for a restart, and no single round is scored on
two scales.

---

## v1

```
score = 100 x (0.60 x core + 0.15 x completeness + 0.15 x fp + 0.10 x quality)
        - overcall_penalty
```

Component emphasis uses `emphasis(m, gamma) = 1 - (1 - m)^gamma`. A gamma below
1 amplifies small differences; above 1 it saturates them.

| Component | Weight | What it measures |
|---|---|---|
| `core` | 0.60 | SNP and indel F1 averaged by truth count, through `emphasis(m, 0.5)`. |
| `completeness` | 0.15 | Mean of `emphasis(recall, 3.0)` and `emphasis(coverage, 2.0)`, where `coverage = 1 - frac_na`. |
| `fp` | 0.15 | Mean of `exp(-max(0, fp_rate - target) / target)` with `target = max(0.002, 1 / truth total)`, and a call-count term `exp(-abs(calls / truth - 1) / 0.10)`. |
| `quality` | 0.10 | Mean of a ti/tv term and a het/hom term, each `exp(-abs(query - truth) / tolerance)`; tolerance 0.1 for ti/tv, 0.15 for het/hom (SNP and indel het/hom averaged). |

The final score is floored at 0 after the overcall penalty is subtracted.

### Versions are fixed

Each scoring version is a fixed formula. Refinements are released as a new
version rather than as changes to an existing one, so scores produced under a
given version stay comparable with each other, and the network moves between
versions through the published `scoring_version`.

---

### The overcall guardrail

The v1 overcall penalty is subtracted from the score when a callset emits far
more false positives than there are targets. It fires only when **both** of
these hold:

| Constant | Value | Meaning |
|---|---|---|
| `OVERCALL_FP_PER_TARGET_MAX` | 10.0 | total FP per target must exceed this |
| `OVERCALL_SNP_FP_PER_TARGET_MAX` | 6.0 | SNP FP per target must *also* exceed this |

Setting `MINOS_OVERCALL_STRICT` drops the second condition, so the total alone
triggers it. Once triggered the penalty is:

```text
penalty = min(45.0, (fp_per_target - 10.0) * 4.0)
```

capped at `OVERCALL_PENALTY_MAX` (45.0) with slope `OVERCALL_PENALTY_SLOPE`
(4.0). Because both conditions must hold by default, a callset can be well past
the total threshold and still take no penalty if its SNP FP rate stays under 6.

## v2 — difficulty-weighted

Active when network configuration advertises `scoring_version: v2`.

v1 weights core F1 by truth *count*, so the score is shaped mostly by the most
numerous variant class in a round. v2 weights each variant class by a fixed
difficulty weight instead, so the score reflects performance across classes
rather than being dominated by the largest one.

### The formula

```
score = 100 x (0.70 x core + 0.30 x germline), gated

core     = F1 per class, combined by fixed difficulty weight
germline = exp(-fp_per_target / 8)
gate     = 0 if ti/tv or het/hom is implausible, else 1
```

| Class | Weight |
|---|---|
| `snp_hom` | 0.02 |
| `snp_het` | 0.18 |
| `indel_1bp` | **0.40** |
| `indel_2_3bp` | 0.16 |
| `indel_4_7bp` | 0.10 |
| `indel_8bp` | 0.14 |

Weights are **fixed constants over intrinsic variant properties** — type, indel
length, zygosity. A validator scores only its assigned subset, so weights
derived from what it observes would make two validators disagree about the
same submission; fixed weights keep every validator's result identical.

Class is decided by `abs(len(ref) - len(alt))`: 1, 2-3, 4-7, or 8 and above.
Equal-length alleles — including MNPs — fall through to `snp_hom` / `snp_het`.

Zygosity is taken from the **TRUTH** sample, not from the call: a false negative
has no call to classify. The call's genotype is read only for a record with no
truth genotype, which means a false positive.

A class with no truth variants in the round is dropped and the remaining weights
renormalised — unless the miner emitted calls in that class, in which case it
stays in at F1 0 so those false positives are still priced.

The gate compares the query's SNP ti/tv and SNP het/hom against truth, and fails
on a deviation above `GATE_TITV_MAX_DELTA` (0.6) or `GATE_HETHOM_MAX_DELTA`
(1.5). A failed gate scores exactly 0.0.

An unmeasurable ratio is not a free pass. The rule is:

- **Truth ratio unmeasurable** — the comparison is skipped. That is a property
  of the round, not of the miner.
- **Truth measurable, query not** — the gate **fails**. In v2 the gate is
  pass/fail, so treating an absent query ratio as "skip" would let a callset
  with no homozygous SNP calls bypass the het/hom check entirely.
- **Both measurable** — compared against the threshold above.

Zero, negative, non-finite and non-numeric all count as unmeasurable: a ti/tv of
0 is what an empty or degenerate callset produces, not a real ratio.

v2 has no separate `completeness`, `fp`/`size_pen` or `quality` components.
Plausibility is a pass/fail gate rather than points; its bounds are loose by
design, sized to reject a malformed callset rather than to be tuned against.

### Runtime requirements and failure behaviour under v2

`AdvancedScorer.compute_score_v2` returns `None` — no score at all, not a low
score — unless both of these are available:

- **Per-variant records.** The validator parses the annotated hap.py VCF
  (`happy_<miner>.vcf.gz`) into per-class TP/FN/FP counts. No file, no parsed
  records, no `core`.
- **`fp_per_target` in the metrics.** Only the region-overcall pass produces
  that key, and `HappyScorer.score_vcf` runs that pass only when the round
  supplies a **mutations VCF**. A round scored GIAB-only carries no
  `fp_per_target`, and v2 declines rather than pricing precision from `core`
  alone.

When v2 is the active version and returns `None` for a miner, that miner gets
no score from that validator for the round. There is no fall-back to v1: a v1
number placed in a v2 ranking would be on a different scale, so a missing score
is preferred to an incomparable one.

Validators that cannot reach network configuration keep the version they last
used, so a change to `scoring_version` takes effect for each validator at its
next round boundary.

### Reading the version in the logs

v1 is computed on every miner regardless of which version is active, so when
v2 is active both numbers are logged together:

```
score v2=86.4900 (v1=97.0900 delta=-10.6000) miner=5F3sa2TJAWMqDhXG
```

The `(v1=... delta=...)` half is dropped if the v1 score itself could not be
computed. When v1 is active, v2 is not computed and the line reads
`score v1=... miner=...` alone.

## The hap.py time limit

Applies to both versions. Each hap.py invocation is given 600 seconds. On
timeout the validator records no score for that miner in that round and leaves
it for backfill — the same outcome as a missing RTG SDF, an invalid region, a
failed truth-VCF slice, or a nonzero hap.py exit with no annotated VCF. A very
large callset can therefore go unscored rather than scoring badly.

---

## Settings

| Setting | Where | Effect |
|---|---|---|
| `scoring_version` | network configuration, in `/scoring/network-config` | Which scoring version the network uses. `v1` and `v2` are the only recognised values, matched case-insensitively; anything else leaves each validator on the version it last used, and a validator that has never resolved one uses v1. |
| `MINOS_SCORING_VERSION_STATE` | validator, and the miner's local practice preview | Where the last used version is remembered. Default `~/.minos/scoring_version.json`. |
| `MINOS_OVERCALL_STRICT` | validator | Overcall guardrail fires on the total FP rate alone. Network-wide setting; accepts `1`, `true`, `yes`, `on`. Off by default. |

The scoring version is selected from network configuration rather than a local
setting, so every validator scores on the same version.
