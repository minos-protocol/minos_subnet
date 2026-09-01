"""
VCF scoring utilities using hap.py.

Validates variant calls against ground truth and computes accuracy metrics.
"""

import csv
import gzip
import math
import os
import subprocess
import time
import traceback
from typing import Any, Dict, List, Optional
from pathlib import Path
import logging

from templates.tool_params import validate_region

logger = logging.getLogger(__name__)

# Docker images for scoring tools.
# hap.py pinned by digest for reproducibility; bcftools uses tag (digest removed from registry).
HAPPY_DOCKER_IMAGE = "genonet/hap-py@sha256:03acabe84bbfba35f5a7234129d524c563f5657e1f21150a2ea2797f8e6d05f2"
BCFTOOLS_DOCKER_IMAGE = "quay.io/biocontainers/bcftools:1.20--h8b25389_0"


def subset_bed(source_bed: Path, target_bed: Path, region: str) -> bool:
    """Filter BED file to entries overlapping the target region."""
    try:
        chrom, coords = region.split(":")
        start_str, end_str = coords.split("-")
        start = int(start_str.replace(",", ""))
        end = int(end_str.replace(",", ""))

        target_bed.parent.mkdir(parents=True, exist_ok=True)

        open_func = gzip.open if str(source_bed).endswith('.gz') else open

        entries_written = 0
        with open_func(source_bed, 'rt', encoding='utf-8') as src, \
             target_bed.open('w', encoding='utf-8') as dst:
            for line in src:
                if not line.strip() or line.startswith('#'):
                    continue

                parts = line.rstrip('\n').split('\t')
                if len(parts) < 3:
                    continue

                if parts[0] != chrom:
                    continue

                entry_start = int(parts[1])
                entry_end = int(parts[2])

                if entry_end <= start or entry_start >= end:
                    continue

                dst.write(line)
                entries_written += 1

        logger.info(f"Subset BED: {entries_written} entries in {region}")
        return True

    except Exception as e:
        logger.error(f"BED subset failed: {e}")
        return False


