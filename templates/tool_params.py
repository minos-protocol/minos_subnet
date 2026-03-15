"""
Tool-specific parameter definitions for variant calling templates.

Only includes parameters that affect variant calling quality, not system resources.
Each parameter includes:
- type: data type (int, float, str, bool)
- allowed_values: specific allowed values (for enums) or None for numeric ranges
- min/max: for numeric types
- flag_format: how to format the command-line flag
"""
import re
from datetime import datetime
from typing import Dict, Any

# Regex pattern for valid genomic regions
# Accepts: chr[1-22,X,Y,M]:[digits]-[digits]
# Examples: chr20:10000000-15000000, chrX:100000-200000
REGION_PATTERN = re.compile(r'^chr([1-9]|1[0-9]|2[0-2]|X|Y|M):\d+-\d+$')


def validate_region(region: str) -> Dict[str, Any]:
    """
    Validate genomic region format to prevent command injection.

    SECURITY CRITICAL: This function prevents command injection attacks by ensuring
    the region parameter matches a strict format before being used in shell commands.

    Args:
        region: Genomic region string (e.g., "chr20:10000000-15000000")

    Returns:
        Dict with:
        - valid: bool (True if region is safe to use)
        - error: str or None (error message if invalid)

    Allowed formats:
        - chr1 through chr22
        - chrX, chrY, chrM
        - Coordinates must be positive integers
        - Start position must be less than end position

    Examples:
        >>> validate_region("chr20:10000000-15000000")
        {'valid': True, 'error': None}

        >>> validate_region("chr20:1-1000; rm -rf /")
        {'valid': False, 'error': 'Invalid region format...'}
    """
    # Check basic type and non-empty
    if not region or not isinstance(region, str):
        return {"valid": False, "error": "Region must be a non-empty string"}

    # Prevent excessively long strings (potential DOS)
    if len(region) > 100:
        return {"valid": False, "error": "Region string too long (max 100 characters)"}

    # Check against strict pattern
    if not REGION_PATTERN.match(region):
        return {
            "valid": False,
            "error": f"Invalid region format: '{region}'. Expected format: chr[1-22,X,Y,M]:[start]-[end]"
        }

    # Parse and validate coordinates
    try:
        chrom, coords = region.split(':')
        start_str, end_str = coords.split('-')
        start = int(start_str)
        end = int(end_str)

        # Ensure start < end
        if start >= end:
            return {
                "valid": False,
                "error": f"Invalid coordinates: start ({start}) must be less than end ({end})"
            }

        # Sanity check: region size should be reasonable (< 1GB)
        if end - start > 1_000_000_000:
            return {
                "valid": False,
                "error": f"Region too large: {end - start} bp (max 1 billion bp)"
            }

        # Ensure no negative coordinates
        if start < 0:
            return {"valid": False, "error": "Negative coordinates not allowed"}

    except (ValueError, AttributeError) as e:
        return {
            "valid": False,
            "error": f"Failed to parse region coordinates: {str(e)}"
        }

    # All checks passed
    return {"valid": True, "error": None}


def validate_round_id(round_id: str) -> Dict[str, Any]:
    """
    Validate round_id as a strict ISO-8601 timestamp with timezone.

    SECURITY CRITICAL: round_id is used in directory paths and Docker volume mounts.
    Uses Python's datetime.fromisoformat() for strict structural validation rather
    than a permissive regex, ensuring only valid timestamps are accepted.

    Args:
        round_id: Round identifier string (e.g., "2026-01-21T12:00:00.000000+00:00")

    Returns:
        Dict with:
        - valid: bool
        - error: str or None
    """
    if not round_id or not isinstance(round_id, str):
        return {"valid": False, "error": "round_id must be a non-empty string"}

    if len(round_id) > 40:
        return {"valid": False, "error": "round_id too long (max 40 characters)"}

    try:
        dt = datetime.fromisoformat(round_id)
    except (ValueError, TypeError):
        return {"valid": False, "error": f"round_id is not a valid ISO-8601 timestamp: '{round_id}'"}

    # Must include timezone to prevent ambiguity
    if dt.tzinfo is None:
        return {"valid": False, "error": "round_id must include timezone offset (e.g., +00:00 or Z)"}

    return {"valid": True, "error": None}


