"""Shared helpers for variant-calling templates."""
import gzip
import logging
import subprocess
import uuid
import zlib
from pathlib import Path

logger = logging.getLogger(__name__)


def container_name(prefix: str) -> str:
    """Build a unique container name for one `docker run`.

    subprocess.run's timeout kills the docker CLI, not the container it
    started; without a name there is no handle to remove a container that is
    still holding its `--cpus`/`--memory` reservation.
    """
    return f"minos-{prefix}-{uuid.uuid4().hex[:12]}"


def reap_container(name: str) -> None:
    """Force-remove a container by name. Safe if it is already gone."""
    if not name:
        return
    try:
        result = subprocess.run(
            ["docker", "rm", "-f", name],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            logger.warning("Reaped orphaned container %s", name)
    except Exception as e:  # noqa: BLE001 - cleanup must never mask the real error
        logger.warning("Could not reap container %s: %s", name, e)


def count_variants(vcf_path: Path) -> int:
    """Count non-header lines in a VCF file."""
    count = 0
    try:
        opener = gzip.open if str(vcf_path).endswith(".gz") else open
        with opener(vcf_path, "rt") as f:
            for line in f:
                if not line.startswith("#"):
                    count += 1
    except (OSError, EOFError, zlib.error, UnicodeDecodeError):
        # A truncated .vcf.gz raises EOFError or zlib.error mid-stream rather
        # than BadGzipFile; an unreadable callset counts as zero here instead
        # of raising out of the template.
        logger.warning("Failed to count variants in %s", vcf_path)
    return count
