"""Minos Subnet Neurons - Miners and Validators.

Miner and Validator are meant to be run as scripts via:
  python -m neurons.miner
  python -m neurons.validator

They are not imported as library components, but both entry points share the
spec version and the chromosome allowlist defined here.
"""

import re

MINOS_SPEC_VERSION = "0.3.0"
SPEC_STRING = MINOS_SPEC_VERSION.split(".")

# 100*major + 10*minor + patch, submitted as the on-chain version_key. The
# subnet's WeightsVersionKey hyperparameter uses the same scale and the chain
# rejects a validator whose key is below it, so widening a field here requires
# raising that hyperparameter in the same release.
#
# One decimal digit per field means 0.10.0 and 1.0.0 both pack to 100, so the
# packing needs widening before a 0.10.0 release.
SPEC_VERSION_FIELD_WIDTH = 10

if not all(0 <= int(part) < SPEC_VERSION_FIELD_WIDTH for part in SPEC_STRING[1:]):
    raise ValueError(
        f"MINOS_SPEC_VERSION={MINOS_SPEC_VERSION!r}: minor and patch must each be "
        f"below {SPEC_VERSION_FIELD_WIDTH} to pack without collision"
    )

SPEC_VERSION_MAJOR = SPEC_VERSION_FIELD_WIDTH * SPEC_VERSION_FIELD_WIDTH * int(SPEC_STRING[0])
SPEC_VERSION_MINOR = SPEC_VERSION_FIELD_WIDTH * int(SPEC_STRING[1])
SPEC_VERSION_PATCH = int(SPEC_STRING[2])

__SPEC_VERSION__ = SPEC_VERSION_MAJOR + SPEC_VERSION_MINOR + SPEC_VERSION_PATCH


# GRCh38 primary contigs the datasets/ tree is laid out for. Same set as
# templates.tool_params.REGION_PATTERN, which guards the region on its way into
# the variant-caller command line — but that check runs inside variant_call(),
# after the validator and miner have already built reference/truth paths out of
# the chromosome. round_id goes through safe_round_dir_name(); the chromosome
# becomes a path component, so it is allowlisted here before any path is built
# from it, and a region such as "../../etc/x:1-2" is rejected rather than
# resolved.
CHROMOSOME_PATTERN = re.compile(r"^chr([1-9]|1[0-9]|2[0-2]|X|Y|M)$")


def safe_chrom(region, default="chr20"):
    """Chromosome named by ``region``, or ``None`` when it is not allowlisted.

    Args:
        region: Region string such as ``"chr20:10000000-15000000"``.
        default: Returned when ``region`` is empty or ``None``, preserving the
            historical ``region.split(":")[0] if region else "chr20"`` default.

    Returns:
        The chromosome (e.g. ``"chr20"``), ``default`` for an empty region, or
        ``None`` when the region names anything outside chr1-22/X/Y/M. Callers
        must treat ``None`` as fatal rather than falling back to a default —
        silently substituting chr20 would score against the wrong reference.
    """
    if region is None or region == "":
        return default
    if not isinstance(region, str):
        return None
    chrom = region.split(":")[0]
    return chrom if CHROMOSOME_PATTERN.match(chrom) else None
