"""Shared helpers for variant-calling templates."""
import gzip
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def count_variants(vcf_path: Path) -> int:
    """Count non-header lines in a VCF file."""
    count = 0
    try:
        opener = gzip.open if str(vcf_path).endswith(".gz") else open
        with opener(vcf_path, "rt") as f:
            for line in f:
                if not line.startswith("#"):
                    count += 1
    except (OSError, gzip.BadGzipFile):
        logger.warning("Failed to count variants in %s", vcf_path)
    return count
