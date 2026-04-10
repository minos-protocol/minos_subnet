"""Minos Status Check — Read-only environment health checker.

Run from the minos_subnet/ directory:
    python neurons/status.py           # Rich table output
    python neurons/status.py --json    # JSON for platform/dashboard
    python neurons/status.py --role miner  # Override auto-detected role

Exit code: 0 if all critical checks pass, 1 if any fail.
"""

import sys
import os
import json
import shutil
import subprocess
import platform
import argparse
import urllib.request
import urllib.error
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional, Any, Callable

# Path setup (same pattern as miner.py / validator.py)
BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env.miner")
    load_dotenv(BASE_DIR / ".env.validator")
    load_dotenv()
except ImportError:
    pass  # dotenv missing is itself a check


# ---------------------------------------------------------------------------
# Docker image constants (source of truth: setup.py lines 88-112)
# ---------------------------------------------------------------------------

MINER_DOCKER_IMAGES = {
    "gatk": [
        "broadinstitute/gatk:4.5.0.0",
        "quay.io/biocontainers/samtools:1.20--h50ea8bc_0",
        "quay.io/biocontainers/bcftools:1.20--h8b25389_0",
    ],
    "deepvariant": [
        "google/deepvariant:1.5.0",
    ],
    "freebayes": [
        "staphb/freebayes:1.3.7",
    ],
    "bcftools": [
        "quay.io/biocontainers/bcftools:1.20--h8b25389_0",
    ],
}

VALIDATOR_DOCKER_IMAGES = [
    "genonet/hap-py:0.3.15",
    "broadinstitute/gatk:4.5.0.0",
    "google/deepvariant:1.5.0",
    "staphb/freebayes:1.3.7",
    "quay.io/biocontainers/bcftools:1.20--h8b25389_0",
    "quay.io/biocontainers/samtools:1.20--h50ea8bc_0",
]

# Supported chromosomes
SUPPORTED_CHROMOSOMES = [f"chr{i}" for i in range(1, 23)]  # chr1-chr22

# Reference files — multi-chromosome (new structure: datasets/reference/{chr}/{chr}.fa)
# Falls back to checking old flat structure (datasets/reference/chr20.fa) if new not found
def _build_reference_files(chromosomes):
    files = []
    for chrom in chromosomes:
        files.append(f"datasets/reference/{chrom}/{chrom}.fa")
        files.append(f"datasets/reference/{chrom}/{chrom}.fa.fai")
        files.append(f"datasets/reference/{chrom}/{chrom}.dict")
    return files

MINER_REFERENCE_FILES = _build_reference_files(SUPPORTED_CHROMOSOMES)

# Truth VCFs are served per-round from the platform API — not stored locally
VALIDATOR_REFERENCE_FILES = MINER_REFERENCE_FILES

VALIDATOR_REFERENCE_DIRS = [
    f"datasets/reference/{chrom}/{chrom}.sdf" for chrom in SUPPORTED_CHROMOSOMES
]

# S3 health check URL
S3_HEALTH_URL = "https://genotypenet-platform.s3.us-east-1.amazonaws.com/reference_data/donors/reference/chr20/chr20.fa.fai"

# Key Python packages to check
REQUIRED_PACKAGES = {
    "bittensor": "bittensor",
    "bittensor_wallet": "bittensor-wallet",
    "httpx": "httpx",
    "boto3": "boto3",
    "pysam": "pysam",
    "numpy": "numpy",
    "torch": "torch",
    "dotenv": "python-dotenv",
}


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class Status(Enum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIP = "skip"


@dataclass
class Check:
    name: str
    status: Status
    detail: str = ""
    category: str = "environment"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status.value,
            "detail": self.detail,
            "category": self.category,
        }


@dataclass
class StatusReport:
    role: str
    template: Optional[str] = None
    checks: List[Check] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.status == Status.PASS)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if c.status == Status.FAIL)

    @property
    def warned(self) -> int:
        return sum(1 for c in self.checks if c.status == Status.WARN)

    @property
    def total(self) -> int:
        return len(self.checks)

    @property
    def all_passed(self) -> bool:
        return all(c.status in (Status.PASS, Status.WARN, Status.SKIP) for c in self.checks)

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "template": self.template,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "hostname": platform.node(),
            "summary": {
                "total": self.total,
                "passed": self.passed,
                "failed": self.failed,
                "warned": self.warned,
            },
            "checks": [c.to_dict() for c in self.checks],
        }


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_python_version() -> Check:
    vi = sys.version_info
    v = f"{vi.major}.{vi.minor}.{vi.micro}"
    if vi >= (3, 10):
        return Check("Python version", Status.PASS, v)
    return Check("Python version", Status.FAIL, f"{v} (need 3.10+)")