GATK_QUALITY_PARAMS = {
    # --- Quality filtering ---

    "min_base_quality_score": {
        "type": "int",
        "min": 0,
        "max": 50,
        "default": 10,
        "flag": "--min-base-quality-score"
    },
    "min_mapping_quality_score": {
        "type": "int",
        "min": 0,
        "max": 60,
        "default": 20,
        "flag": "--minimum-mapping-quality"
    },
    "base_quality_score_threshold": {
        "type": "int",
        "min": 0,
        "max": 50,
        "default": 18,
        "flag": "--base-quality-score-threshold"
    },

    # --- Calling confidence ---

    "standard_min_confidence_threshold_for_calling": {
        "type": "float",
        "min": 0.0,
        "max": 100.0,
        "default": 30.0,
        "flag": "--standard-min-confidence-threshold-for-calling"
    },
    "emit_ref_confidence": {
        "type": "enum",
        "allowed_values": ["NONE", "GVCF", "BP_RESOLUTION"],
        "default": "NONE",
        "flag": "--emit-ref-confidence"
    },

    # --- PCR error model ---

    "pcr_indel_model": {
        "type": "enum",
        "allowed_values": ["NONE", "HOSTILE", "AGGRESSIVE", "CONSERVATIVE"],
        "default": "CONSERVATIVE",
        "flag": "--pcr-indel-model"
    },

    # --- Assembly graph ---

    "min_pruning": {
        "type": "int",
        "min": 1,
        "max": 10,
        "default": 2,
        "flag": "--min-pruning"
    },
    "max_alternate_alleles": {
        "type": "int",
        "min": 1,
        "max": 20,
        "default": 6,
        "flag": "--max-alternate-alleles"
    },
    "min_dangling_branch_length": {
        "type": "int",
        "min": 1,
        "max": 20,
        "default": 4,
        "flag": "--min-dangling-branch-length"
    },
    "recover_all_dangling_branches": {
        "type": "bool",
        "default": False,
        "flag": "--recover-all-dangling-branches"
    },
    "max_num_haplotypes_in_population": {
        "type": "int",
        "min": 8,
        "max": 512,
        "default": 128,
        "flag": "--max-num-haplotypes-in-population"
    },
    "adaptive_pruning_initial_error_rate": {
        "type": "float",
        "min": 0.0001,
        "max": 0.1,
        "default": 0.001,
        "flag": "--adaptive-pruning-initial-error-rate"
    },
    "pruning_lod_threshold": {
        "type": "float",
        "min": 0.5,
        "max": 10.0,
        "default": 2.302585,
        "flag": "--pruning-lod-threshold"
    },

    # --- Active region determination ---

    "active_probability_threshold": {
        "type": "float",
        "min": 0.0001,
        "max": 0.05,
        "default": 0.002,
        "flag": "--active-probability-threshold"
    },
    "min_assembly_region_size": {
        "type": "int",
        "min": 1,
        "max": 300,
        "default": 50,
        "flag": "--min-assembly-region-size"
    },
    "max_assembly_region_size": {
        "type": "int",
        "min": 100,
        "max": 1000,
        "default": 300,
        "flag": "--max-assembly-region-size"
    },
    "assembly_region_padding": {
        "type": "int",
        "min": 0,
        "max": 500,
        "default": 100,
        "flag": "--assembly-region-padding"
    },

    # --- Pair-HMM / likelihood computation ---

    "pair_hmm_gap_continuation_penalty": {
        "type": "int",
        "min": 1,
        "max": 30,
        "default": 10,
        "flag": "--pair-hmm-gap-continuation-penalty"
    },
    "phred_scaled_global_read_mismapping_rate": {
        "type": "int",
        "min": 10,
        "max": 60,
        "default": 45,
        "flag": "--phred-scaled-global-read-mismapping-rate"
    },

    # --- Genotyping priors ---

    "heterozygosity": {
        "type": "float",
        "min": 0.0001,
        "max": 0.01,
        "default": 0.001,
        "flag": "--heterozygosity"
    },
    "indel_heterozygosity": {
        "type": "float",
        "min": 0.00001,
        "max": 0.001,
        "default": 0.000125,
        "flag": "--indel-heterozygosity"
    },
    "sample_ploidy": {
        "type": "int",
        "min": 1,
        "max": 10,
        "default": 2,
        "flag": "--sample-ploidy"
    },
    "contamination_fraction_to_filter": {
        "type": "float",
        "min": 0.0,
        "max": 0.5,
        "default": 0.0,
        "flag": "--contamination-fraction-to-filter"
    },

    # --- Downsampling ---

    "max_reads_per_alignment_start": {
        "type": "int",
        "min": 0,
        "max": 1000,
        "default": 50,
        "flag": "--max-reads-per-alignment-start"
    },

    # --- Read filtering ---

    "dont_use_soft_clipped_bases": {
        "type": "bool",
        "default": False,
        "flag": "--dont-use-soft-clipped-bases"
    },
}