def slice_truth_vcf(source_vcf: Path, target_vcf: Path, region: str) -> bool:
    """Slice truth VCF to region using bcftools."""
    region_check = validate_region(region)
    if not region_check["valid"]:
        logger.error(f"slice_truth_vcf: invalid region '{region}': {region_check['error']}")
        return False

    try:
        source_vcf = Path(source_vcf).resolve()
        target_vcf = Path(target_vcf).resolve()
        target_vcf.parent.mkdir(parents=True, exist_ok=True)

        if not source_vcf.exists():
            logger.error(f"Source VCF not found: {source_vcf}")
            return False

        index_csi = Path(str(source_vcf) + '.csi')
        index_tbi = Path(str(source_vcf) + '.tbi')
        if not index_csi.exists() and not index_tbi.exists():
            logger.warning(f"VCF not indexed, extraction will be slow")

        # Defensive reindex so slicing sees a fresh .tbi. Uses pysam's bundled
        # htslib rather than a `tabix` CLI, which is not guaranteed on PATH.
        try:
            if str(source_vcf).endswith(".gz"):
                import pysam
                pysam.tabix_index(str(source_vcf), preset="vcf", force=True)
        except Exception as e:
            logger.warning(f"defensive reindex skipped for {source_vcf.name} (continuing): {e}")

        source_dir = source_vcf.parent
        target_dir = target_vcf.parent

        if source_dir == target_dir:
            slice_cmd = [
                "docker", "run", "--rm",
                "-v", f"{source_dir}:/data",
                BCFTOOLS_DOCKER_IMAGE,
                "bcftools", "view", "-r", region,
                f"/data/{source_vcf.name}", "-Oz", "-o", f"/data/{target_vcf.name}",
            ]
        else:
            slice_cmd = [
                "docker", "run", "--rm",
                "-v", f"{source_dir}:/source",
                "-v", f"{target_dir}:/target",
                BCFTOOLS_DOCKER_IMAGE,
                "bcftools", "view", "-r", region,
                f"/source/{source_vcf.name}", "-Oz", "-o", f"/target/{target_vcf.name}",
            ]

        logger.info(f"Slicing truth VCF: {region}")
        result = subprocess.run(slice_cmd, capture_output=True, text=True, timeout=300)

        if result.returncode != 0:
            logger.error(f"bcftools failed: {result.stderr}")
            return False

        if not target_vcf.exists():
            logger.error(f"Output not created: {target_vcf}")
            return False

        index_cmd = [
            "docker", "run", "--rm",
            "-v", f"{target_dir}:/data",
            BCFTOOLS_DOCKER_IMAGE,
            "bcftools", "index", f"/data/{target_vcf.name}",
        ]

        result = subprocess.run(index_cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            logger.warning(f"Index failed: {result.stderr}")

        logger.info(f"Created sliced VCF: {target_vcf.name}")
        return True

    except subprocess.TimeoutExpired:
        logger.error("VCF slicing timed out")
        return False
    except Exception as e:
        logger.error(f"VCF slicing failed: {e}")
        return False


def generate_synthetic_regions_bed(truth_vcf: str, output_bed: str, padding: int = 50) -> bool:
    """Extract SYNTHETIC mutation positions from truth VCF and create a BED file.

    Parses the truth VCF for variants with 'SYNTHETIC' in the INFO field
    and writes a BED file with each position padded by ±padding bp.
    This BED can be passed to hap.py as -f (confident regions) to restrict
    scoring to only the synthetic mutation regions.

    Args:
        truth_vcf: Path to the merged truth VCF (GIAB + synthetic)
        output_bed: Path for the output BED file
        padding: Base pairs of padding around each mutation position

    Returns:
        True if BED was created with at least one region, False otherwise
    """
    try:
        truth_path = Path(truth_vcf)
        output_path = Path(output_bed)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        opener = gzip.open if str(truth_path).endswith('.gz') else open
        regions = []

        with opener(truth_path, 'rt') as f:
            for line in f:
                if line.startswith('#'):
                    continue
                parts = line.strip().split('\t')
                if len(parts) < 8:
                    continue
                info = parts[7]
                if 'SYNTHETIC' in info:
                    chrom = parts[0]
                    pos = int(parts[1])
                    ref_len = len(parts[3])
                    # BED is 0-based, half-open
                    start = max(0, pos - 1 - padding)
                    end = pos - 1 + ref_len + padding
                    regions.append((chrom, start, end))

        if not regions:
            logger.warning("No SYNTHETIC variants found in truth VCF")
            return False

        # Sort and merge overlapping regions
        regions.sort()
        merged = [regions[0]]
        for chrom, start, end in regions[1:]:
            prev_chrom, prev_start, prev_end = merged[-1]
            if chrom == prev_chrom and start <= prev_end:
                merged[-1] = (chrom, prev_start, max(end, prev_end))
            else:
                merged.append((chrom, start, end))

        with open(output_path, 'w') as f:
            for chrom, start, end in merged:
                f.write(f"{chrom}\t{start}\t{end}\n")

        logger.info(f"Generated synthetic regions BED: {len(merged)} regions from {len(regions)} mutations")
        return True

    except Exception as e:
        logger.error(f"Failed to generate synthetic regions BED: {e}")
        return False


def generate_challenge_region_bed(region: str, output_bed: str) -> bool:
    """Create a BED file covering the full challenge region."""
    try:
        region_check = validate_region(region)
        if not region_check["valid"]:
            logger.error(f"Invalid challenge region '{region}': {region_check['error']}")
            return False

        chrom, coords = region.split(":")
        start_str, end_str = coords.split("-")
        start = int(start_str.replace(",", ""))
        end = int(end_str.replace(",", ""))

        output_path = Path(output_bed)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w') as f:
            f.write(f"{chrom}\t{max(0, start - 1)}\t{end}\n")

        logger.info(f"Generated full challenge region BED: {region}")
        return True

    except Exception as e:
        logger.error(f"Failed to generate challenge region BED: {e}")
        return False


def compute_synthetic_only_metrics(happy_vcf_path: str, mutations_vcf_path: str,
                                    position_tolerance: int = 10) -> Optional[Dict[str, float]]:
    """Filter hap.py results to only count variants matching the mutations VCF.

    SNPs are matched by exact (chrom, pos, ref, alt). INDELs use position tolerance
    to handle normalization differences.
    """
    try:
        happy_path = Path(happy_vcf_path)
        mutations_path = Path(mutations_vcf_path)
        if not happy_path.exists() or not mutations_path.exists():
            logger.error(f"Missing file: happy={happy_path.exists()}, mutations={mutations_path.exists()}")
            return None

        target_snps = set()
        target_indel_positions = []
        # Counted straight off the mutations VCF, independent of anything the
        # miner produced. The TP+FN totals below cannot serve as a denominator:
        # they come from fuzzy (+/-10bp, non-consuming) matching against hap.py
        # output, so several decomposed query records can match one target and
        # the same variant written differently yields a different total. A rate
        # used in consensus has to have a fixed denominator.
        #
        # The unit is the ALT ALLELE, stated explicitly because a multiallelic
        # record would otherwise count once or N times depending on where you
        # looked.
        target_snp_alleles = 0
        target_indel_alleles = 0
        opener = gzip.open if str(mutations_path).endswith('.gz') else open
        with opener(mutations_path, 'rt') as f:
            for line in f:
                if line.startswith('#'):
                    continue
                parts = line.strip().split('\t')
                if len(parts) < 5:
                    continue
                chrom, pos, ref, alt = parts[0], int(parts[1]), parts[3], parts[4]

                for one_alt in str(alt).split(','):
                    one_alt = one_alt.strip()
                    if not one_alt or one_alt == '.':
                        continue
                    if len(ref) != len(one_alt):
                        target_indel_alleles += 1
                    else:
                        target_snp_alleles += 1

                # Matching structures are built exactly as before: changing how
                # they key would move TP/FP/FN, and those feed v1's live core.
                if len(ref) != len(alt):
                    target_indel_positions.append((chrom, pos))
                else:
                    target_snps.add((chrom, pos, ref, alt))

        logger.info(f"Loaded {len(target_snps)} target SNPs and "
                     f"{len(target_indel_positions)} target INDELs "
                     f"({target_snp_alleles} SNP / {target_indel_alleles} INDEL "
                     f"ALT alleles) from {mutations_path.name}")
        if not target_snps and not target_indel_positions:
            logger.error(f"No target mutations found in {mutations_path.name}")
            return None

        counts = {
            'tp_snp': 0, 'fp_snp': 0, 'fn_snp': 0,
            'tp_indel': 0, 'fp_indel': 0, 'fn_indel': 0,
        }

        def _match_snp(chrom, pos, ref, alt):
            return (chrom, pos, ref, alt) in target_snps

        def _match_indel(chrom, pos):
            return any(chrom == sc and abs(pos - sp) <= position_tolerance
                       for sc, sp in target_indel_positions)

        opener = gzip.open if str(happy_path).endswith('.gz') else open
        with opener(happy_path, 'rt') as f:
            for line in f:
                if line.startswith('#'):
                    continue
                fields = line.strip().split('\t')
                if len(fields) < 11:
                    continue

                chrom, pos, ref, alt = fields[0], int(fields[1]), fields[3], fields[4]
                fmt_keys = fields[8].split(':')
                fmt_truth = dict(zip(fmt_keys, fields[9].split(':')))
                fmt_query = dict(zip(fmt_keys, fields[10].split(':')))

                bd_truth = fmt_truth.get('BD', '.')
                bd_query = fmt_query.get('BD', '.')
                bvt_truth = fmt_truth.get('BVT', '.')
                bvt_query = fmt_query.get('BVT', '.')

                if bd_truth in ('TP', 'FN'):
                    is_snp = bvt_truth == 'SNP'
                    matched = _match_snp(chrom, pos, ref, alt) if is_snp else _match_indel(chrom, pos)
                    if matched:
                        key = f"{'tp' if bd_truth == 'TP' else 'fn'}_{'snp' if is_snp else 'indel'}"
                        counts[key] += 1

                if bd_query == 'FP':
                    is_snp = bvt_query == 'SNP'
                    matched = _match_snp(chrom, pos, ref, alt) if is_snp else _match_indel(chrom, pos)
                    if matched:
                        counts[f"fp_{'snp' if is_snp else 'indel'}"] += 1

        tp_s, fp_s, fn_s = counts['tp_snp'], counts['fp_snp'], counts['fn_snp']
        tp_i, fp_i, fn_i = counts['tp_indel'], counts['fp_indel'], counts['fn_indel']

        def _f1(tp, fp, fn):
            p = tp / (tp + fp) if (tp + fp) > 0 else 0.0
            r = tp / (tp + fn) if (tp + fn) > 0 else 0.0
            return 2 * p * r / (p + r) if (p + r) > 0 else 0.0, p, r

        f1_snp, precision_snp, recall_snp = _f1(tp_s, fp_s, fn_s)
        f1_indel, precision_indel, recall_indel = _f1(tp_i, fp_i, fn_i)

        logger.info(f"Filtered metrics: SNP TP={tp_s} FP={fp_s} FN={fn_s} F1={f1_snp:.4f} | "
                     f"INDEL TP={tp_i} FP={fp_i} FN={fn_i} F1={f1_indel:.4f}")

        return {
            'f1_snp': f1_snp, 'precision_snp': precision_snp, 'recall_snp': recall_snp,
            'f1_indel': f1_indel, 'precision_indel': precision_indel, 'recall_indel': recall_indel,
            'tp_snp': float(tp_s), 'fp_snp': float(fp_s), 'fn_snp': float(fn_s),
            'tp_indel': float(tp_i), 'fp_indel': float(fp_i), 'fn_indel': float(fn_i),
            'truth_total_snp': float(tp_s + fn_s), 'truth_total_indel': float(tp_i + fn_i),
            # Query-independent target counts, for rates that must mean the same
            # thing on every validator. NOT interchangeable with truth_total_*,
            # which is what hap.py assessed and feeds v1's core weighting.
            'target_total_snp': float(target_snp_alleles),
            'target_total_indel': float(target_indel_alleles),
            'query_total_snp': float(tp_s + fp_s), 'query_total_indel': float(tp_i + fp_i),
            'frac_na_snp': 0.0, 'frac_na_indel': 0.0,
            'weighted_f1': 0.7 * f1_snp + 0.3 * f1_indel,
        }

    except Exception as e:
        logger.error(f"Failed to compute filtered metrics: {e}")
        logger.debug(traceback.format_exc())
        return None


# Overcall guardrail thresholds. These decide emitted scores, so they must be
# identical across the fleet.
OVERCALL_FP_PER_TARGET_MAX = 10.0
OVERCALL_SNP_FP_PER_TARGET_MAX = 6.0
OVERCALL_PENALTY_SLOPE = 4.0
OVERCALL_PENALTY_MAX = 45.0


def _env_flag(name: str) -> bool:
    """True when the env var is set to 1/true/yes/on.

    Single spelling of truthiness for every Minos flag; keep new flags on it.
    """
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def overcall_strict_enabled() -> bool:
    """Whether the overcall guardrail fires on total FP rate alone.

    Off by default. It affects only legacy v1 telemetry; emitted v2 consensus
    scores do not use this guardrail.
    """
    return _env_flag("MINOS_OVERCALL_STRICT")


def parse_region_overcall_metrics(happy_vcf_path: str,
                                  synthetic_truth_total: float,
                                  synthetic_snp_truth_total: float,
                                  v1_truth_total: Optional[float] = None,
                                  v1_snp_truth_total: Optional[float] = None
                                  ) -> Optional[Dict[str, float]]:
    """Count full-region query false positives for the overcall guardrail.

    Returns the FP counts plus ``overcall_penalty``, which
    AdvancedScorer.compute_score subtracts from the final score.

    Two denominators, because the two consumers need different things.
    ``synthetic_*`` are target ALT alleles from the mutations VCF: independent
    of what the miner produced, so the ``fp_per_target`` rate v2 reads means the
    same thing on every validator. ``v1_*`` are hap.py-assessed TP+FN, which is
    what the v1 guardrail has always divided by. They default to the target
    counts for callers that predate the split.
    """
    if v1_truth_total is None:
        v1_truth_total = synthetic_truth_total
    if v1_snp_truth_total is None:
        v1_snp_truth_total = synthetic_snp_truth_total
    try:
        vcf_path = Path(happy_vcf_path)
        if not vcf_path.exists():
            logger.warning(f"hap.py VCF not found: {vcf_path}")
            return None

        counts = {
            'region_fp_snp': 0,
            'region_fp_indel': 0,
        }

        opener = gzip.open if str(vcf_path).endswith('.gz') else open
        with opener(vcf_path, 'rt') as f:
            for line in f:
                if line.startswith('#'):
                    continue
                fields = line.strip().split('\t')
                if len(fields) < 11:
                    continue

                fmt_keys = fields[8].split(':')
                fmt_query = dict(zip(fmt_keys, fields[10].split(':')))
                bd_query = fmt_query.get('BD', '.')
                bvt_query = fmt_query.get('BVT', '.')

                if bd_query == 'FP':
                    if bvt_query == 'SNP':
                        counts['region_fp_snp'] += 1
                    elif bvt_query == 'INDEL':
                        counts['region_fp_indel'] += 1

        region_fp_total = counts['region_fp_snp'] + counts['region_fp_indel']
        fp_per_target = region_fp_total / max(float(synthetic_truth_total), 1.0)
        snp_fp_per_target = counts['region_fp_snp'] / max(float(synthetic_snp_truth_total), 1.0)

        # The guardrail decides a v1 score, so it reads the v1 rates.
        v1_fp_per_target = region_fp_total / max(float(v1_truth_total), 1.0)
        v1_snp_fp_per_target = counts['region_fp_snp'] / max(float(v1_snp_truth_total), 1.0)

        over_total = v1_fp_per_target > OVERCALL_FP_PER_TARGET_MAX
        over_snp = v1_snp_fp_per_target > OVERCALL_SNP_FP_PER_TARGET_MAX
        # Strict mode requires only the total rate to be exceeded; the default
        # also requires the SNP rate. Either way the setting must be uniform
        # across the fleet, since it changes emitted v1 scores.
        if overcall_strict_enabled():
            triggered = over_total
        else:
            triggered = over_total and over_snp
        if triggered:
            overcall_penalty = min(
                OVERCALL_PENALTY_MAX,
                (v1_fp_per_target - OVERCALL_FP_PER_TARGET_MAX) * OVERCALL_PENALTY_SLOPE,
            )
        else:
            overcall_penalty = 0.0

        logger.info(f"Overcall guardrail: region_fp={region_fp_total}, "
                    f"fp_per_target={v1_fp_per_target:.2f}, "
                    f"snp_fp_per_target={v1_snp_fp_per_target:.2f}, "
                    f"strict={overcall_strict_enabled()}, "
                    f"penalty={overcall_penalty:.2f}")
        if over_total and not over_snp and not overcall_strict_enabled():
            logger.warning(
                f"Overcall guardrail not triggered: fp_per_target="
                f"{v1_fp_per_target:.2f} over {OVERCALL_FP_PER_TARGET_MAX}, "
                f"snp_fp_per_target={v1_snp_fp_per_target:.2f} under "
                f"{OVERCALL_SNP_FP_PER_TARGET_MAX}. Set MINOS_OVERCALL_STRICT "
                f"to require the total rate alone."
            )

        return {
            'region_fp_snp': float(counts['region_fp_snp']),
            'region_fp_indel': float(counts['region_fp_indel']),
            'region_fp_total': float(region_fp_total),
            # Target-denominated; v2's germline term reads this.
            'fp_per_target': fp_per_target,
            'snp_fp_per_target': snp_fp_per_target,
            'overcall_penalty': overcall_penalty,
        }

    except Exception as e:
        logger.warning(f"Failed to parse region overcall metrics: {e}")
        return None


def parse_happy_vcf_assessed_metrics(happy_vcf_path: str) -> Optional[Dict[str, float]]:
    """Parse hap.py output VCF to compute metrics from assessed variants only.

    hap.py's summary.csv reports QUERY.TOTAL, Frac_NA, TiTv_ratio, and
    het_hom_ratio over the ENTIRE query VCF, including variants outside
    the -f BED regions (marked UNK). This inflates query_total, tanks
    Frac_NA, and skews ratio calculations.

    This function parses the annotated output VCF and computes these
    metrics from only assessed variants (BD=TP or BD=FP), giving accurate
    values for the AdvancedScorer.

    Args:
        happy_vcf_path: Path to hap.py output .vcf.gz

    Returns:
        Dict with corrected metrics, or None on failure.
    """
    try:
        vcf_path = Path(happy_vcf_path)
        if not vcf_path.exists():
            logger.warning(f"hap.py VCF not found: {vcf_path}")
            return None

        stats = {
            'query_total_snp': 0, 'query_total_indel': 0,
            'ti_query': 0, 'tv_query': 0,
            'ti_truth': 0, 'tv_truth': 0,
            'het_query_snp': 0, 'hom_query_snp': 0,
            'het_truth_snp': 0, 'hom_truth_snp': 0,
            'het_query_indel': 0, 'hom_query_indel': 0,
            'het_truth_indel': 0, 'hom_truth_indel': 0,
        }

        opener = gzip.open if str(vcf_path).endswith('.gz') else open
        with opener(vcf_path, 'rt') as f:
            for line in f:
                if line.startswith('#'):
                    continue
                fields = line.strip().split('\t')
                if len(fields) < 11:
                    continue

                fmt_keys = fields[8].split(':')
                truth_vals = fields[9].split(':')
                query_vals = fields[10].split(':')

                fmt_truth = dict(zip(fmt_keys, truth_vals))
                fmt_query = dict(zip(fmt_keys, query_vals))

                bd_truth = fmt_truth.get('BD', '.')
                bd_query = fmt_query.get('BD', '.')
                bvt_truth = fmt_truth.get('BVT', '.')
                bvt_query = fmt_query.get('BVT', '.')
                bi_truth = fmt_truth.get('BI', '.')
                bi_query = fmt_query.get('BI', '.')
                blt_truth = fmt_truth.get('BLT', '.')
                blt_query = fmt_query.get('BLT', '.')

                # Count query-side assessed variants (TP or FP)
                if bd_query in ('TP', 'FP'):
                    vtype = bvt_query
                    if vtype == 'SNP':
                        stats['query_total_snp'] += 1
                        if bi_query == 'ti':
                            stats['ti_query'] += 1
                        elif bi_query == 'tv':
                            stats['tv_query'] += 1
                        if blt_query == 'het':
                            stats['het_query_snp'] += 1
                        elif blt_query == 'homalt':
                            stats['hom_query_snp'] += 1
                    elif vtype == 'INDEL':
                        stats['query_total_indel'] += 1
                        if blt_query == 'het':
                            stats['het_query_indel'] += 1
                        elif blt_query == 'homalt':
                            stats['hom_query_indel'] += 1

                # Count truth-side assessed variants (TP or FN)
                if bd_truth in ('TP', 'FN'):
                    vtype = bvt_truth
                    if vtype == 'SNP':
                        if bi_truth == 'ti':
                            stats['ti_truth'] += 1
                        elif bi_truth == 'tv':
                            stats['tv_truth'] += 1
                        if blt_truth == 'het':
                            stats['het_truth_snp'] += 1
                        elif blt_truth == 'homalt':
                            stats['hom_truth_snp'] += 1
                    elif vtype == 'INDEL':
                        if blt_truth == 'het':
                            stats['het_truth_indel'] += 1
                        elif blt_truth == 'homalt':
                            stats['hom_truth_indel'] += 1

        # Compute ratios from assessed variants only
        result = {
            'query_total_snp': stats['query_total_snp'],
            'query_total_indel': stats['query_total_indel'],
            'frac_na_snp': 0.0,
            'frac_na_indel': 0.0,
        }

        # Ti/Tv ratio (query-side, assessed only)
        if stats['tv_query'] > 0:
            result['titv_query_snp'] = stats['ti_query'] / stats['tv_query']
        if stats['tv_truth'] > 0:
            result['titv_truth_snp'] = stats['ti_truth'] / stats['tv_truth']

        # Het/Hom ratios (assessed only)
        if stats['hom_query_snp'] > 0:
            result['hethom_query_snp'] = stats['het_query_snp'] / stats['hom_query_snp']
        if stats['hom_truth_snp'] > 0:
            result['hethom_truth_snp'] = stats['het_truth_snp'] / stats['hom_truth_snp']
        if stats['hom_query_indel'] > 0:
            result['hethom_query_indel'] = stats['het_query_indel'] / stats['hom_query_indel']
        if stats['hom_truth_indel'] > 0:
            result['hethom_truth_indel'] = stats['het_truth_indel'] / stats['hom_truth_indel']

        titv_str = f"{result['titv_query_snp']:.4f}" if 'titv_query_snp' in result else "N/A"
        logger.info(f"Assessed-only metrics: query_snp={stats['query_total_snp']}, "
                     f"query_indel={stats['query_total_indel']}, titv={titv_str}")

        return result

    except Exception as e:
        logger.warning(f"Failed to parse hap.py VCF for assessed metrics: {e}")
        return None


class HappyScorer:
    """Score VCF outputs using hap.py."""

    def __init__(self, docker_image: str = None):
        self.docker_image = docker_image or HAPPY_DOCKER_IMAGE

    def score_vcf(self, truth_vcf: str, query_vcf: str,
                  reference_fasta: str = None, confident_bed: str = None,
                  region: str = None, reference_sdf: str = None,
                  mutations_vcf: str = None) -> Optional[Dict[str, float]]:
        """Run hap.py and return precision/recall/F1 metrics.

        Args:
            mutations_vcf: Path to mutations-only VCF. Required for accurate scoring.
        """
        if region is not None:
            region_check = validate_region(region)
            if not region_check["valid"]:
                logger.error(f"score_vcf: invalid region '{region}': {region_check['error']}")
                return self._get_zero_scores()

        try:
            truth_vcf = Path(truth_vcf).resolve()
            query_vcf = Path(query_vcf).resolve()
            ref_path = Path(reference_fasta).resolve() if reference_fasta else None
            bed_path = Path(confident_bed).resolve() if confident_bed else None
            sdf_path = Path(reference_sdf).resolve() if reference_sdf else None

            # Use query_vcf's parent as output directory for all intermediate files
            output_dir = query_vcf.parent
            original_query_stem = query_vcf.stem  # Save for output prefix naming

            # Count variants and sample a few positions for diagnostics
            query_variant_count = 0
            query_positions = []
            try:
                opener = gzip.open if str(query_vcf).endswith('.gz') else open
                with opener(query_vcf, 'rt') as f:
                    for line in f:
                        if line.strip() and not line.startswith('#'):
                            query_variant_count += 1
                            if len(query_positions) < 3:
                                parts = line.split('\t')
                                if len(parts) >= 2:
                                    query_positions.append(f"{parts[0]}:{parts[1]}")
            except Exception as e:
                query_variant_count = -1
                query_positions = [str(e)]

            logger.info(f"Scoring with hap.py: truth={truth_vcf.name}, query={query_vcf.name} "
                        f"({query_variant_count} variants), region={region}")
            if query_positions:
                logger.debug(f"Query sample positions: {', '.join(query_positions)}")
            logger.debug(f"Reference={ref_path.name if ref_path else 'None'}, "
                         f"BED={bed_path.name if bed_path else 'None'}, "
                         f"SDF={sdf_path.name if sdf_path else 'None'}")

            if not truth_vcf.exists():
                logger.error(f"Truth VCF not found: {truth_vcf}")
                return self._get_zero_scores()
            if not query_vcf.exists():
                logger.error(f"Query VCF not found: {query_vcf}")
                return self._get_zero_scores()
            if ref_path and not ref_path.exists():
                logger.error(f"Reference not found: {ref_path}")
                return self._get_zero_scores()

            # Validate BED file - if not found, run hap.py without confident regions filter
            use_bed = False
            subset_bed_path = None

            # Per-miner suffix for intermediate filenames. score_vcf is called
            # concurrently for multiple miners sharing the same output_dir;
            # without unique names, two miners would race on the same truth /
            # bed files and produce intermittent zero or wrong scores.
            region_slug = region.replace(':', '_').replace('-', '_') if region else "noregion"
            unique_suffix = original_query_stem

            if bed_path and bed_path.exists():
                # Subset BED file to only include regions overlapping with task region
                # This improves performance and accuracy
                if region:
                    subset_bed_path = output_dir / f"confident_{region_slug}_{unique_suffix}.bed"
                    logger.info(f"Creating subset BED for region {region}...")
                    if subset_bed(bed_path, subset_bed_path, region):
                        bed_path = subset_bed_path
                        use_bed = True
                        logger.info(f"Using subset confident regions BED: {bed_path}")
                    else:
                        logger.warning(f"Failed to subset BED, using full BED")
                        use_bed = True
                else:
                    use_bed = True
                    logger.info(f"Using full confident regions BED: {bed_path}")
            elif bed_path:
                logger.warning(f"Confident BED not found: {bed_path} - scoring without region filter")
            else:
                logger.info("No confident regions BED provided - scoring whole region")

            # Slice truth VCF to region for performance
            sliced_truth_vcf = None
            if region and truth_vcf.exists():
                sliced_truth_vcf = output_dir / f"truth_{region_slug}_{unique_suffix}.vcf.gz"
                logger.info(f"Creating sliced truth VCF for region {region}...")
                if slice_truth_vcf(truth_vcf, sliced_truth_vcf, region):
                    truth_vcf = sliced_truth_vcf
                    logger.info(f"Using sliced truth VCF: {truth_vcf.name}")
                else:
                    if mutations_vcf:
                        logger.error("Failed to slice truth VCF for synthetic scoring; failing closed")
                        return self._get_zero_scores()
                    logger.warning(f"Failed to slice truth VCF, using full chromosome VCF")

            # With mutations_vcf, assess the full challenge region so overcalls are
            # classified as FP instead of UNK. Synthetic scoring below still uses
            # mutations_vcf to restrict TP/FN scoring to injected targets.
            if mutations_vcf and region:
                challenge_bed = output_dir / f"challenge_region_{region_slug}_{unique_suffix}.bed"
                if generate_challenge_region_bed(region, str(challenge_bed)):
                    bed_path = challenge_bed
                    use_bed = True
                    logger.info(f"Scoring full challenge region for overcall guardrail")
                else:
                    logger.error("Could not generate challenge region BED for overcall guardrail; failing closed")
                    return self._get_zero_scores()
            else:
                synthetic_bed = output_dir / f"synthetic_regions_{unique_suffix}.bed"
                if generate_synthetic_regions_bed(str(truth_vcf), str(synthetic_bed)):
                    bed_path = synthetic_bed
                    use_bed = True
                    logger.info(f"Scoring restricted to synthetic mutation regions")
                else:
                    logger.warning("Could not generate synthetic regions BED, scoring full region")

            # Use miner's VCF as-is without normalization
            # We score the exact output the miner provides - no modifications
            logger.debug(f"Using miner VCF as-is: {query_vcf.name}")

            # Require SDF for vcfeval engine — deterministic scoring depends on it
            if not sdf_path or not sdf_path.exists() or not sdf_path.is_dir():
                logger.error(f"SDF reference not available at {sdf_path} — cannot score deterministically. "
                             "Run setup.py or ensure the SDF for this chromosome is downloaded.")
                return self._get_zero_scores()
            logger.info("Using vcfeval engine")

            # Set output prefix for hap.py results (use original query name)
            output_prefix = output_dir / f"happy_{original_query_stem}"

            # Determine query VCF mount path - normalized VCF is in output_dir
            # Original query is in query_vcf.parent (might be different from output_dir)
            query_in_output_dir = query_vcf.parent == output_dir
            if query_in_output_dir:
                query_mount_path = f"/data/output/{query_vcf.name}"
            else:
                query_mount_path = f"/data/query/{query_vcf.name}"

            logger.debug(f"Query VCF for hap.py: {query_vcf.name} (in {'output' if query_in_output_dir else 'query'} dir)")

            # Build Docker command as argument list (no shell=True) to prevent injection
            # RTG_MEM fixes "Cannot determine system memory" error in Docker (use 8g for larger VCFs)
            cmd_parts = [
                "docker", "run", "--rm",
                "-e", f"HGREF=/data/reference/{ref_path.name if ref_path else 'ref.fa'}",
                "-e", "RTG_MEM=8g",
                "-v", f"{truth_vcf.parent}:/data/truth",
                "-v", f"{ref_path.parent if ref_path else '/tmp'}:/data/reference",
                "-v", f"{output_dir}:/data/output",
            ]

            # Only mount query dir if query VCF is not in output_dir
            if not query_in_output_dir:
                cmd_parts.extend(["-v", f"{query_vcf.parent}:/data/query"])

            # Add SDF mount for vcfeval engine. Read-only: RTG vcfeval treats
            # the template as read-only and concurrent miners share this mount.
            cmd_parts.extend(["-v", f"{sdf_path}:/data/sdf:ro"])

            # Add BED mount only if using BED and it's in a different directory than truth VCF
            if use_bed and bed_path.parent != truth_vcf.parent:
                cmd_parts.extend(["-v", f"{bed_path.parent}:/data/bed"])
                bed_mount_path = f"/data/bed/{bed_path.name}"
            elif use_bed:
                bed_mount_path = f"/data/truth/{bed_path.name}"
            else:
                bed_mount_path = None

            # Docker image and hap.py command with arguments
            # IMPORTANT: Each flag and its value must be separate list items
            # NOTE: Docker image ENTRYPOINT is 'hap.py', so we pass VCFs directly (no explicit path)
            cmd_parts.extend([
                self.docker_image,
                f"/data/truth/{truth_vcf.name}",
                query_mount_path,
                "-r", f"/data/reference/{ref_path.name if ref_path else 'ref.fa'}",
                "-o", f"/data/output/{output_prefix.name}",
                "--threads", "4",
            ])

            # vcfeval engine (required — no xcmp fallback)
            cmd_parts.extend(["--engine", "vcfeval"])
            cmd_parts.extend(["--engine-vcfeval-template", "/data/sdf"])

            # Add confident regions filter only if BED file exists
            if use_bed and bed_mount_path:
                cmd_parts.extend(["-f", bed_mount_path])

            if region:
                logger.debug("Region filtering via sliced VCFs and confident BED (vcfeval mode)")

            logger.info(f"Running hap.py validation on region: {region}")
            logger.debug(f"hap.py command: {cmd_parts}")

            index_csi = Path(str(query_vcf) + ".csi")
            index_tbi = Path(str(query_vcf) + ".tbi")
            if not index_csi.exists() and not index_tbi.exists():
                logger.warning(f"Query VCF index not found (.csi or .tbi): {query_vcf}")

            # The prefix is derived from the query filename and the round
            # directory is reused, so a retry or restart finds the PREVIOUS
            # invocation's artifacts sitting at exactly these paths. Clearing
            # them first makes "the file exists" mean "this run produced it",
            # which is what the acceptance check below assumes.
            summary_csv = Path(f"{output_prefix}.summary.csv")
            annotated_vcf = Path(f"{output_prefix}.vcf.gz")
            for stale in sorted(output_dir.glob(f"{output_prefix.name}.*")):
                try:
                    stale.unlink()
                    logger.debug(f"Removed stale hap.py artifact {stale.name}")
                except OSError as exc:
                    logger.warning(f"Could not remove stale artifact {stale.name}: {exc}")

            invocation_start = time.time()
            result = subprocess.run(cmd_parts, capture_output=True, text=True, timeout=600)

            def _from_this_run(path: Path) -> bool:
                """Whether this invocation produced the file.

                The purge above should make this redundant; it is kept because
                an unlink that silently failed would otherwise let a stale
                artifact be scored as if it were fresh. One second of slack
                absorbs coarse filesystem timestamp granularity.
                """
                try:
                    return path.exists() and path.stat().st_mtime >= (invocation_start - 1.0)
                except OSError:
                    return False

            if summary_csv.exists() and not _from_this_run(summary_csv):
                logger.error(
                    f"hap.py summary at {summary_csv.name} predates this invocation; "
                    "refusing to score a stale artifact"
                )
                return self._get_zero_scores()

            if summary_csv.exists():
                logger.info(f"hap.py completed, parsing results from {summary_csv}")
                if result.returncode != 0:
                    # A nonzero exit is only tolerable when this run produced a
                    # complete artifact set. Without the annotated VCF the v2
                    # core cannot be built, and a partial run must not read as
                    # a successful one.
                    if not _from_this_run(annotated_vcf):
                        logger.error(
                            f"hap.py returned {result.returncode} and produced no "
                            f"annotated VCF this run; refusing to score a partial "
                            f"artifact set. stderr: {(result.stderr or '')[-500:]}"
                        )
                        return self._get_zero_scores()
                    logger.debug(f"hap.py returned code {result.returncode} (warnings only, output is valid)")

                # Parse results
                happy_results = {}
                with open(summary_csv, 'r') as f:
                    reader = csv.DictReader(f)
                    # Debug: log CSV headers to catch format changes
                    if reader.fieldnames:
                        logger.debug(f"hap.py CSV columns: {reader.fieldnames}")
                    else:
                        logger.warning("hap.py CSV has no headers!")

                    def safe_float(val):
                        """Parse a hap.py CSV cell.

                        hap.py writes 'inf' for a ratio with a zero denominator.
                        It is passed through: v1 reads it, v2 rejects it at the
                        plausibility gate.
                        """
                        try:
                            return float(val) if val and val != 'nan' else 0.0
                        except (ValueError, TypeError):
                            return 0.0

                    rows_parsed = 0
                    for row in reader:
                        variant_type = row.get('Type', '')
                        logger.debug(f"Parsing row: Type={variant_type}, Filter={row.get('Filter', '')}")

                        if variant_type in ['INDEL', 'SNP']:
                            rows_parsed += 1

                            # Only parse PASS filter rows
                            if row.get('Filter') != 'PASS':
                                continue

                            if variant_type == 'SNP':
                                happy_results['precision_snp'] = safe_float(row.get('METRIC.Precision', 0))
                                happy_results['recall_snp'] = safe_float(row.get('METRIC.Recall', 0))
                                happy_results['f1_snp'] = safe_float(row.get('METRIC.F1_Score', 0))
                                # Additional metrics for advanced scoring
                                happy_results['truth_total_snp'] = safe_float(row.get('TRUTH.TOTAL', 0))
                                happy_results['tp_snp'] = safe_float(row.get('TRUTH.TP', 0))
                                happy_results['fn_snp'] = safe_float(row.get('TRUTH.FN', 0))
                                happy_results['query_total_snp'] = safe_float(row.get('QUERY.TOTAL', 0))
                                happy_results['fp_snp'] = safe_float(row.get('QUERY.FP', 0))
                                happy_results['query_unk_snp'] = safe_float(row.get('QUERY.UNK', 0))
                                happy_results['frac_na_snp'] = safe_float(row.get('METRIC.Frac_NA', 0))
                                happy_results['titv_truth_snp'] = safe_float(row.get('TRUTH.TOTAL.TiTv_ratio', 0))
                                happy_results['titv_query_snp'] = safe_float(row.get('QUERY.TOTAL.TiTv_ratio', 0))
                                happy_results['hethom_truth_snp'] = safe_float(row.get('TRUTH.TOTAL.het_hom_ratio', 0))
                                happy_results['hethom_query_snp'] = safe_float(row.get('QUERY.TOTAL.het_hom_ratio', 0))
                                logger.debug(f"SNP metrics: P={happy_results['precision_snp']:.3f}, R={happy_results['recall_snp']:.3f}, F1={happy_results['f1_snp']:.3f}")
                            elif variant_type == 'INDEL':
                                happy_results['precision_indel'] = safe_float(row.get('METRIC.Precision', 0))
                                happy_results['recall_indel'] = safe_float(row.get('METRIC.Recall', 0))
                                happy_results['f1_indel'] = safe_float(row.get('METRIC.F1_Score', 0))
                                # Additional metrics for advanced scoring
                                happy_results['truth_total_indel'] = safe_float(row.get('TRUTH.TOTAL', 0))
                                happy_results['tp_indel'] = safe_float(row.get('TRUTH.TP', 0))
                                happy_results['fn_indel'] = safe_float(row.get('TRUTH.FN', 0))
                                happy_results['query_total_indel'] = safe_float(row.get('QUERY.TOTAL', 0))
                                happy_results['fp_indel'] = safe_float(row.get('QUERY.FP', 0))
                                happy_results['query_unk_indel'] = safe_float(row.get('QUERY.UNK', 0))
                                happy_results['frac_na_indel'] = safe_float(row.get('METRIC.Frac_NA', 0))
                                happy_results['hethom_truth_indel'] = safe_float(row.get('TRUTH.TOTAL.het_hom_ratio', 0))
                                happy_results['hethom_query_indel'] = safe_float(row.get('QUERY.TOTAL.het_hom_ratio', 0))
                                logger.debug(f"INDEL metrics: P={happy_results['precision_indel']:.3f}, R={happy_results['recall_indel']:.3f}, F1={happy_results['f1_indel']:.3f}")

                    if rows_parsed == 0:
                        logger.warning("hap.py CSV had no SNP/INDEL rows - check CSV format")

                # Fill in missing keys with defaults
                for key in ['f1_snp', 'f1_indel', 'precision_snp', 'recall_snp', 'precision_indel', 'recall_indel']:
                    if key not in happy_results:
                        happy_results[key] = 0.0

                # Calculate weighted F1
                happy_results['weighted_f1'] = 0.7 * happy_results['f1_snp'] + 0.3 * happy_results['f1_indel']

                # Override polluted metrics from summary.csv with assessed-only values.
                # hap.py's summary.csv computes QUERY.TOTAL, Frac_NA, TiTv_ratio, and
                # het_hom_ratio over the ENTIRE query VCF (including UNK variants outside
                # the -f BED regions). This inflates query_total (e.g. 5000+ vs ~120),
                # sets Frac_NA to ~0.99, and skews ratios — breaking the FP Rate,
                # Completeness, and Quality components of AdvancedScorer.
                # Parse the hap.py output VCF to get metrics from only assessed variants.
                happy_vcf = Path(f"{output_prefix}.vcf.gz")
                # Hand the annotated VCF back to the caller. v2 needs per-variant
                # records from it, and every caller otherwise has to rebuild this
                # path from a naming convention it does not own.
                happy_results['happy_vcf_path'] = str(happy_vcf)
                assessed = parse_happy_vcf_assessed_metrics(str(happy_vcf))
                if assessed:
                    old_qt_snp = happy_results.get('query_total_snp', 0)
                    old_qt_indel = happy_results.get('query_total_indel', 0)
                    for key, val in assessed.items():
                        happy_results[key] = val
                    logger.info(f"Corrected metrics from VCF: query_total {old_qt_snp}+{old_qt_indel}"
                                f" -> {assessed['query_total_snp']}+{assessed['query_total_indel']}")

                logger.info(f"hap.py results: SNP F1={happy_results['f1_snp']:.3f}, INDEL F1={happy_results['f1_indel']:.3f}")

                # Recompute metrics from only target mutation positions
                if mutations_vcf:
                    happy_vcf = Path(f"{output_prefix}.vcf.gz")
                    synthetic_metrics = compute_synthetic_only_metrics(
                        str(happy_vcf), mutations_vcf
                    )
                    if synthetic_metrics is None:
                        logger.error("Failed to compute filtered metrics from hap.py output")
                        return self._get_zero_scores()
                    happy_results.update(synthetic_metrics)
                    # fp_per_target is a rate that reaches consensus -- the v1
                    # overcall guardrail and 30% of the v2 score. Its denominator
                    # is the target count from the mutations VCF, not the TP+FN
                    # hap.py happened to assess, so the same callset written two
                    # ways cannot produce two different rates. Falls back to the
                    # old totals only if the target counts are unavailable.
                    synthetic_target_total = (
                        synthetic_metrics.get('target_total_snp', 0.0) +
                        synthetic_metrics.get('target_total_indel', 0.0)
                    )
                    synthetic_target_snp = synthetic_metrics.get('target_total_snp', 0.0)
                    if synthetic_target_total <= 0:
                        logger.warning(
                            "No target ALT alleles counted from the mutations VCF; "
                            "falling back to assessed TP+FN for fp_per_target"
                        )
                        synthetic_target_total = (
                            synthetic_metrics.get('truth_total_snp', 0.0) +
                            synthetic_metrics.get('truth_total_indel', 0.0)
                        )
                        synthetic_target_snp = synthetic_metrics.get('truth_total_snp', 0.0)
                    overcall_metrics = parse_region_overcall_metrics(
                        str(happy_vcf),
                        synthetic_target_total,
                        synthetic_target_snp,
                        # The target counts above are for the v2 rate only.
                        v1_truth_total=(
                            synthetic_metrics.get('truth_total_snp', 0.0) +
                            synthetic_metrics.get('truth_total_indel', 0.0)
                        ),
                        v1_snp_truth_total=synthetic_metrics.get('truth_total_snp', 0.0),
                    )
                    if overcall_metrics:
                        happy_results.update(overcall_metrics)
                    logger.info(f"Filtered metrics: SNP F1={synthetic_metrics['f1_snp']:.3f}, "
                                f"INDEL F1={synthetic_metrics['f1_indel']:.3f}")

                return happy_results
            else:
                logger.error(f"hap.py failed - no summary CSV created. Return code: {result.returncode}")
                logger.error(f"stderr: {result.stderr[:500] if result.stderr else 'None'}")
                logger.error(f"stdout: {result.stdout[:500] if result.stdout else 'None'}")
                return self._get_zero_scores()

        except subprocess.TimeoutExpired:
            logger.error("hap.py execution timed out after 10 minutes")
            return self._get_zero_scores()
        except Exception as e:
            logger.error(f"Error running hap.py: {e}")
            logger.debug(traceback.format_exc())
            return self._get_zero_scores()

    def _get_zero_scores(self) -> Optional[Dict[str, float]]:
        """Return no score so the validator can seek peer backfill."""
        return None


# ---------------------------------------------------------------------------
# Difficulty-weighted scoring (v2)
#
# v1 weights core F1 by truth counts, so the most numerous class dominates; v2
# weights by intrinsic difficulty instead.
#
# The weights are fixed constants and must not be derived from observed results:
# a validator scores only its assigned subset, so field-derived weights would
# make two honest validators disagree about the same submission. Classes are
# defined by intrinsic properties of the truth variant only — type, indel
# length, zygosity.
DIFFICULTY_WEIGHTS = {
    "snp_hom":     0.02,
    "snp_het":     0.18,
    "indel_1bp":   0.40,
    "indel_2_3bp": 0.16,
    "indel_4_7bp": 0.10,
    "indel_8bp":   0.14,
}

# Plausibility is a gate, not points: a callset can fail it but cannot earn by
# it. The bounds are deliberately loose — they reject malformed output and are
# not a tuning target.
GATE_TITV_MAX_DELTA = 0.6
GATE_HETHOM_MAX_DELTA = 1.5

# Germline precision decay: exp(-fp_per_target / GERMLINE_FP_SCALE). Must stay
# strictly monotone in fp_per_target — every false positive costs something, at
# every rate.
GERMLINE_FP_SCALE = 8.0

V2_CORE_WEIGHT = 0.70
V2_GERMLINE_WEIGHT = 0.30


def _is_hom_alt(gt: str) -> bool:
    """True for any homozygous-ALT genotype, including multi-allelic 2/2, 3/3."""
    parts = [p for p in str(gt or "").replace("|", "/").split("/") if p != ""]
    if len(parts) < 2 or not all(p.isdigit() for p in parts):
        return False
    return len(set(parts)) == 1 and parts[0] != "0"


def classify_variant_difficulty(record: Dict[str, Any]) -> str:
    """Bucket one truth variant by intrinsic difficulty.

    Uses only properties of the variant itself, never of the callset or the
    field, so every validator classifies identically.

    Zygosity must come from ``truth_genotype``: an FN has no ALT call to be
    zygous about, so reading the call would file every missed hom variant as
    het. The call is a fallback only for records with no truth genotype (FPs).
    """
    ref = str(record.get("ref") or "")
    alt = str(record.get("alt") or "")
    indel_len = abs(len(ref) - len(alt))
    # Equal-length alleles are a substitution (MNP) whatever the record is typed
    # as, so they fall through to the zygosity branch rather than the length
    # ladder below.
    if indel_len > 0:
        if indel_len >= 8:
            return "indel_8bp"
        if indel_len >= 4:
            return "indel_4_7bp"
        if indel_len >= 2:
            return "indel_2_3bp"
        return "indel_1bp"
    gt = str(record.get("truth_genotype") or record.get("called_genotype") or "")
    return "snp_hom" if _is_hom_alt(gt) else "snp_het"


def difficulty_class_counts(variant_records) -> Dict[str, Dict[str, int]]:
    """Per-class TP/FN/FP counts from hap.py per-variant records."""
    counts = {k: {"tp": 0, "fn": 0, "fp": 0} for k in DIFFICULTY_WEIGHTS}
    for rec in variant_records or []:
        cls = rec.get("classification")
        if cls not in ("TP", "FN", "FP"):
            continue
        key = classify_variant_difficulty(rec)
        if key in counts:
            counts[key][cls.lower()] += 1
    return counts


def difficulty_weighted_f1(class_counts: Dict[str, Dict[str, int]]) -> Optional[float]:
    """F1 per class, combined by fixed difficulty weight.

    A class with no truth variants in this round is dropped and the remaining
    weights renormalised, so a region containing no long indels is not scored as
    if the miner had failed them.
    """
    if not class_counts:
        return None
    total_w = 0.0
    acc = 0.0
    for key, w in DIFFICULTY_WEIGHTS.items():
        c = class_counts.get(key)
        if not c:
            continue
        tp, fn, fp = c.get("tp", 0), c.get("fn", 0), c.get("fp", 0)
        if tp + fn <= 0:
            # Absent truth drops the class only when nothing was called in it;
            # otherwise it stays in at F1 0 so its FPs are still priced.
            if fp <= 0:
                continue
            acc += w * 0.0        # all calls in this class are wrong
            total_w += w
            continue
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn)
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
        acc += w * f1
        total_w += w
    if total_w <= 0:
        return None
    return acc / total_w