def check_docker_daemon() -> Check:
    try:
        result = subprocess.run(
            ["docker", "--version"], capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return Check("Docker", Status.FAIL, "docker command failed")

        import re
        match = re.search(r"(\d+\.\d+\.\d+)", result.stdout)
        version = match.group(1) if match else "unknown"

        result = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            if "permission denied" in (result.stderr or "").lower():
                return Check("Docker", Status.FAIL, "permission denied (add user to docker group)")
            return Check("Docker", Status.FAIL, "daemon not running")

        return Check("Docker", Status.PASS, f"daemon running ({version})")

    except FileNotFoundError:
        return Check("Docker", Status.FAIL, "not installed")
    except subprocess.TimeoutExpired:
        return Check("Docker", Status.FAIL, "daemon not responding (timeout)")


def check_docker_images(role: str, template: Optional[str]) -> List[Check]:
    if role == "miner" and template:
        images = MINER_DOCKER_IMAGES.get(template, [])
    elif role == "miner":
        images = MINER_DOCKER_IMAGES.get("gatk", [])
    else:
        images = VALIDATOR_DOCKER_IMAGES

    results = []
    for image in images:
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", image],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                results.append(Check(f"Image: {image}", Status.PASS, "pulled"))
            else:
                results.append(Check(f"Image: {image}", Status.FAIL, "not pulled"))
        except (FileNotFoundError, subprocess.TimeoutExpired):
            results.append(Check(f"Image: {image}", Status.SKIP, "docker unavailable"))

    return results


def check_reference_files(role: str) -> List[Check]:
    files = VALIDATOR_REFERENCE_FILES if role == "validator" else MINER_REFERENCE_FILES
    dirs = VALIDATOR_REFERENCE_DIRS if role == "validator" else []

    results = []
    for rel_path in files:
        path = BASE_DIR / rel_path
        name = Path(rel_path).name
        if path.exists() and path.stat().st_size > 0:
            size_mb = path.stat().st_size / (1024 * 1024)
            results.append(Check(f"Reference: {name}", Status.PASS, f"{size_mb:.1f} MB"))
        else:
            results.append(Check(f"Reference: {name}", Status.FAIL, "missing"))

    for rel_path in dirs:
        path = BASE_DIR / rel_path
        name = Path(rel_path).name
        if path.exists() and path.is_dir() and any(path.iterdir()):
            results.append(Check(f"Reference: {name}/", Status.PASS, "directory exists"))
        else:
            results.append(Check(f"Reference: {name}/", Status.FAIL, "missing or empty"))

    return results


def check_disk_space(role: str) -> Check:
    try:
        usage = shutil.disk_usage(BASE_DIR)
        free_gb = usage.free / (1024 ** 3)
        min_gb = 50 if role == "validator" else 20

        if free_gb >= min_gb:
            return Check("Disk space", Status.PASS, f"{free_gb:.0f} GB free")
        return Check("Disk space", Status.WARN, f"{free_gb:.0f} GB free (recommend {min_gb}+ GB)")
    except Exception as e:
        return Check("Disk space", Status.FAIL, str(e))


def check_ram(role: str, template: Optional[str]) -> Check:
    try:
        if platform.system() == "Darwin":
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5
            )
            ram_gb = int(result.stdout.strip()) / (1024 ** 3)
        else:
            ram_gb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 ** 3)

        min_gb = 16 if (role == "validator" or template == "deepvariant") else 8

        if ram_gb >= min_gb:
            return Check("RAM", Status.PASS, f"{ram_gb:.0f} GB")
        return Check("RAM", Status.WARN, f"{ram_gb:.0f} GB (recommend {min_gb}+ GB)")
    except Exception as e:
        return Check("RAM", Status.FAIL, str(e))


def check_python_deps() -> Check:
    missing = []
    for module, pkg_name in REQUIRED_PACKAGES.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(pkg_name)

    if not missing:
        return Check("Python deps", Status.PASS, f"all {len(REQUIRED_PACKAGES)} importable")
    return Check("Python deps", Status.FAIL, f"missing: {', '.join(missing)}")