DEEPVARIANT_QUALITY_PARAMS = {
    # Model selection (main quality parameter for DeepVariant)
    "model_type": {
        "type": "enum",
        "allowed_values": ["WGS", "WES", "PACBIO", "HYBRID_PACBIO_ILLUMINA"],
        "default": "WGS",
        "flag": "--model_type"
    },

    # --- make_examples stage: candidate variant thresholds ---

    "vsc_min_fraction_indels": {
        "type": "float",
        "min": 0.0,
        "max": 1.0,
        "default": 0.12,
        "stage": "make_examples"
    },

    "vsc_min_fraction_snps": {
        "type": "float",
        "min": 0.0,
        "max": 1.0,
        "default": 0.12,
        "stage": "make_examples"
    },

    "vsc_min_count_snps": {
        "type": "int",
        "min": 0,
        "max": 50,
        "default": 2,
        "stage": "make_examples"
    },

    "vsc_min_count_indels": {
        "type": "int",
        "min": 0,
        "max": 50,
        "default": 2,
        "stage": "make_examples"
    },

    # --- make_examples stage: read quality filters ---

    "min_mapping_quality": {
        "type": "int",
        "min": 0,
        "max": 60,
        "default": 5,
        "stage": "make_examples"
    },

    "min_base_quality": {
        "type": "int",
        "min": 0,
        "max": 50,
        "default": 10,
        "stage": "make_examples"
    },

    # --- make_examples stage: read processing ---

    "realign_reads": {
        "type": "bool",
        "default": True,
        "stage": "make_examples"
    },

    "normalize_reads": {
        "type": "bool",
        "default": False,
        "stage": "make_examples"
    },

    "keep_duplicates": {
        "type": "bool",
        "default": False,
        "stage": "make_examples"
    },

    "max_reads_per_partition": {
        "type": "int",
        "min": 100,
        "max": 5000,
        "default": 1500,
        "stage": "make_examples"
    },

    # --- make_examples stage: haplotype-aware calling ---

    "sort_by_haplotypes": {
        "type": "bool",
        "default": False,
        "stage": "make_examples"
    },

    "phase_reads": {
        "type": "bool",
        "default": False,
        "stage": "make_examples"
    },

    # --- postprocess_variants stage ---

    "qual_filter": {
        "type": "float",
        "min": 0.0,
        "max": 50.0,
        "default": 1.0,
        "stage": "postprocess_variants"
    },

    "multi_allelic_qual_filter": {
        "type": "float",
        "min": 0.0,
        "max": 50.0,
        "default": 1.0,
        "stage": "postprocess_variants"
    },

    "cnn_homref_call_min_gq": {
        "type": "float",
        "min": 0.0,
        "max": 50.0,
        "default": 20.0,
        "stage": "postprocess_variants"
    },

    "use_multiallelic_model": {
        "type": "bool",
        "default": False,
        "stage": "postprocess_variants"
    },
}