def _usable_ratio(value) -> Optional[float]:
    """A ratio that can be compared, or None.

    Zero, negative, non-finite and non-numeric all mean "not measurable" rather
    than "measured as zero": a ti/tv or het/hom of 0 is what an empty or
    degenerate callset produces, not a real ratio.
    """
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(ratio) or ratio <= 0:
        return None
    return ratio


def plausibility_gate(metrics: Dict[str, float]) -> bool:
    """False when the callset is malformed enough to disqualify.

    Semantics, stated explicitly because the v1 reasoning does not survive here:

    * Truth ratio unmeasurable -- skip. There is nothing to compare against, and
      that is the round's property, not the miner's.
    * Truth measurable, query not -- FAIL. Under v1 these ratios were scored
      components, so an absent one simply earned nothing. In v2 the gate is
      pass/fail, so treating absent as "skip" turns it into a free pass: a
      callset with no homozygous SNP calls would sidestep the het/hom check
      entirely, which is a bypass a miner can choose.
    * Both measurable -- compare against the threshold.
    """
    for truth_key, query_key, max_delta in (
        ("titv_truth_snp", "titv_query_snp", GATE_TITV_MAX_DELTA),
        ("hethom_truth_snp", "hethom_query_snp", GATE_HETHOM_MAX_DELTA),
    ):
        truth_ratio = _usable_ratio(metrics.get(truth_key))
        if truth_ratio is None:
            continue
        query_ratio = _usable_ratio(metrics.get(query_key))
        if query_ratio is None:
            logger.warning(
                f"Plausibility gate failed: {truth_key} is measurable "
                f"({truth_ratio:.3f}) but {query_key} is not "
                f"({metrics.get(query_key)!r})"
            )
            return False
        if abs(query_ratio - truth_ratio) > max_delta:
            return False
    return True