def check_platform_api() -> Check:
    url = os.getenv("PLATFORM_URL", "https://api.theminos.ai").rstrip("/")
    try:
        req = urllib.request.Request(f"{url}/health", method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                host = url.split("//")[-1]
                return Check("Platform API", Status.PASS, f"reachable ({host})", "network")
            return Check("Platform API", Status.WARN, f"HTTP {resp.status}", "network")
    except Exception as e:
        return Check("Platform API", Status.FAIL, _short_error(e), "network")


def check_s3_access() -> Check:
    try:
        req = urllib.request.Request(S3_HEALTH_URL, method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return Check("S3 access", Status.PASS, "reachable", "network")
            return Check("S3 access", Status.WARN, f"HTTP {resp.status}", "network")
    except Exception as e:
        return Check("S3 access", Status.FAIL, _short_error(e), "network")


def check_wallet() -> Check:
    wallet_name = os.getenv("WALLET_NAME", "default")
    wallet_hotkey = os.getenv("WALLET_HOTKEY", "default")
    hotkey_path = Path.home() / ".bittensor" / "wallets" / wallet_name / "hotkeys" / wallet_hotkey

    if not hotkey_path.exists():
        return Check("Wallet", Status.FAIL, f"{wallet_name}/{wallet_hotkey} not found", "functional")

    try:
        data = json.loads(hotkey_path.read_text())
        ss58 = data.get("ss58Address", "unknown")
        return Check("Wallet", Status.PASS, f"{wallet_name}/{wallet_hotkey} ({ss58[:12]}...)", "functional")
    except Exception as e:
        return Check("Wallet", Status.FAIL, f"cannot load: {_short_error(e)}", "functional")


def check_bittensor_chain() -> Check:
    try:
        import concurrent.futures

        def _connect():
            import bittensor as bt
            if not hasattr(bt, "subtensor"):
                bt.subtensor = bt.Subtensor
            network = os.getenv("SUBTENSOR_NETWORK", "finney")
            sub = bt.subtensor(network=network)
            block = sub.block
            return f"{network} (block {block})"

        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(_connect)
            detail = future.result(timeout=30)
            return Check("Bittensor chain", Status.PASS, detail, "network")

    except concurrent.futures.TimeoutError:
        return Check("Bittensor chain", Status.WARN, "connection timeout (30s)", "network")
    except ImportError:
        return Check("Bittensor chain", Status.SKIP, "bittensor not installed", "network")
    except Exception as e:
        return Check("Bittensor chain", Status.WARN, _short_error(e), "network")


def check_env_file() -> Check:
    miner_env = BASE_DIR / ".env.miner"
    validator_env = BASE_DIR / ".env.validator"
    generic_env = BASE_DIR / ".env"

    for path in [miner_env, validator_env, generic_env]:
        if path.exists():
            return Check("Environment file", Status.PASS, path.name)

    return Check("Environment file", Status.WARN, "no .env file found")


def check_config_files(template: Optional[str]) -> Check:
    if not template:
        return Check("Tool config", Status.SKIP, "no template set")

    config_path = BASE_DIR / "configs" / f"{template}.conf"
    if config_path.exists():
        try:
            from utils.config_loader import extract_tool_options
            options = extract_tool_options(template)
            return Check("Tool config", Status.PASS, f"{template}.conf ({len(options)} params)")
        except Exception as e:
            return Check("Tool config", Status.FAIL, f"{template}.conf parse error: {_short_error(e)}")
    return Check("Tool config", Status.FAIL, f"{template}.conf missing")


def check_imports() -> Check:
    """Verify that all subnet modules can be imported without error."""
    errors = []
    modules = [
        "templates.tool_params",
        "utils.scoring",
        "utils.weight_tracking",
        "utils.platform_client",
        "utils.config_loader",
        "utils.path_utils",
        "utils.file_utils",
        "base.genomics_config",
    ]
    for mod in modules:
        try:
            __import__(mod)
        except Exception as e:
            errors.append(f"{mod}: {type(e).__name__}")

    if not errors:
        return Check("Module imports", Status.PASS, f"all {len(modules)} modules OK")
    return Check("Module imports", Status.FAIL, "; ".join(errors))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _short_error(e: Exception) -> str:
    """Truncate exception to a readable string."""
    msg = str(e)
    if len(msg) > 80:
        msg = msg[:77] + "..."
    return msg or type(e).__name__


def safe_run(fn: Callable, *args) -> Any:
    """Run a check function, catching crashes."""
    try:
        return fn(*args)
    except Exception as e:
        name = getattr(fn, "__name__", "unknown")
        return Check(name, Status.FAIL, f"check crashed: {_short_error(e)}")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_checks(role: str, template: Optional[str]) -> StatusReport:
    report = StatusReport(role=role, template=template)

    # Environment checks
    report.checks.append(safe_run(check_python_version))
    report.checks.append(safe_run(check_env_file))
    report.checks.append(safe_run(check_imports))
    report.checks.append(safe_run(check_python_deps))
    report.checks.append(safe_run(check_config_files, template))
    report.checks.append(safe_run(check_disk_space, role))
    report.checks.append(safe_run(check_ram, role, template))

    # Docker checks
    report.checks.append(safe_run(check_docker_daemon))
    docker_results = safe_run(check_docker_images, role, template)
    if isinstance(docker_results, list):
        report.checks.extend(docker_results)
    else:
        report.checks.append(docker_results)

    # Reference files
    ref_results = safe_run(check_reference_files, role)
    if isinstance(ref_results, list):
        report.checks.extend(ref_results)
    else:
        report.checks.append(ref_results)

    # Network checks
    report.checks.append(safe_run(check_platform_api))
    report.checks.append(safe_run(check_s3_access))
    report.checks.append(safe_run(check_bittensor_chain))

    # Functional checks
    report.checks.append(safe_run(check_wallet))

    return report


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_STATUS_SYMBOLS = {
    Status.PASS: ("\u2713", "green"),   # ✓
    Status.FAIL: ("\u2717", "red"),     # ✗
    Status.WARN: ("!", "yellow"),
    Status.SKIP: ("-", "dim"),
}


def render_terminal(report: StatusReport):
    """Render report to terminal, using rich if available."""
    try:
        from rich.console import Console
        from rich.panel import Panel
        _render_rich(report)
    except ImportError:
        _render_plain(report)


def _render_rich(report: StatusReport):
    from rich.console import Console
    from rich.table import Table

    console = Console()

    role_label = report.role.capitalize()
    if report.template:
        role_label += f" - {report.template.upper()}"

    table = Table(
        show_header=False,
        show_edge=False,
        padding=(0, 1),
        title=f"[bold]Minos Status Check ({role_label})[/]",
        title_style="bold white",
    )
    table.add_column("status", width=3, justify="center")
    table.add_column("name", min_width=25)
    table.add_column("detail", style="dim")

    for check in report.checks:
        sym, color = _STATUS_SYMBOLS[check.status]
        table.add_row(f"[{color}]{sym}[/{color}]", check.name, check.detail)

    console.print()
    console.print(table)
    console.print()

    passed = report.passed
    total = report.total
    failed = report.failed
    color = "green" if failed == 0 else "red"
    console.print(f"  [{color}]{passed}/{total} checks passed[/{color}]", highlight=False)

    if failed > 0:
        console.print(f"  [red]{failed} failed[/red] — run [bold]python setup.py[/bold] to fix", highlight=False)
    console.print()


def _render_plain(report: StatusReport):
    role_label = report.role.capitalize()
    if report.template:
        role_label += f" - {report.template.upper()}"

    print(f"\nMinos Status Check ({role_label})")
    print("=" * 50)

    for check in report.checks:
        sym = {Status.PASS: "[PASS]", Status.FAIL: "[FAIL]", Status.WARN: "[WARN]", Status.SKIP: "[SKIP]"}
        detail = f" — {check.detail}" if check.detail else ""
        print(f"  {sym[check.status]:8s} {check.name}{detail}")

    print("=" * 50)
    print(f"  {report.passed}/{report.total} checks passed")
    if report.failed > 0:
        print(f"  {report.failed} failed — run 'python setup.py' to fix")
    print()


def render_json(report: StatusReport) -> str:
    return json.dumps(report.to_dict(), indent=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Minos Status Check — verify miner/validator environment health"
    )
    parser.add_argument("--json", action="store_true", help="Output JSON (for platform/dashboard)")
    parser.add_argument("--role", choices=["miner", "validator"], help="Override auto-detected role")
    args = parser.parse_args()

    # Detect role from environment
    template = os.getenv("MINER_TEMPLATE", "").lower().strip() or None
    if args.role:
        role = args.role
    elif template:
        role = "miner"
    else:
        role = "validator"

    report = run_checks(role, template)

    if args.json:
        print(render_json(report))
    else:
        render_terminal(report)

    return 0 if report.all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