FREEBAYES_QUALITY_PARAMS = {
    # --- Quality filtering ---

    "min_mapping_quality": {
        "type": "int",
        "min": 0,
        "max": 60,
        "default": 1,
        "flag": "--min-mapping-quality"
    },
    "min_base_quality": {
        "type": "int",
        "min": 0,
        "max": 50,
        "default": 1,
        "flag": "--min-base-quality"
    },
    "base_quality_cap": {
        "type": "int",
        "min": 0,
        "max": 60,
        "default": 0,
        "flag": "--base-quality-cap"
    },

    # --- Allele detection thresholds ---

    "min_alternate_fraction": {
        "type": "float",
        "min": 0.0,
        "max": 1.0,
        "default": 0.05,
        "flag": "--min-alternate-fraction"
    },
    "min_alternate_count": {
        "type": "int",
        "min": 1,
        "max": 100,
        "default": 2,
        "flag": "--min-alternate-count"
    },
    "min_alternate_qsum": {
        "type": "int",
        "min": 0,
        "max": 10000,
        "default": 0,
        "flag": "--min-alternate-qsum"
    },

    # --- Coverage ---

    "min_coverage": {
        "type": "int",
        "min": 0,
        "max": 1000,
        "default": 0,
        "flag": "--min-coverage"
    },

    # --- Read filtering ---

    "mismatch_base_quality_threshold": {
        "type": "int",
        "min": 0,
        "max": 60,
        "default": 10,
        "flag": "--mismatch-base-quality-threshold"
    },
    "read_max_mismatch_fraction": {
        "type": "float",
        "min": 0.0,
        "max": 1.0,
        "default": 1.0,
        "flag": "--read-max-mismatch-fraction"
    },

    # --- Genotype likelihood / priors ---

    "theta": {
        "type": "float",
        "min": 0.0,
        "max": 0.1,
        "default": 0.001,
        "flag": "--theta"
    },
    "read_dependence_factor": {
        "type": "float",
        "min": 0.0,
        "max": 1.0,
        "default": 0.9,
        "flag": "--read-dependence-factor"
    },
    "pvar": {
        "type": "float",
        "min": 0.0,
        "max": 1.0,
        "default": 0.0,
        "flag": "--pvar"
    },
    "use_mapping_quality": {
        "type": "bool",
        "default": False,
        "flag": "--use-mapping-quality"
    },
    "harmonic_indel_quality": {
        "type": "bool",
        "default": False,
        "flag": "--harmonic-indel-quality"
    },

    # --- Prior model toggles ---

    "hwe_priors_off": {
        "type": "bool",
        "default": False,
        "flag": "--hwe-priors-off"
    },
    "binomial_obs_priors_off": {
        "type": "bool",
        "default": False,
        "flag": "--binomial-obs-priors-off"
    },
    "allele_balance_priors_off": {
        "type": "bool",
        "default": False,
        "flag": "--allele-balance-priors-off"
    },

    # --- Contamination ---

    "prob_contamination": {
        "type": "float",
        "min": 0.0,
        "max": 1.0,
        "default": 0.0,
        "flag": "--prob-contamination"
    },

    # --- Population genetics ---

    "ploidy": {
        "type": "int",
        "min": 1,
        "max": 10,
        "default": 2,
        "flag": "--ploidy"
    },
    "use_best_n_alleles": {
        "type": "int",
        "min": 0,
        "max": 20,
        "default": 0,
        "flag": "--use-best-n-alleles"
    },

    # --- Haplotype / complex variants ---

    "max_complex_gap": {
        "type": "int",
        "min": 0,
        "max": 100,
        "default": 3,
        "flag": "--max-complex-gap"
    },
    "min_repeat_entropy": {
        "type": "int",
        "min": 0,
        "max": 4,
        "default": 1,
        "flag": "--min-repeat-entropy"
    },
    "min_repeat_size": {
        "type": "int",
        "min": 1,
        "max": 100,
        "default": 5,
        "flag": "--min-repeat-size"
    },

    # --- Algorithm ---

    "genotyping_max_banddepth": {
        "type": "int",
        "min": 1,
        "max": 20,
        "default": 7,
        "flag": "--genotyping-max-banddepth"
    },
}