class AdvancedScorer:
    """Advanced scoring with multi-component emphasis-based evaluation."""

    @staticmethod
    def emphasis(metric: float, gamma: float = 3.0) -> float:
        """
        Apply nonlinear emphasis to push scores toward extremes.

        Args:
            metric: Raw metric (0-1)
            gamma: Emphasis power (higher = more extreme)

        Returns:
            Emphasized metric
        """
        metric = max(0.0, min(metric, 0.999999))
        return 1.0 - (1.0 - metric) ** gamma

    @staticmethod
    def ratio_penalty(delta: float, tolerance: float) -> float:
        """Compute exponential penalty for ratio deviation."""
        return math.exp(-abs(delta) / tolerance)

    @staticmethod
    def compute_advanced_score(metrics: Dict[str, float]) -> float:
        """
        Compute advanced score with four components.

        Components:
        - Core (60%): Truth-weighted F1 with emphasis (γ=0.5)
        - Completeness (15%): Average recall (γ=3.0) + coverage (γ=2.0)
        - FP Rate (15%): Penalizes FP > 0.2% and call count != truth count
        - Quality (10%): Ti/Tv and Het/Hom ratio match penalties

        This formula is frozen. It scores live rounds and a field is routinely
        tied to fifteen decimal places, so any change reorders a leaderboard.
        compute_score_v2 is where it is revised, selected by the platform's
        scoring_version.

        Args:
            metrics: Dictionary with f1_snp, f1_indel, plus additional hap.py metrics

        Returns:
            Final score (0-100)
        """
        # Get metrics with defaults
        f1_snp = metrics.get('f1_snp', 0)
        f1_indel = metrics.get('f1_indel', 0)
        recall_snp = metrics.get('recall_snp', 0)
        recall_indel = metrics.get('recall_indel', 0)

        truth_total_snp = metrics.get('truth_total_snp', 0)
        truth_total_indel = metrics.get('truth_total_indel', 0)
        query_total_snp = metrics.get('query_total_snp', 0)
        query_total_indel = metrics.get('query_total_indel', 0)
        fp_snp = metrics.get('fp_snp', 0)
        fp_indel = metrics.get('fp_indel', 0)
        frac_na_snp = metrics.get('frac_na_snp', 0)
        frac_na_indel = metrics.get('frac_na_indel', 0)

        titv_truth_snp = metrics.get('titv_truth_snp', 0)
        titv_query_snp = metrics.get('titv_query_snp', 0)
        hethom_truth_snp = metrics.get('hethom_truth_snp', 0)
        hethom_query_snp = metrics.get('hethom_query_snp', 0)
        hethom_truth_indel = metrics.get('hethom_truth_indel', 0)
        hethom_query_indel = metrics.get('hethom_query_indel', 0)

        # Component 1: Core F1 (60% weight) - truth-weighted F1 with emphasis
        total_truth = truth_total_snp + truth_total_indel
        if total_truth <= 0:
            logger.error("AdvancedScorer missing truth totals; returning zero score")
            return 0.0
        weighted_f1 = (f1_snp * truth_total_snp + f1_indel * truth_total_indel) / total_truth
        core_component = AdvancedScorer.emphasis(weighted_f1, gamma=0.5)

        # Component 2: Completeness (15% weight) - recall + coverage
        # Frozen: a flat mean, unlike the truth-weighted core above. v2 is
        # where the weighting is revised.
        avg_recall = (recall_snp + recall_indel) / 2
        frac_na = max(frac_na_snp, frac_na_indel)
        coverage = 1.0 - frac_na
        completeness_component = (
            AdvancedScorer.emphasis(avg_recall, gamma=3.0) +
            AdvancedScorer.emphasis(coverage, gamma=2.0)
        ) / 2.0

        # Component 3: FP Rate (15% weight) - penalize high FP and wrong call count
        total_fp = fp_snp + fp_indel
        total_calls = query_total_snp + query_total_indel
        fp_rate = total_fp / max(total_calls, 1.0)
        size_ratio = total_calls / max(total_truth, 1.0)

        # Scale FP target to evaluation set size: allow ~1 FP per eval set
        target_fp = max(0.002, 1.0 / max(total_truth, 1.0))
        scale_fp = target_fp
        fp_pen = math.exp(-max(0.0, fp_rate - target_fp) / scale_fp)
        size_pen = math.exp(-abs(size_ratio - 1.0) / 0.10)
        fp_component = (fp_pen + size_pen) / 2.0

        # Component 4: Quality (10% weight) - Ti/Tv and Het/Hom ratio penalties
        titv_penalties = []
        hethom_penalties = []

        # Frozen: the ratio gates and the no-penalty default below are v1 as
        # deployed. v2 replaces both with a plausibility gate.
        if titv_truth_snp > 0 and titv_query_snp > 0:
            titv_penalties.append(
                AdvancedScorer.ratio_penalty(titv_query_snp - titv_truth_snp, 0.1)
            )

        if hethom_truth_snp > 0 and hethom_query_snp > 0:
            hethom_penalties.append(
                AdvancedScorer.ratio_penalty(hethom_query_snp - hethom_truth_snp, 0.15)
            )
        if hethom_truth_indel > 0 and hethom_query_indel > 0:
            hethom_penalties.append(
                AdvancedScorer.ratio_penalty(hethom_query_indel - hethom_truth_indel, 0.15)
            )

        titv_component = sum(titv_penalties) / len(titv_penalties) if titv_penalties else 1.0
        hethom_component = sum(hethom_penalties) / len(hethom_penalties) if hethom_penalties else 1.0
        quality_component = (titv_component + hethom_component) / 2.0

        # Final weighted score (60/15/15/10)
        final_score = 100.0 * (
            0.60 * core_component +
            0.15 * completeness_component +
            0.15 * fp_component +
            0.10 * quality_component
        )

        overcall_penalty = metrics.get('overcall_penalty', 0.0)
        # Frozen: v1 floors here rather than raising. v2 returns None instead.
        return max(0.0, final_score - overcall_penalty)

    @staticmethod
    def compute_score_v2(
        metrics: Dict[str, float],
        class_counts: Optional[Dict[str, Dict[str, int]]] = None,
    ) -> Optional[float]:
        """Difficulty-weighted score, or None when it cannot be computed.

            score = 100 * (0.70*core + 0.30*germline)   , gated

        Two direct measures against truth over two different populations, so
        neither double-counts the other. v1's completeness, FP-rate and quality
        components are deliberately absent: the first restates core recall, the
        second reduced in practice to a call-count proxy, and the third is now
        the plausibility gate.

        Returns None rather than a wrong number when per-variant records or the
        germline FP measurement are unavailable. The caller does NOT fall back
        to v1 on None — v1 and v2 are different scales, so it skips the miner
        for that round instead of mixing them in one ranking.
        """
        core = difficulty_weighted_f1(class_counts) if class_counts else None
        if core is None:
            return None

        fp_per_target = metrics.get('fp_per_target')
        if fp_per_target is None:
            # Without a germline FP measurement precision cannot be priced;
            # decline rather than score on core alone.
            return None

        # Enforce the scorer's own input contract rather than trusting the
        # producer. max(0.0, nan) returns 0.0 -- NaN compares False against
        # everything -- so a non-finite rate silently became exp(0) == 1.0, a
        # PERFECT germline component. A negative rate does the same. Both are
        # "this measurement is broken", never "this miner was flawless".
        try:
            fp_rate = float(fp_per_target)
        except (TypeError, ValueError):
            logger.error(f"v2: fp_per_target is not numeric ({fp_per_target!r})")
            return None
        if not math.isfinite(fp_rate) or fp_rate < 0:
            logger.error(
                f"v2: fp_per_target must be a finite, non-negative rate; "
                f"got {fp_rate!r}. Declining to score."
            )
            return None
        germline = math.exp(-fp_rate / GERMLINE_FP_SCALE)

        if not plausibility_gate(metrics):
            return 0.0

        score = 100.0 * (V2_CORE_WEIGHT * core + V2_GERMLINE_WEIGHT * germline)
        # A score outside [0, 100] means a component broke its own contract.
        # Emitting it would put an impossible number into consensus.
        if not math.isfinite(score) or not (0.0 <= score <= 100.0):
            logger.error(f"v2: computed an out-of-range score ({score!r}); declining")
            return None
        return score


