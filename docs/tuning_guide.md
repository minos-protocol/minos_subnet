# Minos (SN107) Miner Tuning Guide

Practical guide to maximizing your variant-calling scores on Bittensor Subnet 107.

---

## 1. How Scoring Works

Validators compare your VCF output against a truth VCF using **hap.py** (the GA4GH benchmarking tool). Your raw metrics feed into the **AdvancedScorer**, which produces a score from 0 to 100.

### AdvancedScorer Breakdown

| Component    | Weight | What It Measures                                          |
|--------------|--------|-----------------------------------------------------------|
| Core F1      | 60%    | Truth-weighted F1 score (emphasis gamma = 0.5)            |
| Completeness | 15%    | Recall (emphasis gamma = 3.0) + coverage (gamma = 2.0)    |
| FP Rate      | 15%    | Penalizes false positive rate above a dynamic threshold   |
| Quality      | 10%    | Ti/Tv ratio and Het/Hom ratio deviation penalties         |

**Core F1** dominates. Get your precision and recall right first; everything else is refinement.

**FP Rate** uses a dynamic threshold computed as `max(0.2%, 1 / truth_count)` — so the floor is 0.2% and it widens for small truth sets. The penalty is an exponential decay — exceeding the threshold does not instantly zero your score, but it ramps quickly. Calling too aggressively will hurt you here.

**Completeness** rewards finding more true variants. Over-filtering to reduce FPs will cost you here — it is a balancing act.

**Quality** checks biological plausibility. For WGS data, Ti/Tv should be around 2.0 and Het/Hom around 1.5. Large deviations indicate systematic errors in your calls.

### Round Scores and Winner Weights

The AdvancedScorer outputs a raw score on a 0–100 scale. Validators normalize that to a 0–1 current-round score, so a raw score of 85/100 becomes 0.85. Miner rankings are round-only: each round is a separate challenge, and the current-round score decides the paid ranks for that round.

- A single bad round costs that round, not future rounds.
- There is no historical-score decay or carryover in the current scoring path.
- Eligibility still depends on recent participation: miners must have valid positive scores in at least 10 of the last 20 rounds.
- When reading logs: `Score: 85.00/100` means the current-round score is 0.85 after normalization.

**Before eligibility:** miners receive 0 weight until they have enough recent valid scored rounds. Validators keep the unassigned budget in burn rather than paying ineligible miners.

**After eligibility:** **winner-heavy with pruning dust** among eligible miners — validators burn 87%, give rank #1 10%, and split the remaining 3% across eligible ranks #2 through #10 with ranked decay. Ineligible miners and ranks below the dust cutoff get 0.

Consistency matters as much as peak performance.

### Why Is My Weight 0?

This is the most common question for new miners. There are three distinct causes; check them in this order.

**1. You are not eligible yet (most likely).** Eligibility requires participating in **at least 10 of the last 20 rounds**. With ~20 rounds per day, a fresh miner needs roughly 12 hours of continuous uptime before they can earn any weight, even with perfect scores. During this time you appear in validator logs but receive 0 weight. This is expected.

**2. You are eligible but outside the paid ranks.** Once eligible, the top miner gets the main 10% miner weight and eligible ranks #2 through #10 split the pruning dust. If your current-round score ranks below the paid cutoff, you get 0. The fix is to score better — see Section 4 (Tuning Strategy).

**3. You are submitting but the score is 0.** Causes: wrong reference build, malformed VCF (multi-sample, missing index), tool config rejected by the parameter whitelist, or a Docker error. Check your logs for the line `Score: 0.00/100`. If you see it, the variant call ran but produced no usable output. If you do not see a score line at all, your submission never made it to the scoring phase — check the platform connectivity / round timing.

A useful sanity check: if your logs show `Submitted config for round 2026-XX-XXTXX:XX:XX (variants=N)` with a non-zero N, you are participating. The eligibility counter is what will catch up over time.

### What If I Restart Mid-Round?

Recent participation state is recovered from the platform on startup, so eligibility is not lost during a restart. Already-scored miners in the current round are skipped on the next pass — no double-scoring or wasted compute. There is no manual recovery step needed.

---

## 2. Choosing Your Variant Caller

Three active tools are supported. Pick based on your hardware and willingness to tune.