BCFTOOLS_QUALITY_PARAMS = {
    # --- mpileup: base/mapping quality ---

    "min_MQ": {
        "type": "int",
        "min": 0,
        "max": 60,
        "default": 0,
        "flag_mpileup": "-q",
        "stage": "mpileup"
    },
    "min_BQ": {
        "type": "int",
        "min": 0,
        "max": 50,
        "default": 13,
        "flag_mpileup": "-Q",
        "stage": "mpileup"
    },
    "max_BQ": {
        "type": "int",
        "min": 1,
        "max": 90,
        "default": 60,
        "flag_mpileup": "--max-BQ",
        "stage": "mpileup"
    },
    "delta_BQ": {
        "type": "int",
        "min": 0,
        "max": 99,
        "default": 30,
        "flag_mpileup": "--delta-BQ",
        "stage": "mpileup"
    },
    "adjust_MQ": {
        "type": "int",
        "min": 0,
        "max": 100,
        "default": 50,
        "flag_mpileup": "-C",
        "stage": "mpileup"
    },

    # --- mpileup: depth limits ---

    "max_depth": {
        "type": "int",
        "min": 0,
        "max": 10000,
        "default": 250,
        "flag_mpileup": "-d",
        "stage": "mpileup"
    },
    "max_idepth": {
        "type": "int",
        "min": 1,
        "max": 10000,
        "default": 250,
        "flag_mpileup": "-L",
        "stage": "mpileup"
    },

    # --- mpileup: BAQ (base alignment quality) ---

    "no_BAQ": {
        "type": "bool",
        "default": False,
        "flag_mpileup": "-B",
        "stage": "mpileup"
    },
    "full_BAQ": {
        "type": "bool",
        "default": False,
        "flag_mpileup": "-D",
        "stage": "mpileup"
    },
    "redo_BAQ": {
        "type": "bool",
        "default": False,
        "flag_mpileup": "-E",
        "stage": "mpileup"
    },

    # --- mpileup: indel calling ---

    "open_prob": {
        "type": "int",
        "min": 1,
        "max": 60,
        "default": 40,
        "flag_mpileup": "-o",
        "stage": "mpileup"
    },
    "ext_prob": {
        "type": "int",
        "min": 1,
        "max": 60,
        "default": 20,
        "flag_mpileup": "-e",
        "stage": "mpileup"
    },
    "gap_frac": {
        "type": "float",
        "min": 0.0,
        "max": 1.0,
        "default": 0.002,
        "flag_mpileup": "-F",
        "stage": "mpileup"
    },
    "tandem_qual": {
        "type": "int",
        "min": 0,
        "max": 1000,
        "default": 500,
        "flag_mpileup": "-h",
        "stage": "mpileup"
    },
    "indel_bias": {
        "type": "float",
        "min": 0.1,
        "max": 5.0,
        "default": 1.0,
        "flag_mpileup": "--indel-bias",
        "stage": "mpileup"
    },
    "del_bias": {
        "type": "float",
        "min": 0.1,
        "max": 2.0,
        "default": 1.0,
        "flag_mpileup": "--del-bias",
        "stage": "mpileup"
    },
    "min_ireads": {
        "type": "int",
        "min": 1,
        "max": 100,
        "default": 1,
        "flag_mpileup": "--min-ireads",
        "stage": "mpileup"
    },
    "score_vs_ref": {
        "type": "float",
        "min": 0.0,
        "max": 1.0,
        "default": 0.0,
        "flag_mpileup": "--score-vs-ref",
        "stage": "mpileup"
    },

    # --- mpileup: read filtering ---

    "count_orphans": {
        "type": "bool",
        "default": False,
        "flag_mpileup": "-A",
        "stage": "mpileup"
    },

    # --- call: caller mode ---

    "multiallelic_caller": {
        "type": "bool",
        "default": True,
        "flag_call": "-m",
        "stage": "call"
    },
    "variants_only": {
        "type": "bool",
        "default": True,
        "flag_call": "-v",
        "stage": "call"
    },

    # --- call: genotyping ---

    "ploidy": {
        "type": "enum",
        "allowed_values": ["GRCh37", "GRCh38", "X", "Y", "1", "2"],
        "default": "GRCh38",
        "flag_call": "--ploidy",
        "stage": "call"
    },
    "prior": {
        "type": "float",
        "min": 0.0,
        "max": 1.0,
        "default": 0.0011,
        "flag_call": "-P",
        "stage": "call"
    },
    "pval_threshold": {
        "type": "float",
        "min": 0.0,
        "max": 1.0,
        "default": 0.5,
        "flag_call": "-p",
        "stage": "call"
    },
}


