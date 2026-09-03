# Minos (SN107) Tool Parameter Ranges

The valid ranges for every tool-configuration parameter the platform accepts.

**Source of truth: `templates/tool_params.py`** (`GATK_QUALITY_PARAMS`,
`DEEPVARIANT_QUALITY_PARAMS`, `BCFTOOLS_QUALITY_PARAMS`,
`FREEBAYES_QUALITY_PARAMS`). Those dicts are what `validate_and_build_flags()`
checks your submitted config against, and that check is what decides whether the
validator runs your config at all. The tables below are transcribed from them.

The live endpoint publishes the same set:

```text
GET https://api.theminos.ai/scoring/parameter-ranges
```

If the endpoint and the code ever disagree, the code is the one that runs — a
value the endpoint rejects but the code accepts still executes, and a value the
endpoint accepts but the code rejects still fails the round. Read the endpoint
for changes between releases; verify anything surprising against
`templates/tool_params.py` at the version your validator is running.

---

## How a submitted config is read

A miner submits a tool config shaped like this:

```json
{
  "tool": "gatk",
  "version": "1.0",
  "gatk_options": {
    "standard_min_confidence_threshold_for_calling": 30,
    "pcr_indel_model": "NONE"
  }
}
```

Two things define a valid config:

1. **Accepted parameters** — only the parameters listed below (per tool) are
   accepted. Any other key inside `{tool}_options` is a validation error
   (`Parameter 'x' not in quality params whitelist`) and fails the whole config,
   so a typo'd parameter name costs the round rather than being skipped. The one
   exception is the local-execution settings `memory_gb`, `num_threads`,
   `ref_build`, `threads` and `timeout`, which the miner strips before the config
   is sent (`utils/config_commit.INFRA_PARAMS`). Left unset, these default per
   tool: `timeout` is 1200s for GATK and 1800s for DeepVariant and bcftools, and
   exceeding it fails the round rather than submitting a partial callset. The
   container is given every host core unless `threads` is set. GATK, DeepVariant
   and FreeBayes are also given total host RAM minus 1 GB unless `memory_gb` is
   set (GATK's `-Xmx` is 80% of that); bcftools sets no memory limit. Set them
   explicitly if the box runs anything else.
2. **Valid ranges** — every accepted parameter has a valid range (or a set of
   allowed values), and its declared type is enforced too: an `int` parameter
   given `20.0` is rejected as the wrong type, not coerced.

### Out-of-range values are REJECTED, not clamped

A value outside its range is **not** silently replaced by the default. Plan
around this — an out-of-range value costs you the round:

- `validate_and_build_flags()` records
  `Parameter 'x' value V out of range [min, max]` and returns `valid: False`.
- One bad parameter invalidates the **entire** config, not just that parameter —
  the errors accumulate but `valid` is False if there is even one.
- The template (`templates/gatk.py`, `bcftools.py`, `deepvariant.py`) then
  returns `success: False` with `Invalid <TOOL> parameters: ...` and **never
  launches the caller**. No VCF is produced, so there is no valid positive score
  for that round, and that round does not count toward the 5-of-20 eligibility
  requirement.

Nothing between the `.conf` file and the caller clamps values.
`utils/config_loader.extract_tool_options()` parses `key=value` and coerces types
only; `utils/platform_client.submit_config()` strips the infrastructure params and
sends everything else through unchanged. Enum values are equally strict, with a
single documented exception: `bcftools` `ploidy` accepts the integers `1` and `2`
and normalizes them to the string presets `"1"` / `"2"`, because `.conf` parsing
turns them into ints.

---

## GATK HaplotypeCaller (`gatk_options`)

| Parameter | Type | Valid range / allowed values | Default |
|---|---|---|---|
| `min_base_quality_score` | int | 0 – 50 | 10 |
| `min_mapping_quality_score` | int | 0 – 60 | 20 |
| `base_quality_score_threshold` | int | 0 – 50 | 18 |
| `standard_min_confidence_threshold_for_calling` | float | 0.0 – 100.0 | 30.0 |
| `emit_ref_confidence` | enum | `NONE`, `GVCF`, `BP_RESOLUTION` | `NONE` |
| `pcr_indel_model` | enum | `NONE`, `HOSTILE`, `AGGRESSIVE`, `CONSERVATIVE` | `CONSERVATIVE` |
| `min_pruning` | int | 1 – 10 | 2 |
| `max_alternate_alleles` | int | 1 – 20 | 6 |
| `min_dangling_branch_length` | int | 1 – 20 | 4 |
| `recover_all_dangling_branches` | bool | `true` / `false` | `false` |
| `max_num_haplotypes_in_population` | int | 8 – 512 | 128 |
| `adaptive_pruning_initial_error_rate` | float | 0.0001 – 0.1 | 0.001 |
| `pruning_lod_threshold` | float | 0.5 – 10.0 | 2.302585 |
| `active_probability_threshold` | float | 0.0001 – 0.05 | 0.002 |
| `min_assembly_region_size` | int | 1 – 300 | 50 |
| `max_assembly_region_size` | int | 100 – 1000 | 300 |
| `assembly_region_padding` | int | 0 – 500 | 100 |
| `pair_hmm_gap_continuation_penalty` | int | 1 – 30 | 10 |
| `phred_scaled_global_read_mismapping_rate` | int | 10 – 60 | 45 |
| `heterozygosity` | float | 0.0001 – 0.01 | 0.001 |
| `indel_heterozygosity` | float | 0.00001 – 0.001 | 0.000125 |
| `sample_ploidy` | int | 1 – 10 | 2 |
| `contamination_fraction_to_filter` | float | 0.0 – 0.5 | 0.0 |
| `max_reads_per_alignment_start` | int | 0 – 1000 | 50 |
| `dont_use_soft_clipped_bases` | bool | `true` / `false` | `false` |

---

## DeepVariant (`deepvariant_options`)

`model_type` is a top-level flag. Everything else is forwarded as an extra
argument to the stage named in the last column, so a parameter only takes effect
in that stage.

| Parameter | Type | Valid range / allowed values | Default | Stage |
|---|---|---|---|---|
| `model_type` | enum | `WGS`, `WES`, `PACBIO`, `HYBRID_PACBIO_ILLUMINA` | `WGS` | (top level) |
| `vsc_min_fraction_indels` | float | 0.0 – 1.0 | 0.12 | make_examples |
| `vsc_min_fraction_snps` | float | 0.0 – 1.0 | 0.12 | make_examples |
| `vsc_min_count_snps` | int | 0 – 50 | 2 | make_examples |
| `vsc_min_count_indels` | int | 0 – 50 | 2 | make_examples |
| `min_mapping_quality` | int | 0 – 60 | 5 | make_examples |
| `min_base_quality` | int | 0 – 50 | 10 | make_examples |
| `realign_reads` | bool | `true` / `false` | `true` | make_examples |
| `normalize_reads` | bool | `true` / `false` | `false` | make_examples |
| `keep_duplicates` | bool | `true` / `false` | `false` | make_examples |
| `max_reads_per_partition` | int | 100 – 5000 | 1500 | make_examples |
| `sort_by_haplotypes` | bool | `true` / `false` | `false` | make_examples |
| `phase_reads` | bool | `true` / `false` | `false` | make_examples |
| `qual_filter` | float | 0.0 – 50.0 | 1.0 | postprocess_variants |
| `multi_allelic_qual_filter` | float | 0.0 – 50.0 | 1.0 | postprocess_variants |
| `cnn_homref_call_min_gq` | float | 0.0 – 50.0 | 20.0 | postprocess_variants |
| `use_multiallelic_model` | bool | `true` / `false` | `false` | postprocess_variants |

---

## bcftools (`bcftools_options`)

Split across the two stages of the pipeline: `bcftools mpileup | bcftools call`.
Booleans emit their flag only when `true`.

### mpileup stage

| Parameter | Type | Valid range / allowed values | Default |
|---|---|---|---|
| `min_MQ` | int | 0 – 60 | 0 |
| `min_BQ` | int | 0 – 50 | 13 |
| `max_BQ` | int | 1 – 90 | 60 |
| `delta_BQ` | int | 0 – 99 | 30 |
| `adjust_MQ` | int | 0 – 100 | 50 |
| `max_depth` | int | 0 – 10000 | 250 |
| `max_idepth` | int | 1 – 10000 | 250 |
| `no_BAQ` | bool | `true` / `false` | `false` |
| `full_BAQ` | bool | `true` / `false` | `false` |
| `redo_BAQ` | bool | `true` / `false` | `false` |
| `open_prob` | int | 1 – 60 | 40 |
| `ext_prob` | int | 1 – 60 | 20 |
| `gap_frac` | float | 0.0 – 1.0 | 0.002 |
| `tandem_qual` | int | 0 – 1000 | 500 |
| `indel_bias` | float | 0.1 – 5.0 | 1.0 |
| `del_bias` | float | 0.1 – 2.0 | 1.0 |
| `min_ireads` | int | 1 – 100 | 1 |
| `score_vs_ref` | float | 0.0 – 1.0 | 0.0 |
| `indels_cns` | bool | `true` / `false` | `false` |
| `indel_size` | int | 50 – 150 | 110 |
| `count_orphans` | bool | `true` / `false` | `false` |

### call stage

| Parameter | Type | Valid range / allowed values | Default |
|---|---|---|---|
| `multiallelic_caller` | bool | `true` / `false` | `true` |
| `consensus_caller` | bool | `true` / `false` | `false` |
| `variants_only` | bool | `true` / `false` | `true` |
| `ploidy` | enum | `GRCh37`, `GRCh38`, `X`, `Y`, `1`, `2` | `GRCh38` |
| `prior` | float | 0.0 – 1.0 | 0.0011 |
| `pval_threshold` | float | 0.0 – 1.0 | 0.5 |

`prior`, `indel_bias` and `score_vs_ref` are applied like every other parameter —
they become `-P`, `--indel-bias` and `--score-vs-ref` on the command line.
`multiallelic_caller` and `consensus_caller` are independent booleans, so setting
both emits both `-m` and `-c` and bcftools itself decides; set exactly one.

---

## FreeBayes (`freebayes_options`) — DEPRECATED 2026-05-09

The platform returns HTTP 400 for new `tool: freebayes` submissions. The
parameter set is still defined and still validated, because in-flight
pre-cutover rounds are scored with it; it is listed here for reading those
historical configs, not for tuning.

| Parameter | Type | Valid range / allowed values | Default |
|---|---|---|---|
| `min_mapping_quality` | int | 0 – 60 | 1 |
| `min_base_quality` | int | 0 – 50 | 1 |
| `base_quality_cap` | int | 0 – 60 | 0 |
| `min_alternate_fraction` | float | 0.0 – 1.0 | 0.05 |
| `min_alternate_count` | int | 1 – 100 | 2 |
| `min_alternate_qsum` | int | 0 – 10000 | 0 |
| `min_coverage` | int | 0 – 1000 | 0 |
| `mismatch_base_quality_threshold` | int | 0 – 60 | 10 |
| `read_max_mismatch_fraction` | float | 0.0 – 1.0 | 1.0 |
| `theta` | float | 0.0 – 0.1 | 0.001 |
| `read_dependence_factor` | float | 0.0 – 1.0 | 0.9 |
| `pvar` | float | 0.0 – 1.0 | 0.0 |
| `use_mapping_quality` | bool | `true` / `false` | `false` |
| `harmonic_indel_quality` | bool | `true` / `false` | `false` |
| `hwe_priors_off` | bool | `true` / `false` | `false` |
| `binomial_obs_priors_off` | bool | `true` / `false` | `false` |
| `allele_balance_priors_off` | bool | `true` / `false` | `false` |
| `prob_contamination` | float | 0.0 – 1.0 | 0.0 |
| `ploidy` | int | 1 – 10 | 2 |
| `use_best_n_alleles` | int | 0 – 20 | 0 |
| `max_complex_gap` | int | 0 – 100 | 3 |
| `min_repeat_entropy` | int | 0 – 4 | 1 |
| `min_repeat_size` | int | 1 – 100 | 5 |
| `genotyping_max_banddepth` | int | 1 – 20 | 7 |

---

*Keep this file in sync with `templates/tool_params.py`, which is what the
validator enforces, and with `GET /scoring/parameter-ranges`, which publishes it.*
