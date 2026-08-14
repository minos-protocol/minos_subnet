"""
BCFtools mpileup/call variant caller template.

Requires: Docker with bcftools and samtools images
"""
from pathlib import Path
from typing import Dict, Any
import logging
import subprocess
import platform
import os
import shlex
import time
from .tool_params import validate_and_build_flags, validate_region
from ._common import count_variants

logger = logging.getLogger(__name__)


def variant_call(
    bam_path: Path,
    reference_path: Path,
    output_vcf_path: Path,
    region: str,
    config: Dict[str, Any]
) -> Dict[str, Any]:
    """Run BCFtools mpileup | call | norm pipeline via Docker."""
    timeout = config.get("timeout", 1800)
    threads = min(config.get("threads", os.cpu_count() or 2), os.cpu_count() or 2)

    bam_path = Path(bam_path).resolve()
    reference_path = Path(reference_path).resolve()
    output_vcf_path = Path(output_vcf_path).resolve()
    output_vcf_path.parent.mkdir(parents=True, exist_ok=True)

    if not str(output_vcf_path).endswith('.vcf.gz'):
        output_vcf_path = output_vcf_path.parent / f"{output_vcf_path.stem}.vcf.gz"

    region_validation = validate_region(region)
    if not region_validation["valid"]:
        return {
            "success": False,
            "variant_count": 0,
            "error": f"Region validation failed: {region_validation['error']}"
        }

    if not bam_path.exists():
        return {"success": False, "variant_count": 0, "error": f"BAM not found: {bam_path}"}

    if not reference_path.exists():
        return {"success": False, "variant_count": 0, "error": f"Reference not found: {reference_path}"}

    ref_index = Path(f"{reference_path}.fai")
    if not ref_index.exists():
        return {"success": False, "variant_count": 0, "error": f"Reference index not found: {reference_path}.fai"}

    bcf_options = config.get("bcftools_options", {})
    validation_result = validate_and_build_flags("bcftools", bcf_options)

    if not validation_result["valid"]:
        error_msg = f"Invalid BCFtools parameters: {'; '.join(validation_result['errors'])}"
        return {"success": False, "variant_count": 0, "error": error_msg}

    # Separate flags by pipeline stage
    mpileup_flags = []
    call_flags = []
    difficult_regions_mask = None

    for flag_info in validation_result["flags"]:
        if isinstance(flag_info, dict):
            if flag_info["stage"] == "mpileup":
                mpileup_flags.append(flag_info["flag"])
            elif flag_info["stage"] == "call":
                call_flags.append(flag_info["flag"])
            elif flag_info["stage"] == "post_call":
                difficult_regions_mask = flag_info["flag"]
        else:
            mpileup_flags.append(flag_info)

    mpileup_flags_str = " ".join(mpileup_flags) if mpileup_flags else ""

    # `bcftools call` requires a caller-mode flag (`-m` for multiallelic,
    # `-c` for consensus). The previous default `-mv` was only injected
    # when call_flags was completely empty; submissions that included
    # other call-stage params (e.g. `prior`, `pval_threshold`) produced
    # a malformed `bcftools call` invocation that exited non-zero. Force
    # a caller mode whenever neither is present.
    def _has_caller_mode(flags: list[str]) -> bool:
        for f in flags:
            tok = f.split()[0]
            if tok in ("-m", "--multiallelic-caller", "-c", "--consensus-caller"):
                return True
        return False

    # Exactly one caller mode: `bcftools call -m` (multiallelic) and `-c`
    # (consensus) are mutually exclusive. Reject configs that enable both.
    _has_m = any(f.split()[0] in ("-m", "--multiallelic-caller") for f in call_flags)
    _has_c = any(f.split()[0] in ("-c", "--consensus-caller") for f in call_flags)
    if _has_m and _has_c:
        return {"success": False, "variant_count": 0,
                "error": "Invalid BCFtools parameters: multiallelic_caller and "
                         "consensus_caller are mutually exclusive; enable exactly one."}

    if call_flags and not _has_caller_mode(call_flags):
        call_flags = ["-m"] + call_flags
        logger.info("bcftools.call: no caller mode in submitted flags; defaulting to -m")

    call_flags_str = " ".join(call_flags) if call_flags else "-mv"

    # Optional post-call difficult-regions mask: resolve the path and require
    # the BED up front so a bad path fails before any Docker work starts.
    # The same config is re-run by validators, so the file must exist at this
    # path wherever the config is executed.
    mask_path = None
    if difficult_regions_mask:
        mask_path = Path(difficult_regions_mask).resolve()
        if not mask_path.is_file():
            return {"success": False, "variant_count": 0,
                    "error": f"Difficult-regions mask not found: {mask_path}"}

    start_time = time.time()
    is_arm = platform.machine() == "arm64"

    # Ensure BAM is indexed (bcftools mpileup requires it)
    bam_index = Path(f"{bam_path}.bai")
    if not bam_index.exists():
        bam_index = bam_path.with_suffix(".bam.bai")
        if not bam_index.exists():
            logger.info("Creating BAM index...")
            index_cmd = ["docker", "run", "--rm"]
            if is_arm:
                index_cmd.extend(["--platform", "linux/amd64"])
            index_cmd.extend([
                f"--cpus={threads}",
                "-v", f"{bam_path.parent}:/data",
                "quay.io/biocontainers/samtools:1.20--h50ea8bc_0",
                "samtools", "index", "-@", str(threads), f"/data/{bam_path.name}",
            ])

            try:
                index_result = subprocess.run(
                    index_cmd,
                    capture_output=True,
                    text=True,
                    timeout=300
                )
                if index_result.returncode != 0:
                    return {"success": False, "variant_count": 0,
                            "error": f"Failed to create BAM index: {index_result.stderr[:200]}"}
            except subprocess.TimeoutExpired:
                return {"success": False, "variant_count": 0, "error": "BAM indexing timed out"}

    # Build the pipeline script. With a difficult-regions mask the normalized
    # output stays uncompressed (-Ou) and is piped through a post-call exclude
    # step (bcftools view -T ^<mask>) before writing the final VCF; without a
    # mask the pipeline is exactly mpileup|call|norm as before.
    ref_q = shlex.quote(reference_path.name)
    out_q = shlex.quote(output_vcf_path.name)
    pipeline_script = (
        f"set -euo pipefail\n"
        f"bcftools mpileup --threads {threads} -f /ref/{ref_q} -r {shlex.quote(region)} {mpileup_flags_str} -Ou /data/{shlex.quote(bam_path.name)} "
        f"| bcftools call --threads {threads} {call_flags_str} -Ou "
    )
    if mask_path is not None:
        pipeline_script += (
            f"| bcftools norm --threads {threads} -f /ref/{ref_q} -Ou "
            f"| bcftools view --threads {threads} -T ^/mask/{shlex.quote(mask_path.name)} -Oz -o /output/{out_q}\n"
        )
        pipeline_label = "mpileup|call|norm|view(mask)"
    else:
        pipeline_script += (
            f"| bcftools norm --threads {threads} -f /ref/{ref_q} -Oz -o /output/{out_q}\n"
        )
        pipeline_label = "mpileup|call|norm"
    pipeline_script += f"bcftools index --threads {threads} /output/{out_q}"

    # The pipeline (|) runs inside the Docker container shell, not on the host
    bcftools_cmd = ["docker", "run", "--rm"]
    if is_arm:
        bcftools_cmd.extend(["--platform", "linux/amd64"])
    bcftools_cmd.extend([
        f"--cpus={threads}",
        "-u", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{bam_path.parent}:/data",
        "-v", f"{reference_path.parent}:/ref",
        "-v", f"{output_vcf_path.parent}:/output",
    ])
    if mask_path is not None:
        # Read-only: the container only reads the mask regions.
        bcftools_cmd.extend(["-v", f"{mask_path.parent}:/mask:ro"])
    bcftools_cmd.extend([
        "quay.io/biocontainers/bcftools:1.20--h8b25389_0",
        "sh", "-lc",
        pipeline_script,
    ])

    try:
        result = subprocess.run(
            bcftools_cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )

        elapsed = time.time() - start_time

        if result.returncode != 0:
            error = result.stderr[-500:] if result.stderr else "BCFtools failed"
            if "Cannot connect to the Docker daemon" in str(result.stderr):
                error = "Docker not running"
            elif "Unable to find image" in str(result.stderr):
                error = "Run: docker pull quay.io/biocontainers/bcftools:1.20--h8b25389_0"
            elif "read group" in str(result.stderr).lower():
                error = "BAM lacks read groups. The platform BAM should include read groups — check the downloaded file."
            return {"success": False, "variant_count": 0, "error": error}

        if not output_vcf_path.exists():
            return {"success": False, "variant_count": 0, "error": "VCF not created"}

        return {
            "success": True,
            "variant_count": count_variants(output_vcf_path),
            "metadata": {
                "tool": "BCFtools",
                "version": "1.20",
                "runtime_seconds": elapsed,
                "region": region,
                "pipeline": pipeline_label
            }
        }

    except subprocess.TimeoutExpired:
        return {"success": False, "variant_count": 0, "error": f"Timeout after {timeout}s"}
    except FileNotFoundError:
        return {"success": False, "variant_count": 0, "error": "Docker not found"}
    except Exception as e:
        return {"success": False, "variant_count": 0, "error": str(e)}