def validate_and_build_flags(tool_name: str, tool_options: dict) -> dict:
    """
    Validate tool options and build command-line flags.

    Args:
        tool_name: One of "gatk", "deepvariant", "freebayes", "bcftools"
        tool_options: Dict of parameter_name: value

    Returns:
        Dict with:
        - valid: bool (True if all params valid)
        - flags: list of command-line flag strings
        - errors: list of validation error messages
    """
    param_definitions = {
        "gatk": GATK_QUALITY_PARAMS,
        "deepvariant": DEEPVARIANT_QUALITY_PARAMS,
        "freebayes": FREEBAYES_QUALITY_PARAMS,
        "bcftools": BCFTOOLS_QUALITY_PARAMS,
    }

    if tool_name not in param_definitions:
        return {"valid": False, "flags": [], "errors": [f"Unknown tool: {tool_name}"]}

    params = param_definitions[tool_name]
    flags = []
    errors = []

    for param_name, param_value in tool_options.items():
        # Check if parameter is allowed
        if param_name not in params:
            errors.append(f"Parameter '{param_name}' not in quality params whitelist")
            continue

        param_def = params[param_name]

        # Validate by type
        if param_def["type"] == "int":
            if not isinstance(param_value, int):
                errors.append(f"Parameter '{param_name}' must be int, got {type(param_value)}")
                continue
            if param_value < param_def["min"] or param_value > param_def["max"]:
                errors.append(f"Parameter '{param_name}' value {param_value} out of range [{param_def['min']}, {param_def['max']}]")
                continue

        elif param_def["type"] == "float":
            if not isinstance(param_value, (int, float)):
                errors.append(f"Parameter '{param_name}' must be float, got {type(param_value)}")
                continue
            if param_value < param_def["min"] or param_value > param_def["max"]:
                errors.append(f"Parameter '{param_name}' value {param_value} out of range [{param_def['min']}, {param_def['max']}]")
                continue

        elif param_def["type"] == "enum":
            if param_value not in param_def["allowed_values"]:
                errors.append(f"Parameter '{param_name}' value '{param_value}' not in allowed values: {param_def['allowed_values']}")
                continue

        elif param_def["type"] == "bool":
            if not isinstance(param_value, bool):
                errors.append(f"Parameter '{param_name}' must be bool, got {type(param_value)}")
                continue

        # Build flag string
        if tool_name == "bcftools":
            # BCFtools has stage-specific flags
            stage = param_def.get("stage", "mpileup")
            if stage == "mpileup":
                flag_key = "flag_mpileup"
            elif stage == "call":
                flag_key = "flag_call"
            else:
                flag_key = "flag"

            if param_def["type"] == "bool":
                if param_value:  # Only add flag if True
                    flags.append({"stage": stage, "flag": param_def[flag_key]})
            else:
                flags.append({"stage": stage, "flag": f"{param_def[flag_key]} {param_value}"})
        elif tool_name == "deepvariant" and param_def.get("stage") in ("make_examples", "postprocess_variants"):
            # DeepVariant extra args: make_examples and postprocess_variants collected separately
            # Convert Python bools to lowercase for absl flags
            value = str(param_value).lower() if isinstance(param_value, bool) else param_value
            flags.append({"stage": param_def["stage"], "param": f"{param_name}={value}"})
        else:
            # Standard flag format
            if param_def["type"] == "bool":
                if param_value:
                    flags.append(param_def["flag"])
            else:
                flags.append(f"{param_def['flag']} {param_value}")

    return {
        "valid": len(errors) == 0,
        "flags": flags,
        "errors": errors
    }
