"""
Google DeepVariant template.

Requires: Docker with google/deepvariant:1.5.0
Note: DeepVariant needs more memory (~16GB) than GATK.
"""
from pathlib import Path
from typing import Dict, Any
import subprocess

from ._common import container_name, count_variants, reap_container
import time
import shutil
import os
from .tool_params import validate_and_build_flags, validate_region


def variant_call(
    bam_path: Path,
    reference_path: Path,
    output_vcf_path: Path,
    region: str,
    config: Dict[str, Any]
) -> Dict[str, Any]:
    """Run DeepVariant via Docker."""
    threads = min(config.get("threads", os.cpu_count() or 2), os.cpu_count() or 2)
    timeout = config.get("timeout", 1800)  # DeepVariant needs more time

    bam_path = Path(bam_path).resolve()
    reference_path = Path(reference_path).resolve()
    output_vcf_path = Path(output_vcf_path).resolve()
    output_vcf_path.parent.mkdir(parents=True, exist_ok=True)

    # SECURITY: Validate region format to prevent command injection
    region_validation = validate_region(region)
    if not region_validation["valid"]:
        return {
            "success": False,
            "variant_count": 0,
            "error": f"Region validation failed: {region_validation['error']}"
        }

    # Check required index files
    if not Path(f"{bam_path}.bai").exists() and not bam_path.with_suffix(".bam.bai").exists():
        return {"success": False, "variant_count": 0, "error": f"BAM index not found: {bam_path}.bai"}

    if not Path(f"{reference_path}.fai").exists():
        return {"success": False, "variant_count": 0, "error": f"Reference index not found: {reference_path}.fai"}

    # Validate and build DeepVariant-specific quality parameter flags
    dv_options = config.get("deepvariant_options", {})
    validation_result = validate_and_build_flags("deepvariant", dv_options)

    if not validation_result["valid"]:
        error_msg = f"Invalid DeepVariant parameters: {'; '.join(validation_result['errors'])}"
        return {"success": False, "variant_count": 0, "error": error_msg}

    # Extract model_type (default to WGS if not specified)
    model_type = dv_options.get("model_type", "WGS")

    # Separate direct flags from make_examples / postprocess_variants extra args
    direct_flags = []
    make_examples_args = []
    postprocess_variants_args = []
    for f in validation_result["flags"]:
        if isinstance(f, dict) and f.get("stage") == "make_examples":
            make_examples_args.append(f["param"])
        elif isinstance(f, dict) and f.get("stage") == "postprocess_variants":
            postprocess_variants_args.append(f["param"])
        elif isinstance(f, str) and not f.startswith("--model_type"):
            direct_flags.append(f)

    start_time = time.time()
    output_name = output_vcf_path.stem.replace(".vcf", "")
    # Per-output intermediate dir. Validators run multiple DeepVariant miners
    # concurrently from the same parent dir; a fixed "dv_intermediate" name
    # would collide and the finally-block rmtree would delete a peer's data.
    intermediate_subdir = f"dv_intermediate_{output_name}"
    intermediate_dir = output_vcf_path.parent / intermediate_subdir
    intermediate_dir.mkdir(parents=True, exist_ok=True)

    final_vcf = output_vcf_path.parent / f"{output_name}.vcf.gz"

    # Auto-detect available memory. Fallback floor is DeepVariant's 16 GB
    # minimum to avoid silent OOM if sysconf is unavailable.
    try:
        total_mem_bytes = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
        auto_mem_gb = max(1, int(total_mem_bytes / (1024**3)) - 1)
    except (ValueError, OSError, AttributeError):
        auto_mem_gb = 16
    memory_gb = config.get("memory_gb", auto_mem_gb)

    _cname = container_name("deepvariant")
    cmd = [
        "docker", "run", "--rm", "--name", _cname,
        f"--cpus={threads}", f"--memory={memory_gb}g",
        "-v", f"{bam_path.parent}:/input:ro",
        "-v", f"{reference_path.parent}:/reference:ro",
        "-v", f"{output_vcf_path.parent}:/output",
        "google/deepvariant:1.5.0",
        "/opt/deepvariant/bin/run_deepvariant",
        f"--model_type={model_type}",
        f"--ref=/reference/{reference_path.name}",
        f"--reads=/input/{bam_path.name}",
        f"--output_vcf=/output/{final_vcf.name}",
        f"--output_gvcf=/output/{output_name}.g.vcf.gz",
        f"--intermediate_results_dir=/output/{intermediate_subdir}",
        f"--num_shards={threads}",
        f"--regions={region}",
    ]

    # Append direct quality flags (e.g. future run_deepvariant flags)
    for flag in direct_flags:
        cmd.extend(str(flag).split())

    # Pass make_examples params via --make_examples_extra_args
    # These are internal DeepVariant flags that run_deepvariant doesn't accept directly
    if make_examples_args:
        cmd.append(f'--make_examples_extra_args={",".join(make_examples_args)}')

    # Pass postprocess_variants params via --postprocess_variants_extra_args
    if postprocess_variants_args:
        cmd.append(f'--postprocess_variants_extra_args={",".join(postprocess_variants_args)}')

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        elapsed = time.time() - start_time

        if result.returncode != 0:
            # Strip TF INFO log lines (they dominate DV stderr) and keep the
            # tail of what's left, where the actual error lives.
            raw = result.stderr or ""
            kept = "\n".join(
                line for line in raw.splitlines()
                if not line.startswith("I0")
                and "I tensorflow" not in line
                and not line.lstrip().startswith("To enable")
            )
            error = (kept or raw)[-500:] or "DeepVariant failed"
            if "Cannot connect to the Docker daemon" in str(result.stderr):
                error = "Docker not running"
            elif "Unable to find image" in str(result.stderr):
                error = "Run: docker pull google/deepvariant:1.5.0"
            elif "out of memory" in str(result.stderr).lower():
                error = "DeepVariant needs at least 16GB RAM"
            return {"success": False, "variant_count": 0, "error": error}

        # Find output file
        vcf_to_read = final_vcf if final_vcf.exists() else output_vcf_path
        if not vcf_to_read.exists():
            return {"success": False, "variant_count": 0, "error": "VCF not created"}

        if final_vcf != output_vcf_path and final_vcf.exists():
            shutil.copy(final_vcf, output_vcf_path)

        return {
            "success": True,
            "variant_count": count_variants(vcf_to_read),
            "metadata": {"tool": "DeepVariant", "version": "1.5.0", "runtime_seconds": elapsed}
        }

    except subprocess.TimeoutExpired:
        reap_container(_cname)
        return {"success": False, "variant_count": 0, "error": f"Timeout after {timeout}s"}
    except FileNotFoundError:
        return {"success": False, "variant_count": 0, "error": "Docker not found"}
    except Exception as e:
        return {"success": False, "variant_count": 0, "error": str(e)}
    finally:
        # Cleanup
        if intermediate_dir.exists():
            shutil.rmtree(intermediate_dir, ignore_errors=True)
