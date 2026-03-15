"""
GATK HaplotypeCaller template.

Requires: Docker with broadinstitute/gatk:4.5.0.0
"""
from pathlib import Path
from typing import Dict, Any
import logging
import subprocess
import time
import os
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
    """Run GATK HaplotypeCaller via Docker."""
    threads = min(config.get("threads", os.cpu_count() or 2), os.cpu_count() or 2)
    timeout = config.get("timeout", 1200)

    # Auto-detect available memory, reserve 512MB for OS/Docker overhead
    try:
        total_mem_bytes = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
        auto_mem_gb = max(1, int(total_mem_bytes / (1024**3)) - 1)
    except (ValueError, OSError, AttributeError):
        auto_mem_gb = 2  # Conservative default
    memory_gb = config.get("memory_gb", auto_mem_gb)

    bam_path = Path(bam_path).resolve()
    reference_path = Path(reference_path).resolve()
    output_vcf_path = Path(output_vcf_path).resolve()
    output_vcf_path.parent.mkdir(parents=True, exist_ok=True)

    region_validation = validate_region(region)
    if not region_validation["valid"]:
        return {
            "success": False,
            "variant_count": 0,
            "error": f"Region validation failed: {region_validation['error']}"
        }

    if not Path(f"{bam_path}.bai").exists() and not bam_path.with_suffix(".bam.bai").exists():
        return {"success": False, "variant_count": 0, "error": f"BAM index not found: {bam_path}.bai"}

    if not Path(f"{reference_path}.fai").exists():
        return {"success": False, "variant_count": 0, "error": f"Reference index not found: {reference_path}.fai"}

    gatk_options = config.get("gatk_options", {})
    validation_result = validate_and_build_flags("gatk", gatk_options)

    if not validation_result["valid"]:
        error_msg = f"Invalid GATK parameters: {'; '.join(validation_result['errors'])}"
        return {"success": False, "variant_count": 0, "error": error_msg}

    start_time = time.time()

    cmd = [
        "docker", "run", "--rm",
        f"--cpus={threads}", f"--memory={memory_gb}g",
        "-v", f"{bam_path.parent}:/data/bams:ro",
        "-v", f"{reference_path.parent}:/data/reference:ro",
        "-v", f"{output_vcf_path.parent}:/data/output",
        "broadinstitute/gatk:4.5.0.0",
        "gatk", "--java-options", f"-Xmx{memory_gb}g", "HaplotypeCaller",
        "-R", f"/data/reference/{reference_path.name}",
        "-I", f"/data/bams/{bam_path.name}",
        "-O", f"/data/output/{output_vcf_path.name}",
        "--native-pair-hmm-threads", str(threads),
        "-L", region,
    ]

    for flag in validation_result["flags"]:
        cmd.extend(str(flag).split())

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        elapsed = time.time() - start_time

        if result.returncode != 0:
            error = result.stderr[:500] if result.stderr else "GATK failed"
            if "Cannot connect to the Docker daemon" in str(result.stderr):
                error = "Docker not running"
            elif "Unable to find image" in str(result.stderr):
                error = "Run: docker pull broadinstitute/gatk:4.5.0.0"
            return {"success": False, "variant_count": 0, "error": error}

        if not output_vcf_path.exists():
            return {"success": False, "variant_count": 0, "error": "VCF not created"}

        return {
            "success": True,
            "variant_count": count_variants(output_vcf_path),
            "metadata": {"tool": "GATK", "version": "4.5.0.0", "runtime_seconds": elapsed}
        }

    except subprocess.TimeoutExpired:
        return {"success": False, "variant_count": 0, "error": f"Timeout after {timeout}s"}
    except FileNotFoundError:
        return {"success": False, "variant_count": 0, "error": "Docker not found"}
    except Exception as e:
        return {"success": False, "variant_count": 0, "error": str(e)}
