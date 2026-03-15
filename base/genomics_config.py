"""Genomics configuration for the variant-calling subnet."""

import os
import subprocess
from pathlib import Path


def is_local_network() -> bool:
    """Check if running on a local Bittensor network."""
    network = os.getenv("SUBTENSOR_NETWORK", "").lower()
    endpoint = os.getenv("SUBTENSOR_ENDPOINT", "").lower()
    return (
        network in ["local", "localhost", "localnet"] or
        "127.0.0.1" in endpoint or
        "localhost" in endpoint or
        "192.168." in endpoint  # Common local IP ranges
    )

# Base paths
BASE_DIR = Path(__file__).parent.parent  # project root (parent of base/)

# Genomics task configuration
GENOMICS_CONFIG = {
    # Scoring parameters
    "ema_alpha": 0.1,  # EMA smoothing factor (used by ScoreTracker)

    # Docker settings
    "happy_docker_image": "genonet/hap-py@sha256:03acabe84bbfba35f5a7234129d524c563f5657e1f21150a2ea2797f8e6d05f2",

    # Timeout settings (adjusted for local networks)
    "variant_calling_timeout": 7200 if is_local_network() else 3600,  # 2h local, 1h production
    "task_interval": int(os.getenv("TASK_INTERVAL", "600" if is_local_network() else "3600")),  # 10m local, 1h production
}

# Validator specific settings
VALIDATOR_CONFIG = {
    "scoring_interval": 1,  # Steps between weight updates (set weights every round)
}

# Miner specific settings
MINER_CONFIG = {
    "default_caller": "gatk",  # Default variant caller - use real GATK for production
    "num_threads": 4,  # Threads per task
}


def is_docker_available() -> bool:
    """Check if Docker is available and daemon is running."""
    try:
        # Check docker is installed
        result = subprocess.run(["docker", "--version"], capture_output=True, timeout=5)
        if result.returncode != 0:
            return False
        # Check daemon is running (docker info fails if daemon not running)
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return False
