"""Minos Subnet Neurons - Miners and Validators.

Miner and Validator are meant to be run as scripts via:
  python -m neurons.miner
  python -m neurons.validator

They are not imported as library components.
"""

MINOS_SPEC_VERSION = "0.1.0"
SPEC_STRING = MINOS_SPEC_VERSION.split(".")
SPEC_VERSION_MAJOR = 100*int(SPEC_STRING[0])
SPEC_VERSION_MINOR = 10*int(SPEC_STRING[1])
SPEC_VERSION_PATCH = int(SPEC_STRING[2])

__SPEC_VERSION__ = SPEC_VERSION_MAJOR + SPEC_VERSION_MINOR + SPEC_VERSION_PATCH
