# How scoring works

Two scorers exist. **The platform decides which one the network uses**, and
advertises it as `scoring_version` in `/scoring/network-config`. Validators
follow it; no local switch overrides it.

v1 is the fallback: a validator that has never been told otherwise scores with
it. See [Scoring v2](#scoring-v2-difficulty-weighted) for the other.

**Why the platform and not an environment variable.** v1 and v2 are different
scales — several points apart on the same callset — so a round scored partly by
each produces a meaningless ranking, and ranking is what pays. A per-operator
flag guarantees exactly that mixture for as long as the slowest operator takes
to notice a release note.

## Which version a validator uses

Before scoring, the validator fetches `/scoring/network-config` from the
platform (`neurons/validator.py`, `_scoring_version`). It fetches it there
rather than reading the copy cached during round finalization, because that copy
is refreshed *after* the round it would be used for. `utils/scoring_version.py`
`resolve` then decides:

- **The platform advertised `v1` or `v2`** — matched case-insensitively, with
  surrounding whitespace ignored. That version is used and written to the state
  file. If it differs from the one last used, the change is logged as a warning:
  scores either side of it are not comparable.
- **The platform advertised nothing usable** — key missing, value unrecognised,
  or the fetch failed. The validator keeps the version it last used, read back
  from the state file. An unrecognised value is *not* an instruction to use v1.
- **Nothing was ever resolved on this machine** — v1.

The state file is `~/.minos/scoring_version.json` unless
`MINOS_SCORING_VERSION_STATE` points elsewhere. It is written through a temp
file and `os.replace`, so a crash mid-write cannot leave a truncated file that
would read as "never resolved". A validator that cannot write it still scores
the round; it just cannot remember the version across a restart.

Falling back to v1 on a failed fetch would be worse than keeping the last
version: during a v2 rollout, every validator that briefly lost the platform
would drop to v1 and diverge from the fleet precisely when the network is least
able to notice.

Whatever it settles on is cached **for the round**, so no round can be split
across two scales — a change landing mid-round cannot score some miners on one
scale and the rest on another. The next round resolves again, so a change to
`scoring_version` reaches a running validator on its next round rather than
waiting for a restart.

---

## v1 — the live scorer

```
score = 100 x (0.60 x core + 0.15 x completeness + 0.15 x fp + 0.10 x quality)
        - overcall_penalty
```

Component emphasis uses `emphasis(m, gamma) = 1 - (1 - m)^gamma`. A gamma below
1 amplifies small differences; above 1 it saturates them.

| Component | Weight | What it measures |
|---|---|---|
| `core` | 0.60 | SNP and indel F1 averaged by truth count, through `emphasis(m, 0.5)`. |
| `completeness` | 0.15 | Mean of `emphasis(recall, 3.0)` — recall also averaged by truth count — and `emphasis(coverage, 2.0)`, where `coverage = 1 - frac_na`. Both metric producers set `frac_na` to 0.0, so in practice coverage contributes a constant. |
| `fp` | 0.15 | Mean of `exp(-max(0, fp_rate - target) / target)` with `target = max(0.002, 1 / truth total)`, and a call-count term `exp(-abs(calls / truth - 1) / 0.10)`. |
| `quality` | 0.10 | Mean of a ti/tv term and a het/hom term, each `exp(-abs(query - truth) / tolerance)`; tolerance 0.1 for ti/tv, 0.15 for het/hom (SNP and indel het/hom averaged). |

A quality term whose truth-side ratio is usable but whose query-side ratio is
not scores 0, not full marks. The final score is floored at 0 after the overcall
penalty is subtracted.

### The overcall penalty is subtractive and sits outside the 60/15/15/10 table

The four weighted components sum to 100, so the table above looks complete. A
separate guardrail then **subtracts up to 45 points** from the total, which means
your downside is not bounded by the 15% FP component.

```
fp_per_target      = (region SNP FPs + region INDEL FPs) / synthetic truth total
snp_fp_per_target  = region SNP FPs / synthetic SNP truth total

if fp_per_target > 10.0 and snp_fp_per_target > 6.0:
    penalty = min(45.0, (fp_per_target - 10.0) * 4.0)
```

False positives are counted across the whole challenge region, not only at truth
positions. Constants live in `utils/scoring.py` as `OVERCALL_FP_PER_TARGET_MAX`,
`OVERCALL_SNP_FP_PER_TARGET_MAX`, `OVERCALL_PENALTY_SLOPE` and
`OVERCALL_PENALTY_MAX`.

### `MINOS_OVERCALL_STRICT`

With this set, the guardrail fires on `fp_per_target` alone. It is **off by
default**: it changes emitted v1 scores, so it must be set identically on every
validator or honest validators will disagree about the same submission. It moves
only the v1 penalty — v2 does not use `overcall_penalty`, and the
`fp_per_target` its germline term reads is the same number either way.

---

## Scoring v2 — difficulty-weighted

**Status: implemented; used when the platform advertises
`scoring_version: v2`.** See [How a change of version behaves](#how-a-change-of-version-behaves).

v1's core F1 is weighted by truth *count*, so the most numerous variant class
dominates the score. On live rounds that class is SNPs, which are the easiest
part of the callset. v2 weights each class by intrinsic difficulty instead, so
the score is decided by the parts of the truth set that callers actually differ
on.

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
length, zygosity — and must stay that way. A validator scores only its assigned
subset, so weights derived from what it observes would make two honest
validators disagree about the same submission.

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
(1.5). A failed gate scores exactly 0.0. A ratio is judged only when both truth
and query sides are present, so an undefined ratio neither passes nor fails —
and earns nothing.

v2 drops v1's `completeness`, `fp`/`size_pen` and `quality` components.
Plausibility becomes a pass/fail gate rather than points; its bounds are loose
by design, sized to reject a malformed callset rather than to be tuned against.

### What v2 needs before it can score anything

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

**When v2 is selected and returns `None`, the validator skips that miner
entirely.** It does not fall back to v1: the rest of the fleet is on v2 that
round, and one v1 score mixed into that ranking is worse than one missing score.
The miner records no local score for the round.

### How a change of version behaves

- The version is resolved once per round and held, so a change landing
  mid-round cannot score some miners on one scale and the rest on another.
- Within one round the validator refuses to mix scales: if v2 is selected and
  unavailable for a miner, that miner is skipped with an error rather than
  quietly scored on v1. One missing score is better than one incomparable score
  in the ranking.
- A validator that cannot reach the platform keeps its previous version, so a
  change propagates as each one next resolves rather than instantly.
- v2 needs rounds that carry a mutations VCF — see
  [what v2 needs](#what-v2-needs-before-it-can-score-anything). Without one it
  declines to score rather than pricing precision from `core` alone.
- Scores either side of a change are not comparable, because the two scorers
  produce different scales.

### Reading the change in the logs

v1 is computed on every miner regardless of which version is selected, so when
v2 is the selected version both numbers are logged together:

```
score v2=86.4900 (v1=97.0900 delta=-10.6000) miner=5F3sa2TJAWMqDhXG
```

The `(v1=... delta=...)` half is dropped if the v1 score itself could not be
computed. The reverse comparison does not happen at all: when v1 is selected, v2
is never computed and the line reads `score v1=... miner=...` alone, so a
validator running v1 cannot preview what v2 would have scored.

---

## Environment flags

| Setting | Where | Default | Effect |
|---|---|---|---|
| `scoring_version` | **platform**, in `/scoring/network-config` | `v1` | Which scorer the whole network uses. `v1` and `v2` are the only recognised values, matched case-insensitively; anything else leaves each validator on the version it last used. |
| `MINOS_SCORING_VERSION_STATE` | validator, and the miner's local practice preview | `~/.minos/scoring_version.json` | Where the last used version is remembered. |
| `MINOS_OVERCALL_STRICT` | validator | off | Overcall guardrail fires on the total FP rate alone. Fleet-wide flip only; accepts `1`, `true`, `yes`, `on`. |

There is no local switch for the scorer. That is deliberate — see above.
