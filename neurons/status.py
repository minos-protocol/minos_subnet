"""Minos Status Check — Read-only environment health checker.

Run from the minos_subnet/ directory:
    python neurons/status.py           # Rich table output
    python neurons/status.py --json    # JSON for platform/dashboard
    python neurons/status.py --role miner  # Override auto-detected role

Role is auto-detected as miner unless --role says otherwise or an .env file in
this checkout identifies the node as a validator; see detect_role().

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
    "bcftools": [
        "quay.io/biocontainers/bcftools:1.20--h8b25389_0",
    ],
    # freebayes is intentionally absent — see DEPRECATED_TEMPLATES in
    # templates/__init__.py and the runtime block in neurons/miner.py.
}

VALIDATOR_DOCKER_IMAGES = [
    "genonet/hap-py@sha256:03acabe84bbfba35f5a7234129d524c563f5657e1f21150a2ea2797f8e6d05f2",
    "broadinstitute/gatk:4.5.0.0",
    "google/deepvariant:1.5.0",
    # Retained only so validators can replay in-flight pre-cutover rounds;
    # removed in a follow-up release.
    "staphb/freebayes:1.3.7",
    "quay.io/biocontainers/bcftools:1.20--h8b25389_0",
    "quay.io/biocontainers/samtools:1.20--h50ea8bc_0",
]

# Supported chromosomes
SUPPORTED_CHROMOSOMES = [f"chr{i}" for i in range(1, 23)]  # chr1-chr22


def _demo_mode() -> bool:
    return os.getenv("MINER_DEMO", "").strip().lower() in ("1", "true", "yes", "on")


def expected_chromosomes() -> List[str]:
    """Chromosomes setup.py actually installs on this node.

    setup.py step_reference_data() filters its download list down to "/chr20/"
    whenever MINER_DEMO is truthy, so a demo install has only chr20 present.
    Requiring all 22 here would report the rest as missing on a node whose
    install completed correctly.
    """
    return ["chr20"] if _demo_mode() else list(SUPPORTED_CHROMOSOMES)


# Reference files exist only in the per-chromosome layout
# datasets/reference/{chrom}/{chrom}.fa. A flat layout is not something setup.py
# can leave behind: setup.py._migrate_legacy_reference_data() MOVES a legacy
# datasets/reference/chr20.* into the per-chromosome directory before anything
# else runs, so accepting flat paths would only report a half-migrated tree
# (which the callers then fail to open) as healthy.
def _build_reference_files(chromosomes):
    files = []
    for chrom in chromosomes:
        files.append(f"datasets/reference/{chrom}/{chrom}.fa")
        files.append(f"datasets/reference/{chrom}/{chrom}.fa.fai")
        files.append(f"datasets/reference/{chrom}/{chrom}.dict")
    return files


def _build_reference_dirs(chromosomes):
    return [f"datasets/reference/{chrom}/{chrom}.sdf" for chrom in chromosomes]


MINER_REFERENCE_FILES = _build_reference_files(SUPPORTED_CHROMOSOMES)

# Truth VCFs are served per-round from the platform API — not stored locally
VALIDATOR_REFERENCE_FILES = MINER_REFERENCE_FILES

VALIDATOR_REFERENCE_DIRS = _build_reference_dirs(SUPPORTED_CHROMOSOMES)

# Reference data health check URL — uses platform redirect endpoint so we
# stay in sync with whatever storage backend the platform points to (R2 today).
# Must match REF_S3_BASE in setup.py.
REF_HEALTH_URL = "https://api.theminos.ai/reference/chr20/chr20.fa.fai"

# Chain probe budget. CI already wraps status.py in `timeout 90`; the probe must
# come back well inside that even when the RPC endpoint never answers.
CHAIN_PROBE_TIMEOUT = 30.0

# Key Python packages to check
REQUIRED_PACKAGES = {
    "bittensor": "bittensor",
    "bittensor_wallet": "bittensor-wallet",
    "httpx": "httpx",
    "boto3": "boto3",
    "pysam": "pysam",
    "numpy": "numpy",
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
        # Surface deprecated miner templates as a FAIL so this matches the
        # runtime block in neurons/miner.py, rather than reporting freebayes
        # as a valid miner choice.
        from templates import DEPRECATED_TEMPLATES  # local import to avoid cycle
        if template in DEPRECATED_TEMPLATES:
            return [Check(
                name="Variant caller template",
                status=Status.FAIL,
                detail=f"'{template}' is deprecated; {DEPRECATED_TEMPLATES[template]}",
            )]
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
    chromosomes = expected_chromosomes()
    files = _build_reference_files(chromosomes)
    dirs = _build_reference_dirs(chromosomes) if role == "validator" else []

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
    """Verify reference data download works via the platform redirect endpoint.

    Uses GET (the redirect endpoint doesn't accept HEAD). chr20.fa.fai is ~23
    bytes so the request completes near-instantly. urllib follows the 302
    redirect to the underlying storage (R2 today) automatically.
    """
    try:
        req = urllib.request.Request(REF_HEALTH_URL, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return Check("Reference data access", Status.PASS, "reachable", "network")
            return Check("Reference data access", Status.WARN, f"HTTP {resp.status}", "network")
    except Exception as e:
        return Check("Reference data access", Status.FAIL, _short_error(e), "network")


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


def check_bittensor_chain(timeout: float = CHAIN_PROBE_TIMEOUT) -> Check:
    def _connect():
        import bittensor as bt
        if not hasattr(bt, "subtensor"):
            bt.subtensor = bt.Subtensor
        # miner.py and validator.py both read NETWORK; keep SUBTENSOR_NETWORK
        # as a fallback so the probe reports the chain the neuron really uses.
        network = os.getenv("NETWORK") or os.getenv("SUBTENSOR_NETWORK") or "finney"
        sub = bt.subtensor(network=network)
        block = sub.block
        return f"{network} (block {block})"

    try:
        detail = _call_with_timeout(_connect, timeout)
        return Check("Bittensor chain", Status.PASS, detail, "network")
    except TimeoutError:
        return Check("Bittensor chain", Status.WARN, f"connection timeout ({timeout:g}s)", "network")
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

def _call_with_timeout(fn: Callable, timeout: float) -> Any:
    """Run fn on a daemon thread; raise TimeoutError once `timeout` elapses.

    Not a ThreadPoolExecutor: its context manager shuts down with wait=True, so
    the TimeoutError could not reach the caller until the worker returned, and
    even shutdown(wait=False) leaves the pool's non-daemon worker to be joined
    by the interpreter's atexit hook — so against an unresponsive
    SUBTENSOR_NETWORK the command would wait indefinitely. A daemon thread is
    abandoned instead: the probe is reported as a timeout on schedule and the
    process can still exit.
    """
    import threading

    box = {}

    def _run():
        try:
            box["value"] = fn()
        except BaseException as e:  # re-raised on the caller's thread below
            box["error"] = e

    thread = threading.Thread(target=_run, name="minos-status-probe", daemon=True)
    thread.start()
    thread.join(timeout)

    if thread.is_alive():
        raise TimeoutError(f"timed out after {timeout:g}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


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
# Role detection
# ---------------------------------------------------------------------------

def _env_file_role(base_dir: Path) -> Optional[str]:
    """Role claimed by the .env files on disk, or None if they don't claim one.

    Two signals, both written by setup.py: a role-specific .env.miner /
    .env.validator (the names load_dotenv() reads at import), and the header
    setup.py stamps on the generated .env ("# Minos Validator Configuration").
    The header matters because the wizard writes a plain .env for both roles,
    so without it a real validator has no positive evidence at all.
    """
    for role in ("miner", "validator"):
        if (base_dir / f".env.{role}").exists() and not (base_dir / f".env.{_other(role)}").exists():
            return role

    env_path = base_dir / ".env"
    if env_path.exists():
        try:
            head = env_path.read_text(errors="replace").splitlines()[:5]
        except OSError:
            return None
        for line in head:
            lowered = line.strip().lower()
            if lowered.startswith("#") and "configuration" in lowered:
                if "validator" in lowered:
                    return "validator"
                if "miner" in lowered:
                    return "miner"
    return None


def _other(role: str) -> str:
    return "validator" if role == "miner" else "miner"


def detect_role(explicit_role: Optional[str] = None, base_dir: Optional[Path] = None):
    """Resolve (role, template) for this node.

    An unset MINER_TEMPLATE is not "no miner": templates.get_template_name()
    resolves it to DEFAULT_TEMPLATE, so a default gatk miner runs fine without
    it. Treating that as a validator would check the node against validator
    Docker images (hap.py, DeepVariant, freebayes) and 22 .sdf directories it
    does not own, and skip the gatk.conf parse, which is the check that
    matters for a miner.

    Validator intent is therefore only ever taken from an explicit signal: the
    --role flag, or an .env that says so. MINER_TEMPLATE outranks the files
    because setup.py writes it for miners only. Anything else is a miner on the
    resolved default template — the diagnosis whose checks a node with no
    evidence either way can actually satisfy.
    """
    base = base_dir or BASE_DIR
    template = os.getenv("MINER_TEMPLATE", "").lower().strip() or None

    if explicit_role:
        role = explicit_role
    elif template is None and _env_file_role(base) == "validator":
        role = "validator"
    else:
        role = "miner"

    if role == "miner" and template is None:
        try:
            from templates import DEFAULT_TEMPLATE
            template = DEFAULT_TEMPLATE
        except ImportError:
            template = "gatk"

    return role, template


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

    role, template = detect_role(args.role)

    report = run_checks(role, template)

    if args.json:
        print(render_json(report))
    else:
        render_terminal(report)

    return 0 if report.all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
