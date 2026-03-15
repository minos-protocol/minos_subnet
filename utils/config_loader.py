"""
Config Loader Utility

Loads tool configuration from .conf files in configs/ directory.
Format: key=value with # comments. Miners edit these to tune quality params.
"""
from pathlib import Path
from typing import Dict, Any


# Base directory for config files
CONFIG_DIR = Path(__file__).parent.parent / "configs"

# Tool versions (static — tied to Docker image tags)
TOOL_VERSIONS = {
    "gatk": "4.5.0.0",
    "deepvariant": "1.5.0",
    "freebayes": "1.3.7",
    "bcftools": "1.20",
}


def _parse_value(raw: str) -> Any:
    """Parse a string value into the appropriate Python type."""
    raw = raw.strip()

    # Booleans
    if raw.lower() == "true":
        return True
    if raw.lower() == "false":
        return False

    # Integers
    try:
        return int(raw)
    except ValueError:
        pass

    # Floats
    try:
        return float(raw)
    except ValueError:
        pass

    # String (as-is)
    return raw


def extract_tool_options(tool: str) -> Dict[str, Any]:
    """
    Load tool-specific quality parameters from .conf file.

    Args:
        tool: Tool name (gatk, deepvariant, freebayes, bcftools)

    Returns:
        Dict with parameter names and their values.
        Example: {"min_base_quality_score": 10, "pcr_indel_model": "CONSERVATIVE"}

    Raises:
        FileNotFoundError: If config file doesn't exist
        ValueError: If config file has invalid lines
    """
    config_file = CONFIG_DIR / f"{tool}.conf"

    if not config_file.exists():
        raise FileNotFoundError(f"Config file not found: {config_file}")

    options = {}

    with open(config_file, 'r') as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()

            # Skip empty lines and comments
            if not line or line.startswith("#"):
                continue

            if "=" not in line:
                raise ValueError(f"{config_file}:{line_num}: Invalid line (missing '='): {line}")

            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()

            if not key:
                raise ValueError(f"{config_file}:{line_num}: Empty key")

            options[key] = _parse_value(value)

    return options


def get_tool_version(tool: str) -> str:
    """Get tool version string."""
    return TOOL_VERSIONS.get(tool, "1.0.0")


if __name__ == "__main__":
    for tool in ["gatk", "deepvariant", "freebayes", "bcftools"]:
        print(f"\n{tool.upper()} Config:")
        print("=" * 60)
        try:
            options = extract_tool_options(tool)
            print(f"Loaded {len(options)} parameters:")
            for param, value in sorted(options.items()):
                print(f"  {param}: {value} ({type(value).__name__})")
        except Exception as e:
            print(f"  ERROR: {e}")
