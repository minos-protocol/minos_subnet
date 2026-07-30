# Minos (SN107) Tool Parameter Ranges

The valid ranges for every tool-configuration parameter the platform accepts.
This is the human-readable companion to the live endpoint, which is the
**source of truth**:

```text
GET https://api.theminos.ai/scoring/parameter-ranges
```

**Always query that endpoint for the up-to-date ranges.** The tables in this doc
are a point-in-time snapshot and may lag the platform — if the two ever disagree,
the endpoint wins. Ranges can change between rounds, so tooling should read them
from the endpoint rather than hard-coding the values below.

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
   read. This accepted set has been public since day one; it keeps configs safe
   to run. Anything not on the list is ignored.
2. **Valid ranges** — every accepted parameter has a valid range (or a set of
   allowed values). **Submitted values must be within these ranges. A value
   outside its range is automatically set to that parameter's default**, so a
   config never fails just because one value was out of range.

Accepted top-level keys: `tool`, `version`, and the tool's `{tool}_options`
object (e.g. `gatk_options`). Local-execution settings are managed by the system
and ignored if submitted: `memory_gb`, `num_threads`, `ref_build`, `threads`,
`timeout`.

---

## GATK HaplotypeCaller (`gatk_options`)

| Parameter | Type | Valid range / allowed values | Default |
|---|---|---|---|
| `min_base_quality_score` | int | 10 – 50 | 10 |
| `min_mapping_quality_score` | int | 0 – 60 | 20 |
| `base_quality_score_threshold` | int | 0 – 50 | 18 |
| `standard_min_confidence_threshold_for_calling` | float | 10.0 – 100.0 | 30.0 |
| `emit_ref_confidence` | enum | `NONE`, `GVCF`, `BP_RESOLUTION` | `NONE` |
| `pcr_indel_model` | enum | `NONE`, `HOSTILE`, `AGGRESSIVE`, `CONSERVATIVE` | `CONSERVATIVE` |
| `min_pruning` | int | 2 – 10 | 2 |
| `max_alternate_alleles` | int | 1 – 20 | 6 |
| `min_dangling_branch_length` | int | 2 – 20 | 4 |
| `recover_all_dangling_branches` | bool | `true` / `false` | `false` |
| `max_num_haplotypes_in_population` | int | 8 – 128 | 128 |
| `adaptive_pruning_initial_error_rate` | float | 0.0001 – 0.1 | 0.001 |
| `pruning_lod_threshold` | float | 0.5 – 10.0 | 2.302585 |
| `active_probability_threshold` | float | 0.001 – 0.05 | 0.002 |
| `min_assembly_region_size` | int | 1 – 300 | 50 |
| `max_assembly_region_size` | int | 100 – 700 | 300 |
| `assembly_region_padding` | int | 0 – 500 | 100 |
| `pair_hmm_gap_continuation_penalty` | int | 1 – 30 | 10 |
| `phred_scaled_global_read_mismapping_rate` | int | 10 – 60 | 45 |
| `heterozygosity` | float | 0.0001 – 0.01 | 0.001 |
| `indel_heterozygosity` | float | 0.00001 – 0.001 | 0.000125 |
| `sample_ploidy` | int | 1 – 10 | 2 |
| `contamination_fraction_to_filter` | float | 0.0 – 0.5 | 0.0 |
| `max_reads_per_alignment_start` | int | 25 – 300 | 50 |
| `dont_use_soft_clipped_bases` | bool | `true` / `false` | `false` |

---

## DeepVariant (`deepvariant_options`)

| Parameter | Type | Valid range / allowed values | Default |
|---|---|---|---|
| `model_type` | enum | `WGS`, `WES`, `PACBIO`, `HYBRID_PACBIO_ILLUMINA` | `WGS` |
| `vsc_min_fraction_indels` | float | 0.0 – 1.0 | 0.12 |
| `vsc_min_fraction_snps` | float | 0.0 – 1.0 | 0.12 |
| `vsc_min_count_snps` | int | 0 – 50 | 2 |
| `vsc_min_count_indels` | int | 0 – 50 | 2 |
| `min_mapping_quality` | int | 0 – 60 | 5 |
| `min_base_quality` | int | 0 – 50 | 10 |
| `realign_reads` | bool | `true` / `false` | `true` |
| `normalize_reads` | bool | `true` / `false` | `false` |
| `keep_duplicates` | bool | `true` / `false` | `false` |
| `max_reads_per_partition` | int | 100 – 5000 | 1500 |
| `sort_by_haplotypes` | bool | `true` / `false` | `false` |
| `phase_reads` | bool | `true` / `false` | `false` |
| `qual_filter` | float | 0.0 – 50.0 | 1.0 |
| `multi_allelic_qual_filter` | float | 0.0 – 50.0 | 1.0 |
| `cnn_homref_call_min_gq` | float | 0.0 – 50.0 | 20.0 |
| `use_multiallelic_model` | bool | `true` / `false` | `false` |

---

## bcftools mpileup (`bcftools_options`)

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
| `multiallelic_caller` | bool | `true` / `false` | `true` |
| `consensus_caller` | bool | `true` / `false` | `false` |
| `variants_only` | bool | `true` / `false` | `true` |
| `ploidy` | enum | `GRCh37`, `GRCh38`, `X`, `Y`, `1`, `2` | `GRCh38` |
| `prior` | float | 0.0 – 1.0 | 0.0011 |
| `pval_threshold` | float | 0.0 – 1.0 | 0.5 |

`prior` applies to the multiallelic caller (`-m`). `pval_threshold` applies to
the consensus caller (`-c`). `indel_bias` and `score_vs_ref` are mpileup
likelihood-model parameters and apply with either caller.

---

## FreeBayes

FreeBayes was deprecated on 2026-05-09 and is no longer accepted — submissions
with `tool: freebayes` are rejected. Use GATK, DeepVariant, or bcftools.

---

*Keep this file in sync with `GET /scoring/parameter-ranges`. The accepted
parameter set itself lives in `templates/tool_params.py`.*