def _representative_alt(alts, truth_genotype, called_genotype) -> Optional[str]:
    """The ALT a benchmark decision is about, at a possibly multi-allelic site.

    Prefers the allele the truth genotype selects, then the call's, then the
    first. Reads only the record itself, so every validator agrees.
    """
    alts = list(alts or [])
    if not alts:
        return None
    if len(alts) == 1:
        return alts[0]
    for gt in (truth_genotype, called_genotype):
        for token in str(gt or "").replace("|", "/").split("/"):
            if token.isdigit():
                idx = int(token)
                if 1 <= idx <= len(alts):     # 0 is REF, alleles are 1-based
                    return alts[idx - 1]
    return alts[0]


def parse_happy_vcf(vcf_path: str, truth_vcf_path: str = None) -> Optional[List[Dict[str, Any]]]:
    """Parse hap.py annotated VCF to extract per-variant TP/FP/FN classifications.

    Parsing is ATOMIC: either every record is read, or None is returned. A
    partial list would be indistinguishable from a complete one, and the v2
    core is computed from these records -- dropping the FNs and FPs after a
    mid-file failure while keeping the TPs before it inflates the score, in the
    miner's favour, silently. None means "could not parse"; an empty list means
    "parsed fine, no benchmark decisions in the file", and the two are not
    interchangeable.

    hap.py annotated VCFs have FORMAT fields:
    - BD: Benchmark Decision (TP, FP, FN)
    - BVT: Benchmark Variant Type (SNP, INDEL)

    Also extracts call-level details from the QUERY sample:
    - GT: Called genotype (e.g. 0/1, 1/1)
    - DP: Read depth
    - AD: Allele depth (ref,alt counts)
    - GQ: Genotype quality

    Args:
        vcf_path: Path to hap.py output .vcf.gz file
        truth_vcf_path: Optional path to truth VCF to determine which
                        variants are synthetic (have SYNTHETIC INFO flag)

    Returns:
        One dict per benchmark decision (NOT per ALT allele) with keys: chrom,
        pos, ref, alt, alt_alleles, variant_type, classification, quality,
        filter_status, called_genotype, truth_genotype, read_depth,
        allele_depth, genotype_quality, is_synthetic.

        ``truth_genotype`` is the zygosity source for scoring; ``called_genotype``
        describes what the miner called and is absent for an FN by definition.
    """
    results = []
    vcf_path = str(vcf_path)

    try:
        import pysam
    except ImportError:
        logger.warning("pysam not installed, cannot parse hap.py VCF for variant-level results")
        return None

    # Build set of synthetic positions from truth VCF if provided.
    # Test the INFO keys, not str(rec.info): pysam's VariantRecordInfo has no
    # string form containing the flag names.
    synthetic_positions = set()
    if truth_vcf_path:
        try:
            truth_vcf = pysam.VariantFile(str(truth_vcf_path))
            for rec in truth_vcf:
                try:
                    keys = set(rec.info.keys())
                except Exception:  # noqa: BLE001 - malformed INFO on one record
                    continue
                if "SYNTHETIC" in keys:
                    synthetic_positions.add((rec.chrom, rec.pos))
            truth_vcf.close()
            logger.info(f"Loaded {len(synthetic_positions)} synthetic positions from truth VCF")
            if not synthetic_positions:
                logger.warning(
                    f"No SYNTHETIC-flagged variants found in {truth_vcf_path}; "
                    f"is_synthetic will be False for every record. If this truth "
                    f"VCF is expected to carry synthetic variants, the flag name "
                    f"or the file is wrong."
                )
        except Exception as e:
            logger.warning(f"Could not parse truth VCF for synthetic flags: {e}")

    try:
        vcf_in = pysam.VariantFile(vcf_path)
    except Exception as e:
        logger.error(f"Failed to open hap.py VCF {vcf_path}: {e}")
        return None

    try:
        for record in vcf_in:
            # hap.py output has two samples: TRUTH and QUERY
            # BD and BVT are in the FORMAT fields for each sample
            for sample_name in record.samples:
                sample = record.samples[sample_name]

                bd = sample.get("BD", None)
                bvt = sample.get("BVT", None)

                if bd is None or bvt is None:
                    continue

                # Only record TP, FP, FN (skip N = not assessed)
                if bd not in ("TP", "FP", "FN"):
                    continue

                # For TRUTH sample: TP and FN
                # For QUERY sample: TP and FP
                # Avoid double-counting TPs (only take from TRUTH)
                if bd == "TP" and sample_name != "TRUTH":
                    continue
                if bd == "FP" and sample_name != "QUERY":
                    continue
                if bd == "FN" and sample_name != "TRUTH":
                    continue

                # Extract call-level details from QUERY sample
                query_sample = record.samples.get("QUERY")
                called_genotype = None
                read_depth = None
                allele_depth = None
                genotype_quality = None

                if query_sample is not None:
                    gt = query_sample.get("GT", None)
                    if gt is not None:
                        called_genotype = "/".join(str(a) if a is not None else "." for a in gt)
                    dp = query_sample.get("DP", None)
                    if dp is not None:
                        read_depth = int(dp)
                    ad = query_sample.get("AD", None)
                    if ad is not None:
                        allele_depth = ",".join(str(a) for a in ad)
                    gq = query_sample.get("GQ", None)
                    if gq is not None:
                        genotype_quality = float(gq)

                # Zygosity for scoring comes from TRUTH: an FN has no call to
                # be zygous about. See classify_variant_difficulty.
                truth_sample = record.samples.get("TRUTH")
                truth_genotype = None
                if truth_sample is not None:
                    tgt = truth_sample.get("GT", None)
                    if tgt is not None:
                        truth_genotype = "/".join(
                            str(a) if a is not None else "." for a in tgt
                        )

                # Determine if variant is synthetic
                is_synthetic = None
                if synthetic_positions:
                    is_synthetic = (record.chrom, record.pos) in synthetic_positions

                # One row per benchmark decision, not per ALT: BD/BVT describe
                # the whole record, so expanding over record.alts would count a
                # single TP/FP/FN once per allele. The representative allele is
                # the one the genotype selects, so indel length is measured
                # against it; the rest are kept in alt_alleles.
                alts = list(record.alts or [])
                alt_repr = _representative_alt(alts, truth_genotype, called_genotype)
                if bvt not in ("SNP", "INDEL"):
                    # Infer rather than coerce to SNP: SNP is the lowest-weighted
                    # v2 class, so a parsing surprise must not discount itself.
                    inferred = "INDEL" if len(str(record.ref or "")) != len(str(alt_repr or "")) else "SNP"
                    logger.warning(
                        f"Unrecognised BVT {bvt!r} at {record.chrom}:{record.pos}; "
                        f"inferred {inferred} from alleles"
                    )
                    variant_type = inferred
                else:
                    variant_type = bvt

                results.append({
                    "chrom": record.chrom,
                    "pos": record.pos,
                    "ref": record.ref,
                    "alt": alt_repr,
                    "alt_alleles": ",".join(alts) if len(alts) > 1 else None,
                    "variant_type": variant_type,
                    "classification": bd,
                    "quality": float(record.qual) if record.qual is not None else None,
                    "filter_status": ",".join(record.filter.keys()) if record.filter else None,
                    "called_genotype": called_genotype,
                    "truth_genotype": truth_genotype,
                    "read_depth": read_depth,
                    "allele_depth": allele_depth,
                    "genotype_quality": genotype_quality,
                    "is_synthetic": is_synthetic,
                })

        logger.info(f"Parsed {len(results)} variant-level results from hap.py VCF")
    except Exception as e:
        # Discard what was accumulated. Returning it would hand the caller a
        # truncated callset that looks complete: the records before the failure
        # keep their TPs while the FNs and FPs after it vanish.
        logger.error(
            f"Error parsing hap.py VCF after {len(results)} record(s); "
            f"discarding the partial parse: {e}"
        )
        return None
    finally:
        vcf_in.close()

    return results