| Tool                  | Accuracy | Speed   | Tunability | Best For                        |
|-----------------------|----------|---------|------------|---------------------------------|
| GATK HaplotypeCaller  | Highest  | Slowest | Most knobs | Maximizing score, if you have time to tune |
| DeepVariant           | High     | Medium  | Few knobs  | Consistent high scores with minimal tuning |
| bcftools mpileup      | Lower    | Fastest | Moderate   | Quick testing, low-resource setups |

> **FreeBayes was deprecated on 2026-05-09 16:00 UTC.** The platform rejects new freebayes submissions; switch to GATK, DeepVariant, or BCFtools.

**Recommendation:** Start with DeepVariant if you want reliable scores fast. Move to GATK if you want to squeeze out every point and are willing to iterate.

---

## 3. Key Parameters for Each Tool

Only quality-related parameters are exposed. Infrastructure params (threads, memory) are handled by the system and stripped before submission.

> **Full accepted-parameter ranges:** see [parameter_ranges.md](parameter_ranges.md) or the live endpoint `GET https://api.theminos.ai/scoring/parameter-ranges`. Every parameter has a valid range; a value outside its range is automatically set to that parameter's default, so a config never fails just because one value was out of range. The ranges below reflect those valid ranges.

### GATK HaplotypeCaller

| Parameter | Default | Range | Effect |
|-----------|---------|-------|--------|
| `standard_min_confidence_threshold_for_calling` | 30 | 10-100 | Higher = fewer, more confident calls. Raising this reduces FPs but hurts recall. |
| `min_base_quality_score` | 10 | 10-50 | Filters bases below this quality. Too high kills soft evidence for real variants. |
| `pcr_indel_model` | CONSERVATIVE | NONE, HOSTILE, AGGRESSIVE, CONSERVATIVE | Use **NONE** for PCR-free libraries (GIAB donors are PCR-free). |

**Tuning priority:** Set `pcr_indel_model=NONE` first. Then adjust `standard_min_confidence_threshold_for_calling` between 20-40 to find your sweet spot between precision and recall.

### DeepVariant

| Parameter             | Default | Effect                                                                |
|-----------------------|---------|-----------------------------------------------------------------------|
| `model_type`          | WGS     | Must be **WGS** for Minos BAMs. Using WES will produce wrong results. |
| `min_mapping_quality` | 5       | Filters reads with mapping quality below this.                        |

DeepVariant is intentionally less tunable — it relies on its trained model. Your main lever is `min_mapping_quality`. The defaults are generally strong.

### FreeBayes (DEPRECATED 2026-05-09 16:00 UTC)

> **No longer accepted by the platform.** New submissions with `tool_name=freebayes`
> return HTTP 400. The reference below is retained for historical context only.

| Parameter                | Default | Effect                                                                         |
|--------------------------|---------|--------------------------------------------------------------------------------|
| `min_alternate_fraction` | 0.05    | Minimum variant allele frequency to call. Lower = more sensitive but more FPs. |
| `min_mapping_quality`    | 1       | Filter poorly mapped reads. Consider raising to 10-20.                         |

### bcftools mpileup

| Parameter | Default | Effect |
|-----------|---------|--------|
| `min_MQ` | 0 | Mapping quality filter. Raise to 10-20 for cleaner calls. |
| `min_BQ` | 13 | Base quality filter. |

bcftools is fast but generally scores lower. Good for testing your pipeline before switching to a more competitive caller.

---

## 4. Tuning Strategy

### Step 1: Start with Defaults

The defaults are tuned for GIAB-style WGS data, which is exactly what Minos uses. Run a few rounds with stock settings to establish your baseline.

### Step 2: Read Your Scores

Look at which component is dragging you down:

- **Low Core F1?** Your precision or recall is off. Check base quality and confidence thresholds.
- **Low Completeness?** You are filtering too aggressively. Lower your quality thresholds.
- **High FP penalty?** You are calling too many false variants. Raise confidence thresholds or mapping quality filters.
- **Low Quality score?** Your Ti/Tv or Het/Hom ratios are off. This usually indicates systematic issues — check that your model_type and PCR settings are correct before touching other params.

### Step 3: Adjust One Parameter at a Time

Change one parameter, run a round, compare. Changing multiple parameters at once makes it impossible to know what helped.

