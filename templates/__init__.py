"""
Miner template registry.

To add a new template:
1. Create a file in this directory (e.g., mycaller.py)
2. Implement variant_call(bam_path, reference_path, output_vcf_path, region, config) -> dict
3. Add entry to TEMPLATES below

Templates must return: {"success": bool, "variant_count": int, "error": str|None}
"""
import os
from pathlib import Path

TEMPLATES_DIR = Path(__file__).parent

# Map template name -> filename
# To add a template: add entry here and create the file
TEMPLATES = {
    "gatk": "gatk.py",
    "deepvariant": "deepvariant.py",
    "freebayes": "freebayes.py",
    "bcftools": "bcftools.py",
}

DEFAULT_TEMPLATE = "gatk"


def get_template_name():
    """Get template name from MINER_TEMPLATE env var or default."""
    return os.getenv("MINER_TEMPLATE", DEFAULT_TEMPLATE).lower()


def get_template_path(template_name=None):
    """Get path to template file."""
    if template_name is None:
        template_name = get_template_name()

    template_name = template_name.lower()
    filename = TEMPLATES.get(template_name)

    if not filename:
        raise ValueError(f"Unknown template: {template_name}. Available: {list(TEMPLATES.keys())}")

    path = TEMPLATES_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Template not found: {path}")

    return path


def load_template(template_name=None):
    """Load template module dynamically.

    Sets up proper package context so relative imports (e.g., from .tool_params)
    work correctly inside templates.
    """
    import importlib.util
    import sys

    path = get_template_path(template_name)

    # Register templates package in sys.modules so relative imports resolve
    if "templates" not in sys.modules:
        sys.modules["templates"] = sys.modules[__name__]

    module_name = f"templates.{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "templates"
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    if not hasattr(module, "variant_call"):
        raise AttributeError(f"Template must implement variant_call(): {path}")

    return module
