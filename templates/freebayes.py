"""
FreeBayes variant caller template.

Requires: Docker with staphb/freebayes:1.3.7
"""
from pathlib import Path
from typing import Dict, Any
import logging
import subprocess
import platform
import shlex
import time
import os
from .tool_params import validate_and_build_flags, validate_region
from ._common import container_name, count_variants, reap_container

logger = logging.getLogger(__name__)


def variant_call(
    bam_path: Path,
    reference_path: Path,
    output_vcf_path: Path,
    region: str,
    config: Dict[str, Any]
) -> Dict[str, Any]:
    """Run FreeBayes variant calling via Docker."""
    timeout = config.get("timeout", 1800)
    threads = min(config.get("threads", os.cpu_count() or 2), os.cpu_count() or 2)

    # Cap RAM as gatk.py and deepvariant.py do: the validator sizes its
    # concurrency assuming every caller job stays inside a container limit.
    try:
        total_mem_bytes = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
        auto_mem_gb = max(1, int(total_mem_bytes / (1024**3)) - 1)
    except (ValueError, OSError, AttributeError):
        auto_mem_gb = 2
    memory_gb = config.get("memory_gb", auto_mem_gb)

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

    bam_index = Path(f"{bam_path}.bai")
    if not bam_index.exists():
        bam_index = bam_path.with_suffix(".bam.bai")
        if not bam_index.exists():
            return {"success": False, "variant_count": 0, "error": f"BAM index not found: {bam_path}.bai"}

    ref_index = Path(f"{reference_path}.fai")
    if not ref_index.exists():
        return {"success": False, "variant_count": 0, "error": f"Reference index not found: {reference_path}.fai"}

    fb_options = config.get("freebayes_options", {})
    validation_result = validate_and_build_flags("freebayes", fb_options)

    if not validation_result["valid"]:
        error_msg = f"Invalid FreeBayes parameters: {'; '.join(validation_result['errors'])}"
        return {"success": False, "variant_count": 0, "error": error_msg}

    start_time = time.time()
    is_arm = platform.machine() == "arm64"

    # FreeBayes outputs uncompressed VCF to stdout, compress after
    temp_vcf = output_vcf_path.parent / f"{output_vcf_path.stem.replace('.vcf', '')}_temp.vcf"

    _cname = container_name("freebayes")
    # Bound before the try so the TimeoutExpired handler can tell whether the
    # compress container was ever started.
    _cname1 = None
    freebayes_cmd = ["docker", "run", "--rm", "--name", _cname]
    if is_arm:
        freebayes_cmd.extend(["--platform", "linux/amd64"])
    freebayes_cmd.extend([
        f"--cpus={threads}", f"--memory={memory_gb}g",
        "-v", f"{bam_path.parent}:/data:ro",
        "-v", f"{reference_path.parent}:/ref:ro",
        "staphb/freebayes:1.3.7",
        "freebayes",
        "-f", f"/ref/{reference_path.name}",
        "--region", region,
    ])

    for flag in validation_result["flags"]:
        freebayes_cmd.extend(str(flag).split())

    freebayes_cmd.append(f"/data/{bam_path.name}")

    try:
        with open(temp_vcf, 'w') as vcf_out:
            result = subprocess.run(
                freebayes_cmd,
                stdout=vcf_out,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout
            )

        if result.returncode != 0:
            # Keep both ends of stderr: the error can be at either, and a
            # tail-only slice may hold nothing but the echoed command line.
            _err = (result.stderr or "").strip()
            if _err:
                error = (_err if len(_err) <= 1200
                         else _err[:700] + "\n...[truncated]...\n" + _err[-500:])
            else:
                error = "FreeBayes failed"
            error = f"rc={result.returncode} {error}"
            if "Cannot connect to the Docker daemon" in str(result.stderr):
                error = "Docker not running"
            elif "Unable to find image" in str(result.stderr):
                error = "Run: docker pull staphb/freebayes:1.3.7"
            elif "read group" in str(result.stderr).lower():
                error = "BAM lacks read groups. The platform BAM should include read groups — check the downloaded file."
            if temp_vcf.exists():
                temp_vcf.unlink()
            return {"success": False, "variant_count": 0, "error": error}

        if not temp_vcf.exists() or temp_vcf.stat().st_size < 100:
            if temp_vcf.exists():
                temp_vcf.unlink()
            return {"success": False, "variant_count": 0, "error": "VCF not created or empty"}

        # Compress with bgzip and index via bcftools (runs inside Docker, not on host)
        _cname1 = container_name("freebayes-compress")
        compress_cmd = ["docker", "run", "--rm", "--name", _cname1]
        if is_arm:
            compress_cmd.extend(["--platform", "linux/amd64"])
        compress_cmd.extend([
            f"--cpus={threads}", f"--memory={memory_gb}g",
            "-v", f"{output_vcf_path.parent}:/data",
            "quay.io/biocontainers/bcftools:1.20--h8b25389_0",
            "sh", "-c",
            f"bgzip -@ {threads} -c /data/{shlex.quote(temp_vcf.name)} > /data/{shlex.quote(output_vcf_path.name)} && "
            f"bcftools index --threads {threads} /data/{shlex.quote(output_vcf_path.name)}",
        ])

        compress_result = subprocess.run(
            compress_cmd,
            capture_output=True,
            text=True,
            timeout=120
        )

        if temp_vcf.exists():
            temp_vcf.unlink()

        # The shell redirect creates the output file before bgzip writes a
        # byte, so exists() cannot tell success from a killed compress step:
        # branch on the returncode instead.
        if compress_result.returncode != 0:
            _cerr = (compress_result.stderr or compress_result.stdout or "").strip()
            error = f"VCF compression failed: rc={compress_result.returncode}"
            if _cerr:
                error = f"{error} {_cerr[-500:]}"
            if "Unable to find image" in str(compress_result.stderr):
                error = "Run: docker pull quay.io/biocontainers/bcftools:1.20--h8b25389_0"
            # A partial file must not survive to be scored as this round's
            # callset.
            if output_vcf_path.exists():
                output_vcf_path.unlink()
            return {"success": False, "variant_count": 0, "error": error}

        if not output_vcf_path.exists():
            return {"success": False, "variant_count": 0, "error": "VCF compression failed"}

        elapsed = time.time() - start_time

        return {
            "success": True,
            "variant_count": count_variants(output_vcf_path),
            "metadata": {
                "tool": "FreeBayes",
                "version": "1.3.7",
                "runtime_seconds": elapsed,
                "region": region
            }
        }

    except subprocess.TimeoutExpired:
        # Either subprocess.run can raise this, so both containers are reaped.
        reap_container(_cname)
        if _cname1:
            reap_container(_cname1)
        if temp_vcf.exists():
            temp_vcf.unlink()
        # A partial file must not survive to be scored as this round's
        # callset.
        if output_vcf_path.exists():
            output_vcf_path.unlink()
        return {"success": False, "variant_count": 0, "error": f"Timeout after {timeout}s"}
    except FileNotFoundError:
        return {"success": False, "variant_count": 0, "error": "Docker not found"}
    except Exception as e:
        if temp_vcf.exists():
            temp_vcf.unlink()
        return {"success": False, "variant_count": 0, "error": str(e)}