### Step 4: Watch the FP Threshold

The FP rate penalty ramps steeply once you exceed the dynamic threshold. If your FP component is low, prioritize reducing false positives over marginal recall improvements. The 15% FP weight makes this penalty expensive.

### Step 5: Test Locally First

Use **demo mode** (`--demo`) for a one-shot check before committing to live rounds — it downloads a single fixed, fully-answered sample (BAM + truth), runs your variant caller, and prints the exact score a validator would compute.

To score against **any** sample (not just the fixed demo one), use **practice mode** (`--practice`): pick from a menu of fully-answered chr20/chr21 samples and compare configs before going live. A bad config can cost the current round and may fail to count toward eligibility if it produces no valid positive score.

### The Core Tradeoff

```
More aggressive calling → Higher recall → Better completeness
                        → More false positives → Worse FP penalty

More conservative calling → Higher precision → Better FP rate
                          → Lower recall → Worse completeness
```

The scoring weights (60% F1, 15% completeness, 15% FP) mean you want to lean slightly toward precision, but not so far that completeness drops significantly.

---

## 5. Score Ranges

| Range   | Interpretation                                                    |
|---------|-------------------------------------------------------------------|
| 80-95+  | Competitive. You are in contention for the top paid ranks.        |
| 60-80   | Solid baseline. Targeted parameter tuning can push you higher.    |
| 40-60   | Something is suboptimal. Check tool parameters and Docker image.  |
| Below 40| Likely a configuration error. See Common Mistakes below.          |

The genomic region (chr20, 5MB windows from 10MB-55MB) varies per round, so some score variance is normal. Focus on your current-round score trend and consistency across regions.

---

## 6. Common Mistakes

**Wrong model_type in DeepVariant.** Using `WES` instead of `WGS` will silently produce worse calls. Minos BAMs are whole-genome. Always use `WGS`.

**Over-filtering.** Setting `min_base_quality_score` to 30+ or `standard_min_confidence_threshold_for_calling` to 50+ will kill your recall. The completeness penalty (15%) will eat your score. Start conservative with thresholds and only raise them if your FP rate is clearly too high.

**Not using PCR-free mode for GATK.** GIAB donors use PCR-free library prep. Running GATK with the default `CONSERVATIVE` PCR indel model introduces unnecessary indel filtering. Set `pcr_indel_model=NONE`.

**Timeout from too few threads.** While you cannot control thread count directly (it is an infrastructure parameter), make sure your Docker environment has enough resources. If your job times out before the submission window closes, you get no valid positive score for that round and may miss an eligibility count.

**Ignoring Ti/Tv ratio.** A Ti/Tv ratio significantly below 2.0 for WGS usually means you are calling many false SNPs. Check your quality filters rather than accepting a low Quality component score.

**Changing too many parameters at once.** Because each region is a different challenge, changing several knobs at once makes it hard to tell what helped. Methodical single-parameter tuning is the only reliable approach.

---

## 7. Quick Reference: Config Files

Config files live in your miner's `configs/` directory:

```
configs/gatk.conf
configs/deepvariant.conf
configs/bcftools.conf
configs/freebayes.conf      # DEPRECATED 2026-05-09
```

### Format

```ini
# Lines starting with # are comments
standard_min_confidence_threshold_for_calling=30
min_base_quality_score=10
pcr_indel_model=NONE
```

One key=value pair per line. Only quality parameters are accepted — infrastructure parameters (threads, memory, tmp directories) are managed by the system and will be stripped if included.

### Minimal Competitive Configs

**GATK (recommended starting point):**
```ini
standard_min_confidence_threshold_for_calling=30
min_base_quality_score=10
pcr_indel_model=NONE
```

**DeepVariant:**
```ini
model_type=WGS
min_mapping_quality=10
```

> Note: FreeBayes minimal-config example was removed because the tool was deprecated on 2026-05-09 16:00 UTC.


---

## Summary

1. Pick your tool (DeepVariant for ease, GATK for max score).
2. Start with defaults.
3. Set PCR-free mode if using GATK.
4. Tune one parameter at a time, watching your per-component scores.
5. Keep your FP rate low — the penalty ramps steeply past the threshold.
6. Be patient — eligibility takes 10 valid scored rounds, and region-to-region variance is normal.
