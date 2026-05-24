#!/usr/bin/env python3
"""
Minos Subnet Interactive Setup Wizard.

Guides first-time miners and validators through system checks,
wallet configuration, Docker image pulls, reference data downloads,
environment setup, and process management.

Usage:
    python setup.py          (after install.sh, or standalone if deps are installed)
    python setup.py --update-data-only  (non-interactive data + image refresh)
    ./install.sh             (recommended: bootstraps deps then launches this)
"""

import sys
import os
import re
import json
import shutil
import shlex
import platform
import subprocess
import tarfile
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple, Callable
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError


# ── Bootstrap: ensure rich + questionary are available ────────────────────────

def _bootstrap_deps():
    """Install rich and questionary if missing."""
    missing = []
    try:
        import rich  # noqa: F401
    except ImportError:
        missing.append("rich>=13.0.0")
    try:
        import questionary  # noqa: F401
    except ImportError:
        missing.append("questionary>=2.0.0")

    if missing:
        print(f"\nSetup wizard requires: {', '.join(missing)}")
        if "--update-data-only" in sys.argv:
            print("Installing automatically for --update-data-only...")
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", *missing, "--quiet"]
            )
        else:
            answer = input("Install now? [Y/n] ").strip().lower()
            if answer in ("", "y", "yes"):
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", *missing, "--quiet"]
                )
            else:
                print("Cannot proceed without dependencies. Exiting.")
                sys.exit(1)

_bootstrap_deps()

# Now safe to import
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.align import Align
from rich.rule import Rule
import questionary
from questionary import Style as QStyle


# ── Constants ─────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.resolve()
NETUID = 107
NETWORK = "finney"
PLATFORM_URL = "https://api.theminos.ai"

# Valid characters for wallet/hotkey names
WALLET_NAME_REGEX = re.compile(r'^[a-zA-Z0-9_-]+$')

CUSTOM_STYLE = QStyle([
    ("qmark", "fg:cyan bold"),
    ("question", "bold"),
    ("answer", "fg:green bold"),
    ("pointer", "fg:cyan bold"),
    ("highlighted", "fg:cyan bold"),
    ("selected", "fg:green"),
])

# Docker images per role/template
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
    # freebayes deprecated 2026-05-09 16:00 UTC. Image stays in
    # VALIDATOR_DOCKER_IMAGES so validators can score any in-flight
    # pre-cutover freebayes submissions; it will be removed in a
    # follow-up release once those rounds have settled.
}

VALIDATOR_DOCKER_IMAGES = [
    "genonet/hap-py@sha256:03acabe84bbfba35f5a7234129d524c563f5657e1f21150a2ea2797f8e6d05f2",
    "broadinstitute/gatk:4.5.0.0",
    "google/deepvariant:1.5.0",
    "staphb/freebayes:1.3.7",
    "quay.io/biocontainers/bcftools:1.20--h8b25389_0",
    "quay.io/biocontainers/samtools:1.20--h50ea8bc_0",
]

# Supported chromosomes
SUPPORTED_CHROMOSOMES = [f"chr{i}" for i in range(1, 23)]  # chr1-chr22
# Permanent indirected URL — platform 302-redirects to the actual storage
# backend (R2 today, anything tomorrow). Lets us swap providers without
# requiring miner/validator setup-script updates.
REF_S3_BASE = "https://api.theminos.ai/reference"

# Reference data files per role — multi-chromosome (chr1-chr22)
# Estimated sizes in MB for user feedback
def _build_miner_data_files():
    files = []
    for chrom in SUPPORTED_CHROMOSOMES:
        files.extend([
            {
                "name": f"GRCh38 {chrom} Reference",
                "local": f"datasets/reference/{chrom}/{chrom}.fa",
                "url": f"{REF_S3_BASE}/{chrom}/{chrom}.fa",
                "size_mb": 60,
            },
            {
                "name": f"{chrom} Reference Index",
                "local": f"datasets/reference/{chrom}/{chrom}.fa.fai",
                "url": f"{REF_S3_BASE}/{chrom}/{chrom}.fa.fai",
                "size_mb": 1,
            },
            {
                "name": f"{chrom} Reference Dictionary",
                "local": f"datasets/reference/{chrom}/{chrom}.dict",
                "url": f"{REF_S3_BASE}/{chrom}/{chrom}.dict",
                "size_mb": 1,
            },
        ])
    return files

MINER_DATA_FILES = _build_miner_data_files()

# Files that make up an RTG SDF directory (vcfeval template). Match the
# layout produced by `rtg format` and stored unpacked on R2/AWS.
#
# seqdata0 is the only large file (~24MB for chr20, ~250MB for chr1).
# Everything else is tiny indexes/markers (<1MB each).
#
# format.log is intentionally excluded: it's a non-essential log of the
# `rtg format` command (RTG/vcfeval works fine without it), and CloudFront
# WAFs commonly block .log extensions by default. Dockerfile tolerates its
# absence too (`|| true` on its wget).
_SDF_FILES = [
    "done", "mainIndex",
    "nameIndex0", "namedata0", "namepointer0",
    "progress", "seqdata0", "seqpointer0",
    "sequenceIndex0", "summary.txt",
]


def _build_validator_data_files():
    """Validators need reference + SDF for all chromosomes.
    Truth VCFs are served per-round from the platform API — not stored locally.

    SDF directories are stored UNPACKED on R2 (no .tar.gz exists). We download
    each file individually under datasets/reference/{chr}/{chr}.sdf/.
    """
    files = list(MINER_DATA_FILES)
    for chrom in SUPPORTED_CHROMOSOMES:
        for sdf_file in _SDF_FILES:
            files.append({
                "name": f"{chrom} SDF {sdf_file}",
                "local": f"datasets/reference/{chrom}/{chrom}.sdf/{sdf_file}",
                "url": f"{REF_S3_BASE}/{chrom}/{chrom}.sdf/{sdf_file}",
                # seqdata0 dominates — ~24MB chr20, ~250MB chr1. Others <1MB.
                "size_mb": 250 if sdf_file == "seqdata0" else 1,
            })
    return files

VALIDATOR_DATA_FILES = _build_validator_data_files()


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class StepResult:
    success: bool = True
    skipped: bool = False


@dataclass
class WizardState:
    role: str = ""
    os_name: str = ""
    arch: str = ""
    python_version: str = ""
    docker_version: str = ""
    disk_free_gb: float = 0.0
    ram_gb: float = 0.0
    in_venv: bool = False
    wallet_name: str = "default"
    wallet_hotkey: str = "default"
    wallet_registered: bool = False
    template: str = "gatk"
    docker_images_needed: List[str] = field(default_factory=list)
    docker_images_pulled: List[str] = field(default_factory=list)
    reference_data_ready: bool = False
    process_management: str = "none"
    # Demo-miner branch: pipeline test only, no wallet/registration needed.
    # When True the wizard skips Wallet Configuration entirely and writes a
    # .env without WALLET_* (the miner uses an ephemeral keypair via --demo).
    is_demo: bool = False


# ── Wizard ────────────────────────────────────────────────────────────────────

class SetupWizard:

    def __init__(self):
        self.console = Console()
        self.state = WizardState()
        self.steps: List[Tuple[str, Callable]] = [
            ("Welcome & Role Selection", self.step_welcome),
            ("System Verification", self.step_system_check),
            ("Python Dependencies", self.step_python_deps),
            ("Wallet Configuration", self.step_wallet),
            ("Template Selection", self.step_template),
            ("Docker Images", self.step_docker_images),
            ("Reference Data", self.step_reference_data),
            ("Environment Configuration", self.step_env_config),
            ("Process Management", self.step_process_management),
            ("Summary & Launch", self.step_summary),
        ]

    # ── Runner ────────────────────────────────────────────────────────────

    def run(self):
        """Run the full wizard."""
        try:
            total = len(self.steps)
            for i, (name, fn) in enumerate(self.steps):
                self.console.print()
                self.console.print(Rule(
                    f"[bold cyan]Step {i + 1} of {total} -- {name}[/]",
                    style="cyan",
                ))
                self.console.print()

                result = fn()

                if result is None:
                    self.console.print("\n  [yellow]Setup cancelled.[/]")
                    sys.exit(0)

                if result.skipped:
                    self.console.print("  [dim]Skipped.[/]")
                    continue

                if not result.success:
                    self.console.print("\n  [red]Step failed. Cannot continue.[/]")
                    sys.exit(1)

        except KeyboardInterrupt:
            self.console.print("\n\n  [yellow]Setup interrupted. Run again to resume.[/]")
            sys.exit(0)

        self.console.print()
        self.console.print(Panel(
            "[bold green]Setup complete![/]\n\nYour Minos node is ready.",
            border_style="green",
            padding=(1, 2),
        ))

    # ── Step 1: Welcome ───────────────────────────────────────────────────

    def step_welcome(self) -> StepResult:
        banner = Text.from_markup(
            "[bold cyan]"
            "      A---T\n"
            "     { \\ / }\n"
            "      \\ X /     [bold white]MINOS GENOMICS SUBNET[/bold white]\n"
            "      / X \\     [dim]Setup Wizard[/dim]\n"
            "     { / \\ }\n"
            "      G---C"
            "[/bold cyan]"
        )
        self.console.print(Panel(
            Align.center(banner),
            border_style="cyan",
            padding=(1, 2),
        ))

        self.console.print(
            "  [dim]This wizard guides you through setting up a Minos miner or validator.[/]"
        )
        self.console.print(
            "  [dim]Safe to run multiple times -- already-completed steps are detected and skipped.[/]"
        )
        self.console.print()

        role = questionary.select(
            "What are you setting up?",
            choices=[
                questionary.Choice("Miner     -- Run variant callers, earn TAO", value="miner"),
                questionary.Choice("Validator  -- Score miners, set weights", value="validator"),
                questionary.Choice("Demo miner -- Test the pipeline only (no wallet, no TAO)", value="demo-miner"),
            ],
            style=CUSTOM_STYLE,
        ).ask()

        if role is None:
            return None

        if role == "demo-miner":
            # Demo branch reuses all the miner steps (template, Docker images,
            # reference data, env, process mgmt) but skips wallet setup and
            # writes a MINER_DEMO=true env so start-miner.sh routes to the
            # platform's /v2/demo/* sandbox.
            self.state.role = "miner"
            self.state.is_demo = True
            self.state.wallet_name = "demo"
            self.state.wallet_hotkey = "demo"
            self.console.print("  Selected: [bold green]Miner (demo mode)[/]")
            self.console.print(
                "  [dim]No wallet will be created; the miner uses an ephemeral "
                "keypair against /v2/demo/* — no TAO, no scoring.[/]"
            )
        else:
            self.state.role = role
            self.console.print(f"  Selected: [bold green]{role.capitalize()}[/]")
        return StepResult()

    # ── Step 2: System check ──────────────────────────────────────────────

    def step_system_check(self) -> StepResult:
        table = Table(show_header=True, header_style="bold cyan", padding=(0, 1))
        table.add_column("Check", style="white", width=25)
        table.add_column("Status", justify="center", width=8)
        table.add_column("Details", style="dim", max_width=45)

        blockers = []

        # OS
        os_name = platform.system()
        arch = platform.machine()
        self.state.os_name = os_name
        self.state.arch = arch
        table.add_row("Operating System", "[green]PASS[/]", f"{os_name} {platform.release()} ({arch})")

        # Python
        py = sys.version_info
        py_str = f"{py.major}.{py.minor}.{py.micro}"
        self.state.python_version = py_str
        if py >= (3, 10):
            table.add_row("Python", "[green]PASS[/]", f"{py_str}")
        else:
            table.add_row("Python", "[red]FAIL[/]", f"{py_str} (need 3.10+)")
            blockers.append("Python 3.10+ is required")

        # Docker
        docker_ok, docker_detail = self._check_docker()
        self.state.docker_version = docker_detail
        if docker_ok:
            table.add_row("Docker", "[green]PASS[/]", docker_detail)
        elif "permission denied" in docker_detail:
            # Docker is installed and daemon is running, but the docker group is not
            # active in this shell yet (happens right after install without logout).
            # Try to re-exec this wizard under sg docker so it works seamlessly.
            if shutil.which("sg"):
                self.console.print(
                    "  [yellow]Docker group not active — re-launching with docker group...[/]"
                )
                cmd_str = sys.executable + " " + shlex.join(sys.argv)
                os.execvp("sg", ["sg", "docker", "-c", cmd_str])
                # execvp replaces this process; we only reach here if exec failed
            table.add_row("Docker", "[yellow]WARN[/]", docker_detail)
            blockers.append(
                "Docker group not active in this shell.\n"
                "    Fix: run [bold]newgrp docker[/bold] then re-run: python setup.py\n"
                "    Or log out and back in, then re-run setup."
            )
        else:
            table.add_row("Docker", "[red]FAIL[/]", docker_detail)
            blockers.append("Docker is required but not available")

        # Disk
        min_disk = 60 if self.state.role == "miner" else 100
        disk_free = shutil.disk_usage(BASE_DIR).free / (1024 ** 3)
        self.state.disk_free_gb = disk_free
        if disk_free >= min_disk:
            table.add_row("Disk Space", "[green]PASS[/]", f"{disk_free:.0f} GB free ({min_disk}+ needed)")
        else:
            table.add_row("Disk Space", "[yellow]WARN[/]", f"{disk_free:.0f} GB free ({min_disk}+ recommended)")

        # RAM
        ram_gb = self._get_ram_gb()
        self.state.ram_gb = ram_gb
        min_ram = 8 if self.state.role == "miner" else 32
        if ram_gb > 0:
            if ram_gb >= min_ram:
                table.add_row("RAM", "[green]PASS[/]", f"{ram_gb:.0f} GB ({min_ram}+ needed)")
            else:
                table.add_row("RAM", "[yellow]WARN[/]", f"{ram_gb:.0f} GB ({min_ram}+ recommended)")
        else:
            table.add_row("RAM", "[dim]???[/]", "Could not detect")

        # Architecture
        if arch in ("x86_64", "AMD64"):
            table.add_row("Architecture", "[green]PASS[/]", arch)
        else:
            table.add_row("Architecture", "[yellow]WARN[/]", f"{arch} (Rosetta may be needed)")

        self.console.print(table)

        if blockers:
            for b in blockers:
                self.console.print(f"  [red]BLOCKER: {b}[/]")
            return StepResult(success=False)

        return StepResult()

    # ── Step 3: Python deps ───────────────────────────────────────────────

    def step_python_deps(self) -> StepResult:
        self.state.in_venv = sys.prefix != sys.base_prefix
        if not self.state.in_venv:
            self.console.print("  [yellow]Not in a virtual environment.[/]")
            self.console.print("  [dim]Recommended: python3 -m venv .venv && source .venv/bin/activate[/]")
            proceed = questionary.confirm(
                "Continue without a virtual environment?",
                default=True, style=CUSTOM_STYLE,
            ).ask()
            if proceed is None or not proceed:
                return StepResult(success=False)
        else:
            self.console.print(f"  [green]Virtual environment active:[/] {sys.prefix}")

        # Check key packages
        packages = {
            "bittensor": "bittensor",
            "torch": "torch",
            "numpy": "numpy",
            "pydantic": "pydantic",
            "dotenv": "python-dotenv",
            "boto3": "boto3",
            "pysam": "pysam",
            "tqdm": "tqdm",
            "rich": "rich",
            "questionary": "questionary",
        }

        missing = []
        for import_name in packages:
            try:
                __import__(import_name)
            except ImportError:
                missing.append(packages[import_name])

        if not missing:
            self.console.print(f"  [green]All {len(packages)} required packages installed.[/]")
            return StepResult()

        self.console.print(f"  [yellow]Missing: {', '.join(missing)}[/]")

        # Warn if critical packages are missing
        critical_missing = [p for p in missing if p in ("bittensor", "torch", "pysam")]
        if critical_missing:
            self.console.print(f"  [yellow]Critical packages missing: {', '.join(critical_missing)}[/]")
            self.console.print(f"  [dim]Your node will not be able to launch without these.[/]")

        install = questionary.confirm(
            "Install requirements now? (pip install -r requirements.txt)",
            default=True, style=CUSTOM_STYLE,
        ).ask()

        if install is None or not install:
            if critical_missing:
                self.console.print("  [yellow]Skipping. Node will NOT work without critical packages.[/]")
            else:
                self.console.print("  [yellow]Skipping. Install dependencies manually before running.[/]")
            return StepResult(skipped=True)

        self.console.print("  [dim]This includes PyTorch (~2 GB) and may take 5-15 minutes...[/]")
        with self.console.status("[bold cyan]Installing Python dependencies...[/]", spinner="dots"):
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(BASE_DIR / "requirements.txt")],
                capture_output=True, text=True, timeout=600,
            )

        if result.returncode == 0:
            self.console.print("  [green]Dependencies installed successfully.[/]")
            return StepResult()
        else:
            self.console.print(f"  [red]pip install failed:[/] {result.stderr[:300]}")
            return StepResult(success=False)

    # ── Step 4: Wallet ────────────────────────────────────────────────────

    def step_wallet(self) -> StepResult:
        if self.state.is_demo:
            self.console.print(
                "  [dim]Demo mode: skipping wallet setup — the miner will use an "
                "ephemeral keypair generated per process.[/]"
            )
            return StepResult(skipped=True)
        # List existing wallets
        wallets = self._list_wallets()
        if wallets:
            table = Table(show_header=True, header_style="bold cyan", padding=(0, 1))
            table.add_column("Wallet", style="white")
            table.add_column("Hotkeys", style="dim")
            for name, hotkeys in wallets.items():
                table.add_row(name, ", ".join(hotkeys) if hotkeys else "[dim]none[/]")
            self.console.print(table)
        else:
            self.console.print("  [dim]No existing wallets found in ~/.bittensor/wallets/[/]")

        self.console.print()

        # Wallet name
        default_name = "miner" if self.state.role == "miner" else "validator"
        wallet_name = self._ask_validated_name("Wallet name:", default_name)
        if wallet_name is None:
            return None
        self.state.wallet_name = wallet_name

        # Hotkey
        wallet_hotkey = self._ask_validated_name("Hotkey name:", "default")
        if wallet_hotkey is None:
            return None
        self.state.wallet_hotkey = wallet_hotkey

        # Check wallet exists
        wallet_path = Path.home() / ".bittensor" / "wallets" / wallet_name
        hotkey_path = wallet_path / "hotkeys" / wallet_hotkey

        if hotkey_path.exists():
            self.console.print(f"  [green]Wallet found:[/] {wallet_name}/{wallet_hotkey}")
        else:
            self.console.print(f"  [yellow]Wallet '{wallet_name}/{wallet_hotkey}' not found.[/]")
            create = questionary.confirm(
                f"Create wallet '{wallet_name}' with hotkey '{wallet_hotkey}'?",
                default=True, style=CUSTOM_STYLE,
            ).ask()
            if create:
                self._create_wallet(wallet_name, wallet_hotkey)
            else:
                self.console.print(
                    f"  [dim]Create later: btcli wallet create "
                    f"--wallet-name {wallet_name} --wallet-hotkey {wallet_hotkey}[/]"
                )

        # Check registration (only if wallet exists)
        hotkey_path = wallet_path / "hotkeys" / wallet_hotkey
        if not hotkey_path.exists():
            self.console.print("  [dim]Skipping registration check (no wallet).[/]")
            return StepResult()

        self.console.print(f"  Checking subnet {NETUID} registration...")
        self.console.print(f"  [dim]Connecting to Bittensor network (may take 30-60 seconds)...[/]")
        registered = self._check_registration(wallet_name, wallet_hotkey)
        self.state.wallet_registered = registered

        if registered:
            self.console.print(f"  [green]Registered on subnet {NETUID}.[/]")
        else:
            self.console.print()
            self.console.print(f"  [bold yellow]Not registered on subnet {NETUID}.[/]")
            self.console.print(
                f"  [yellow]Your {self.state.role} will not work until this hotkey is registered.[/]"
            )
            self.console.print(
                f"  [yellow]Registration recycles TAO — ensure your wallet has sufficient balance.[/]"
            )
            self.console.print()
            register_cmd = (
                f"btcli subnets register --netuid {NETUID} "
                f"--wallet-name {wallet_name} --wallet-hotkey {wallet_hotkey}"
            )
            self.console.print(f"  Register command:")
            self.console.print(f"  [bold cyan]{register_cmd}[/]")
            self.console.print()

            register_now = questionary.confirm(
                "Would you like to register now?",
                default=False, style=CUSTOM_STYLE,
            ).ask()

            if register_now:
                self.console.print(f"  [dim]Running registration (this may take a minute)...[/]")
                # Try new-style flags first, fall back to old-style
                result = subprocess.run(
                    ["btcli", "subnets", "register",
                     "--netuid", str(NETUID),
                     "--wallet-name", wallet_name,
                     "--wallet-hotkey", wallet_hotkey],
                )
                if result.returncode != 0:
                    self.console.print(
                        "  [yellow]Registration may have failed. "
                        "Try running the command above manually.[/]"
                    )
                else:
                    self.console.print(f"  [green]Registration submitted successfully.[/]")
                    self.state.wallet_registered = True
            else:
                self.console.print(
                    f"  [dim]You can register later before launching your {self.state.role}.[/]"
                )

        return StepResult()

    # ── Step 5: Template selection (miner only) ───────────────────────────

    def step_template(self) -> StepResult:
        if self.state.role != "miner":
            self.console.print("  [dim]Template selection is for miners only.[/]")
            self.console.print(
                "  [dim]Validators re-run all miner tool configs (GATK, DeepVariant, BCFtools) and score with hap.py.[/]"
            )
            self.console.print(
                "  [dim]All required Docker images will be configured in the next step.[/]"
            )
            return StepResult(skipped=True)

        templates = [
            questionary.Choice(
                "GATK HaplotypeCaller    (recommended, most reliable)",
                value="gatk",
            ),
            questionary.Choice(
                "DeepVariant             (GPU-accelerated, Google AI)",
                value="deepvariant",
            ),
            questionary.Choice(
                "BCFtools                (minimal, fastest)",
                value="bcftools",
            ),
        ]

        template = questionary.select(
            "Select your variant calling template:",
            choices=templates,
            default="gatk",
            style=CUSTOM_STYLE,
        ).ask()

        if template is None:
            return None

        self.state.template = template

        info = {
            "gatk": ("broadinstitute/gatk:4.5.0.0", "~4 GB", "10-20 min/window"),
            "deepvariant": ("google/deepvariant:1.5.0", "~3 GB", "5-15 min (GPU) / 15-30 min (CPU)"),
            "bcftools": ("quay.io/biocontainers/bcftools:1.20--h8b25389_0", "~200 MB", "2-5 min/window"),
        }
        image, size, timing = info[template]
        self.console.print(f"  Primary image: [cyan]{image}[/] ({size})")
        self.console.print(f"  Typical runtime: {timing}")
        self.console.print(f"  [dim]All 3 tool images will be pulled so you can switch templates later.[/]")

        return StepResult()

    # ── Step 6: Docker images ─────────────────────────────────────────────

    def step_docker_images(self, force_pull: bool = False, assume_yes: bool = False) -> StepResult:
        if self.state.role == "miner":
            seen: set = set()
            needed = []
            if self.state.is_demo:
                # Demo only runs one template per process — no reason to
                # pull the other tools' images (saves ~5 GB of bandwidth
                # for a pipeline smoke test).
                template_images = MINER_DOCKER_IMAGES.get(self.state.template, [])
                for img in template_images:
                    if img not in seen:
                        seen.add(img)
                        needed.append(img)
                self.console.print(
                    f"  [dim]Demo mode: pulling images for {self.state.template} only.[/]"
                )
            else:
                # Pull all template images so the miner can switch tools without re-running setup.
                for imgs in MINER_DOCKER_IMAGES.values():
                    for img in imgs:
                        if img not in seen:
                            seen.add(img)
                            needed.append(img)
        else:
            needed = VALIDATOR_DOCKER_IMAGES

        self.state.docker_images_needed = list(needed)

        if shutil.which("docker") is None:
            self.console.print("  [yellow]Docker is not installed; skipping image pull.[/]")
            return StepResult(skipped=True)

        existing = []
        missing = []
        to_pull = []
        for image in needed:
            if self._docker_image_exists(image):
                existing.append(image)
                if force_pull:
                    to_pull.append(image)
            else:
                missing.append(image)
                to_pull.append(image)

        # Display table
        table = Table(show_header=True, header_style="bold cyan", padding=(0, 1))
        table.add_column("Image", style="white", max_width=55)
        table.add_column("Status", justify="center", width=10)
        for img in existing:
            status = "[cyan]REFRESH[/]" if force_pull else "[green]PULLED[/]"
            table.add_row(img, status)
        for img in missing:
            table.add_row(img, "[yellow]MISSING[/]")
        self.console.print(table)

        self.state.docker_images_pulled = list(existing)

        if not to_pull:
            self.console.print("  [green]All required Docker images available.[/]")
            return StepResult()

        if force_pull:
            self.console.print("  [dim]Refreshing configured Docker image references.[/]")
        else:
            self.console.print(f"  [dim]Total download may be several GB. This can take 5-15 minutes.[/]")

        if assume_yes:
            pull = True
        else:
            action = "Refresh" if force_pull else "Pull"
            target = "configured image(s)" if force_pull else "missing image(s)"
            pull = questionary.confirm(
                f"{action} {len(to_pull)} {target}?",
                default=True, style=CUSTOM_STYLE,
            ).ask()

        if pull is None or not pull:
            self.console.print("  [yellow]Skipping. Pull images manually before running.[/]")
            return StepResult(skipped=True)

        failed = []
        for img in to_pull:
            self.console.print(f"\n  Pulling [cyan]{img}[/] ...")
            result = subprocess.run(
                ["docker", "pull", img],
                timeout=1800,
            )
            if result.returncode == 0:
                self.console.print(f"  [green]Pulled {img}[/]")
                if img not in self.state.docker_images_pulled:
                    self.state.docker_images_pulled.append(img)
            else:
                self.console.print(f"  [red]Failed to pull {img}[/]")
                failed.append(img)

        if failed:
            self.console.print(f"\n  [yellow]{len(failed)} image(s) failed to pull:[/]")
            for img in failed:
                self.console.print(f"    [yellow]- {img}[/]")
            self.console.print(f"  [dim]Pull manually with: docker pull <image>[/]")
            self.console.print(f"  [yellow]Your node will NOT work without these images.[/]")

        return StepResult()

    # ── Step 7: Reference data ────────────────────────────────────────────

    def _migrate_legacy_reference_data(self):
        """Migrate old flat chr20 reference structure to new per-chromosome directories.

        Old: datasets/reference/chr20.fa
        New: datasets/reference/chr20/chr20.fa
        """
        old_ref = BASE_DIR / "datasets" / "reference" / "chr20.fa"
        new_ref_dir = BASE_DIR / "datasets" / "reference" / "chr20"

        if old_ref.exists() and not (new_ref_dir / "chr20.fa").exists():
            self.console.print("  [yellow]Migrating reference data to new multi-chromosome structure...[/]")
            new_ref_dir.mkdir(parents=True, exist_ok=True)

            import glob
            import shutil

            # Move chr20.* files (fa, fai, dict, amb, ann, bwt, pac, sa)
            for f in glob.glob(str(BASE_DIR / "datasets" / "reference" / "chr20.*")):
                fname = Path(f).name
                # Skip if it's actually the new directory
                if Path(f).is_dir():
                    continue
                shutil.move(f, new_ref_dir / fname)

            # Move chr20.sdf directory
            old_sdf = BASE_DIR / "datasets" / "reference" / "chr20.sdf"
            new_sdf = new_ref_dir / "chr20.sdf"
            if old_sdf.exists() and old_sdf.is_dir() and not new_sdf.exists():
                shutil.move(str(old_sdf), str(new_sdf))

            self.console.print("  [green]Migration complete — chr20 data moved to datasets/reference/chr20/[/]")

    def _try_archive_download(self, target_dir: Path) -> bool:
        """Stream-download and extract the bundled tar.zst reference archive.

        Returns True if extraction completed (curl + tar both exit 0). The
        caller is responsible for verifying every required file is on disk
        afterwards — a stale or partial archive can extract cleanly but be
        missing chromosomes or SDF files. Falls back to per-file download
        anywhere it returns False.

        Used for validators (where archive saves ~10 minutes); miners stick
        with per-file because their dataset is small and the archive contains
        validator-only SDF files they don't need.
        """
        if shutil.which("zstd") is None:
            self.console.print("  [dim]zstd not installed — using per-file download[/]")
            return False

        archive_url = f"{REF_S3_BASE}/reference-grch38-v1.tar.zst"
        self.console.print(f"  [cyan]Trying single-archive install (~1.8 GB compressed)...[/]")
        target_dir.mkdir(parents=True, exist_ok=True)

        curl = tar = None
        try:
            curl = subprocess.Popen(
                ["curl", "-fsSL", archive_url],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            tar = subprocess.Popen(
                ["tar", "--use-compress-program=zstd -d", "-xf", "-", "-C", str(target_dir)],
                stdin=curl.stdout, stderr=subprocess.PIPE,
            )
            curl.stdout.close()
            _, tar_err = tar.communicate(timeout=900)
            curl_rc = curl.wait(timeout=10)
        except Exception as e:
            self.console.print(f"  [yellow]Archive download failed ({type(e).__name__}); falling back to per-file[/]")
            for p in (curl, tar):
                if p and p.poll() is None:
                    p.kill()
            return False

        if curl_rc != 0 or tar.returncode != 0:
            err = (tar_err or b"").decode("utf-8", errors="replace")[-200:]
            self.console.print(f"  [yellow]Archive extract failed (curl={curl_rc}, tar={tar.returncode}); falling back to per-file[/]")
            if err.strip():
                self.console.print(f"  [dim]{err.strip()}[/]")
            return False

        self.console.print("  [green]Archive extracted[/]")
        return True

    def step_reference_data(self, assume_yes: bool = False) -> StepResult:
        # Migrate old flat chr20 structure if detected
        self._migrate_legacy_reference_data()

        files = MINER_DATA_FILES if self.state.role == "miner" else VALIDATOR_DATA_FILES

        # Demo mode only ever scores against the static chr20 demo BAM
        # (see DEMO_REGION in platform .env.example). Downloading 22
        # chromosomes' worth of reference data for a pipeline smoke test
        # would burn ~3.9 GB of bandwidth and disk for no reason.
        if self.state.is_demo:
            files = [f for f in files if "/chr20/" in f["local"]]
            self.console.print(
                "  [dim]Demo mode: restricting reference data to chr20 "
                f"({len(files)} files).[/]"
            )

        existing = []
        to_download = []
        for f in files:
            local_path = BASE_DIR / f["local"]
            # For SDF: check if directory exists with contents
            if f.get("extract"):
                if local_path.exists() and local_path.is_dir() and any(local_path.iterdir()):
                    existing.append(f)
                else:
                    to_download.append(f)
            elif local_path.exists() and local_path.stat().st_size > 0:
                existing.append(f)
            else:
                to_download.append(f)

        # Display table
        table = Table(show_header=True, header_style="bold cyan", padding=(0, 1))
        table.add_column("File", style="white", max_width=35)
        table.add_column("Size", justify="right", width=8)
        table.add_column("Status", justify="center", width=10)
        for f in existing:
            table.add_row(f["name"], f"~{f.get('size_mb', '?')} MB", "[green]EXISTS[/]")
        for f in to_download:
            table.add_row(f["name"], f"~{f.get('size_mb', '?')} MB", "[yellow]MISSING[/]")
        self.console.print(table)

        if not to_download:
            self.console.print("  [green]All reference data available.[/]")
            self.state.reference_data_ready = True
            return StepResult()

        total_size_mb = sum(f.get("size_mb", 0) for f in to_download)
        self.console.print(f"  [dim]Total download: ~{total_size_mb} MB[/]")

        if assume_yes:
            self.console.print("  [dim]Downloading missing reference files without prompting.[/]")
            download = True
        else:
            download = questionary.confirm(
                f"Download {len(to_download)} missing file(s)?",
                default=True, style=CUSTOM_STYLE,
            ).ask()

        if download is None or not download:
            self.console.print("  [yellow]Skipping. Download files before running.[/]")
            return StepResult(skipped=True)

        # Validators with most files missing: try the bundled archive first
        # (single ~1.8 GB download vs ~280 individual files). Miners skip this
        # because their dataset is small and the archive includes SDF files
        # they don't need.
        if self.state.role == "validator" and len(to_download) >= 50:
            if self._try_archive_download(BASE_DIR / "datasets" / "reference"):
                # Re-verify every required file. A stale or partial archive
                # could extract cleanly but be missing chromosomes / SDF files,
                # so we re-check disk state and only consider files still
                # missing as the new to_download list. The remainder falls
                # through to per-file download below.
                still_missing = []
                for f in files:
                    local_path = BASE_DIR / f["local"]
                    if f.get("extract"):
                        if not (local_path.exists() and local_path.is_dir() and any(local_path.iterdir())):
                            still_missing.append(f)
                    elif not (local_path.exists() and local_path.stat().st_size > 0):
                        still_missing.append(f)

                if not still_missing:
                    self.console.print("  [green]Archive install complete; all reference files verified[/]")
                    self.state.reference_data_ready = True
                    return StepResult()

                self.console.print(
                    f"  [yellow]Archive extracted but {len(still_missing)} file(s) still missing; "
                    f"completing via per-file download[/]"
                )
                to_download = still_missing
            # Archive failed or incomplete — fall through to per-file download

        # Try to use existing download_file, fall back to urllib
        download_fn = self._get_download_function()

        all_ok = True
        for f in to_download:
            local_path = BASE_DIR / f["local"]
            local_path.parent.mkdir(parents=True, exist_ok=True)
            self.console.print(f"\n  Downloading [cyan]{f['name']}[/] (~{f.get('size_mb', '?')} MB)...")

            if f.get("extract"):
                # Download tarball to temp file, then extract
                ok = self._download_and_extract(download_fn, f["url"], local_path)
            else:
                # Download to temp file first, rename on success (prevents partial files)
                tmp_path = local_path.with_suffix(local_path.suffix + ".tmp")
                ok = download_fn(f["url"], tmp_path)
                if ok and tmp_path.exists():
                    tmp_path.rename(local_path)

            if ok and (local_path.exists()):
                if local_path.is_file():
                    size_bytes = local_path.stat().st_size
                    size_str = f"{size_bytes / (1024*1024):.1f} MB" if size_bytes >= 1024*1024 else f"{size_bytes // 1024} KB"
                    self.console.print(f"  [green]Downloaded[/] ({size_str})")
                else:
                    self.console.print(f"  [green]Downloaded and extracted[/]")
            else:
                self.console.print(f"  [red]Failed to download {f['name']}[/]")
                # Clean up partial temp file
                tmp_path = local_path.with_suffix(local_path.suffix + ".tmp")
                if tmp_path.exists():
                    tmp_path.unlink()
                all_ok = False

        self.state.reference_data_ready = all_ok
        return StepResult()

    # ── Step 8: Environment configuration ─────────────────────────────────

    def step_env_config(self) -> StepResult:
        env = {
            "NETUID": str(NETUID),
            "NETWORK": NETWORK,
            "PLATFORM_URL": PLATFORM_URL,
            "PLATFORM_TIMEOUT": "60",
            "STORAGE_PRIMARY_BACKEND": "hippius",
        }
        if not self.state.is_demo:
            env["WALLET_NAME"] = self.state.wallet_name
            env["WALLET_HOTKEY"] = self.state.wallet_hotkey

        if self.state.role == "miner":
            env["MINER_TEMPLATE"] = self.state.template

        if self.state.is_demo:
            # start-miner.sh checks MINER_DEMO; the miner --demo flag also
            # reads this env var. Both paths end up routing to /v2/demo/*.
            env["MINER_DEMO"] = "true"

        # Build .env content
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        role_label = "Miner (demo)" if self.state.is_demo else self.state.role.capitalize()
        lines = [
            f"# Minos {role_label} Configuration",
            f"# Generated by setup wizard on {ts}",
            "",
        ]

        sections = [
            ("Bittensor", ["NETUID", "NETWORK", "WALLET_NAME", "WALLET_HOTKEY"]),
            ("Miner", ["MINER_TEMPLATE", "MINER_DEMO"]),
            ("Platform", ["PLATFORM_URL", "PLATFORM_TIMEOUT"]),
            ("Storage", ["STORAGE_PRIMARY_BACKEND"]),
        ]

        for section_name, keys in sections:
            section_vars = [(k, env[k]) for k in keys if k in env]
            if section_vars:
                lines.append(f"# {section_name}")
                for k, v in section_vars:
                    lines.append(f"{k}={v}")
                lines.append("")

        env_content = "\n".join(lines)

        # Preview
        self.console.print(Panel(
            env_content.rstrip(),
            title=".env",
            border_style="cyan",
            padding=(0, 1),
        ))

        # Check existing
        env_path = BASE_DIR / ".env"
        if env_path.exists():
            self.console.print("  [yellow]An existing .env file was found.[/]")
            overwrite = questionary.confirm(
                "Overwrite existing .env? (backup will be created)",
                default=False, style=CUSTOM_STYLE,
            ).ask()
            if overwrite is None or not overwrite:
                self.console.print("  [dim]Keeping existing .env.[/]")
                return StepResult(skipped=True)
            ts_backup = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = env_path.parent / f".env.backup.{ts_backup}"
            shutil.copy2(env_path, backup)
            self.console.print(f"  [dim]Backed up to {backup.name}[/]")

        env_path.write_text(env_content)
        self.console.print(f"  [green].env written.[/]")
        return StepResult()

    # ── Step 9: Process management ────────────────────────────────────────

    def step_process_management(self) -> StepResult:
        role = self.state.role

        choice = questionary.select(
            "How would you like to run your node?",
            choices=[
                questionary.Choice(
                    "Generate PM2 config  (recommended — auto-restart, logs, monitoring)",
                    value="pm2",
                ),
                questionary.Choice(
                    "Generate systemd service  (Linux, auto-restart)",
                    value="systemd",
                ),
                questionary.Choice(
                    f"Run directly  (python -m neurons.{role})",
                    value="direct",
                ),
                questionary.Choice(
                    "Skip  (I will configure this myself)",
                    value="none",
                ),
            ],
            style=CUSTOM_STYLE,
        ).ask()

        if choice is None:
            return None

        self.state.process_management = choice

        if choice == "direct":
            self.console.print(f"  Run with: [bold cyan]python -m neurons.{role}[/]")
            return StepResult()

        if choice == "systemd":
            return self._generate_systemd()

        if choice == "pm2":
            return self._generate_pm2()

        return StepResult()

    # ── Step 10: Summary & launch ─────────────────────────────────────────

    def step_summary(self) -> StepResult:
        role = self.state.role

        table = Table(show_header=False, border_style="cyan", padding=(0, 2))
        table.add_column("Setting", style="bold white", width=22)
        table.add_column("Value", style="green")

        role_display = "Miner (demo mode)" if self.state.is_demo else role.capitalize()
        table.add_row("Role", role_display)
        table.add_row("System", f"{self.state.os_name} ({self.state.arch})")
        table.add_row("Python", self.state.python_version)
        table.add_row("Docker", self.state.docker_version)
        table.add_row("Disk Free", f"{self.state.disk_free_gb:.0f} GB")
        if self.state.ram_gb > 0:
            table.add_row("RAM", f"{self.state.ram_gb:.0f} GB")
        table.add_row("", "")
        if self.state.is_demo:
            table.add_row("Wallet", "[dim]none (ephemeral keypair)[/]")
            table.add_row(f"Registered (SN{NETUID})", "[dim]N/A (demo)[/]")
        else:
            table.add_row("Wallet", f"{self.state.wallet_name} / {self.state.wallet_hotkey}")
            reg_text = "[green]Yes[/]" if self.state.wallet_registered else "[yellow]No[/]"
            table.add_row(f"Registered (SN{NETUID})", reg_text)
        if role == "miner":
            table.add_row("Template", self.state.template.upper())
        n_pulled = len(self.state.docker_images_pulled)
        n_needed = len(self.state.docker_images_needed)
        img_text = f"{n_pulled}/{n_needed} pulled"
        if n_pulled < n_needed:
            img_text = f"[yellow]{img_text}[/]"
        table.add_row("Docker Images", img_text)
        data_text = "[green]Ready[/]" if self.state.reference_data_ready else "[yellow]Incomplete[/]"
        table.add_row("Reference Data", data_text)
        table.add_row("", "")
        table.add_row("Network", NETWORK)
        table.add_row("Subnet", str(NETUID))
        table.add_row("Platform", PLATFORM_URL)

        self.console.print(table)

        # Warnings
        warnings = []
        if self.state.is_demo:
            self.console.print()
            self.console.print(
                "  [bold green]Demo mode:[/] launch with [bold]bash start-miner.sh --demo[/] "
                "(or [bold]MINER_DEMO=true[/] in .env)."
            )
            self.console.print(
                "  [dim]When ready to mine for real, register a hotkey on subnet "
                f"{NETUID} and re-run setup as 'Miner'.[/]"
            )
        elif not self.state.wallet_registered:
            warnings.append(
                f"btcli subnets register --netuid {NETUID} "
                f"--wallet-name {self.state.wallet_name} --wallet-hotkey {self.state.wallet_hotkey}"
            )
            self.console.print()
            self.console.print(
                "  [dim]Not registered yet — to test the pipeline first without "
                "registering, re-run setup and choose 'Demo miner'.[/]"
            )
        if n_pulled < n_needed:
            warnings.append("Some Docker images are not pulled yet")
        if not self.state.reference_data_ready:
            warnings.append("Reference data download incomplete")

        if warnings:
            self.console.print()
            self.console.print("  [yellow]Before launching:[/]")
            for w in warnings:
                self.console.print(f"    [yellow]- {w}[/]")

        # Show process management commands if configured
        pm = self.state.process_management
        if pm == "systemd":
            service_name = f"minos-{role}"
            self.console.print()
            self.console.print(f"  [bold]Manage your {role} (systemd):[/]")
            self.console.print(f"  [dim]  sudo systemctl start {service_name}     # start[/]")
            self.console.print(f"  [dim]  sudo systemctl stop {service_name}      # stop[/]")
            self.console.print(f"  [dim]  sudo systemctl restart {service_name}   # restart[/]")
            self.console.print(f"  [dim]  sudo systemctl disable {service_name}   # disable auto-start[/]")
            self.console.print(f"  [dim]  sudo systemctl enable {service_name}    # enable auto-start[/]")
            self.console.print(f"  [dim]  sudo systemctl status {service_name}    # check status[/]")
            self.console.print(f"  [dim]  journalctl -u {service_name} -f         # view logs[/]")
        elif pm == "pm2":
            service_name = f"minos-{role}"
            eco_file = f"ecosystem.{role}.config.js"
            self.console.print()
            self.console.print(f"  [bold]Manage your {role} (PM2):[/]")
            self.console.print(f"  [dim]  bash pm2-{role}.sh                    # start / restart (recommended)[/]")
            self.console.print(f"  [dim]  pm2 start {eco_file} # start via config[/]")
            self.console.print(f"  [dim]  pm2 stop {service_name}                 # stop[/]")
            self.console.print(f"  [dim]  pm2 restart {service_name}              # restart[/]")
            self.console.print(f"  [dim]  pm2 delete {service_name}               # remove from PM2[/]")
            self.console.print(f"  [dim]  pm2 status                              # check status[/]")
            self.console.print(f"  [dim]  pm2 logs {service_name}                 # view logs[/]")
            self.console.print(f"  [dim]  pm2 save                                # persist across reboots[/]")

        # PM2: always register the process so pm2 status / pm2 start works
        if pm == "pm2":
            run_cmd = f"bash pm2-{role}.sh"
            service_name = f"minos-{role}"
            eco_file = str(BASE_DIR / f"ecosystem.{role}.config.js")
            os.chdir(BASE_DIR)

            # Register with PM2 (start then immediately stop if user doesn't want to launch)
            self.console.print(f"\n  [dim]Registering {service_name} with PM2...[/]")
            subprocess.call(["pm2", "start", eco_file], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.call(["pm2", "save"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            launch = questionary.confirm(
                "Launch now?",
                default=True, style=CUSTOM_STYLE,
            ).ask()

            if launch:
                self.console.print(f"\n  [bold cyan]Starting Minos {role} via PM2...[/]\n")
                subprocess.call(["pm2", "restart", service_name, "--update-env"])
                self.console.print()
                self.console.print(f"  [green]{service_name} is running.[/]")
                self.console.print(f"  [dim]  pm2 logs {service_name}   # view logs[/]")
                self.console.print(f"  [dim]  pm2 status               # check status[/]")
            else:
                subprocess.call(["pm2", "stop", service_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.call(["pm2", "save"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.console.print(f"\n  [green]{service_name} registered with PM2 (stopped).[/]")
                self.console.print(f"  [dim]  pm2 start {service_name}   # start when ready[/]")
                self.console.print(f"  [dim]  pm2 status                # check status[/]")
        else:
            run_cmd = f"python -m neurons.{role}"
            self.console.print()
            self.console.print(f"  Launch command: [bold cyan]{run_cmd}[/]")
            self.console.print()

            launch = questionary.confirm(
                "Launch now?",
                default=False, style=CUSTOM_STYLE,
            ).ask()

            if launch:
                self.console.print(f"\n  [bold cyan]Starting Minos {role}...[/]\n")
                os.chdir(BASE_DIR)
                if pm == "systemd":
                    service_name = f"minos-{role}"
                    sys.exit(subprocess.call(["sudo", "systemctl", "start", service_name]))
                else:
                    # Direct launch
                    needs_sg = (
                        shutil.which("sg")
                        and "docker" not in os.getgroups().__str__()
                        and subprocess.run(["docker", "info"], capture_output=True).returncode != 0
                    )
                    if needs_sg:
                        cmd_str = f"{sys.executable} -m neurons.{role}"
                        sys.exit(subprocess.call(["sg", "docker", "-c", cmd_str]))
                    else:
                        sys.exit(subprocess.call([sys.executable, "-m", f"neurons.{role}"]))

            self.console.print(f"\n  To start later:")
            self.console.print(f"  [dim]  cd {BASE_DIR}[/]")
            self.console.print(f"  [dim]  source .venv/bin/activate[/]")
            self.console.print(f"  [dim]  {run_cmd}[/]")
            self.console.print()
            self.console.print(f"  [dim]  If Docker gives a permission error, run: newgrp docker[/]")

        return StepResult()

    # ── Helpers ────────────────────────────────────────────────────────────

    def _ask_validated_name(self, prompt: str, default: str) -> Optional[str]:
        """Ask for a wallet/hotkey name and validate it contains only safe characters."""
        while True:
            name = questionary.text(
                prompt,
                default=default,
                style=CUSTOM_STYLE,
            ).ask()
            if name is None:
                return None
            if WALLET_NAME_REGEX.match(name):
                return name
            self.console.print(
                "  [red]Invalid name. Use only letters, numbers, hyphens, and underscores.[/]"
            )

    def _check_docker(self) -> Tuple[bool, str]:
        try:
            ver = subprocess.run(
                ["docker", "--version"], capture_output=True, text=True, timeout=5,
            )
            if ver.returncode != 0:
                return False, "docker command failed"
            version = ver.stdout.strip()

            info = subprocess.run(
                ["docker", "info"], capture_output=True, text=True, timeout=10,
            )
            if info.returncode != 0:
                stderr = (info.stderr or "").lower()
                if "permission denied" in stderr or "got permission denied" in stderr:
                    return False, "permission denied (not in docker group)"
                return False, "Docker installed but daemon not running"

            # Extract version number
            match = re.search(r'(\d+\.\d+\.\d+)', version)
            return True, f"v{match.group(1)}" if match else version
        except FileNotFoundError:
            return False, "Docker not installed"
        except subprocess.TimeoutExpired:
            return False, "Docker command timed out (daemon not running?)"

    def _get_ram_gb(self) -> float:
        if platform.system() == "Darwin":
            try:
                r = subprocess.run(
                    ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True,
                )
                return int(r.stdout.strip()) / (1024 ** 3)
            except Exception:
                pass
        else:
            try:
                return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 ** 3)
            except (ValueError, OSError, AttributeError):
                pass
        return 0.0

    def _create_wallet(self, wallet_name: str, wallet_hotkey: str):
        """Create a wallet, with or without password protection."""
        use_password = questionary.confirm(
            "Set a password for this wallet? (recommended)",
            default=True, style=CUSTOM_STYLE,
        ).ask()

        if use_password:
            # Interactive btcli — lets user set their own password
            self.console.print("  [cyan]Creating wallet (follow the prompts)...[/]\n")
            result = subprocess.run(
                ["btcli", "wallet", "create",
                 "--wallet-name", wallet_name,
                 "--wallet-hotkey", wallet_hotkey],
                timeout=120,
            )
            if result.returncode != 0:
                # Fall back to old-style flags (btcli v7)
                result = subprocess.run(
                    ["btcli", "wallet", "create",
                     "--wallet.name", wallet_name,
                     "--wallet.hotkey", wallet_hotkey],
                    timeout=120,
                )
            self.console.print()
            if result.returncode != 0:
                self.console.print("  [yellow]Wallet creation may have failed. Check manually.[/]")
        else:
            # Programmatic — no password, no prompts
            try:
                import bittensor as bt
                if not hasattr(bt, "wallet"):
                    bt.wallet = bt.Wallet
                self.console.print("  [cyan]Creating wallet...[/]")
                wallet = bt.wallet(name=wallet_name, hotkey=wallet_hotkey)
                wallet.create_if_non_existent(
                    coldkey_use_password=False, hotkey_use_password=False,
                )
                self.console.print(f"  [green]Wallet created:[/] {wallet_name}/{wallet_hotkey}")
                self.console.print(f"  [dim]Hotkey SS58: {wallet.hotkey.ss58_address}[/]")
            except Exception as e:
                self.console.print(f"  [red]Wallet creation failed: {e}[/]")
                self.console.print(
                    f"  [dim]Create manually: btcli wallet create "
                    f"--wallet-name {wallet_name} --wallet-hotkey {wallet_hotkey}[/]"
                )

    def _list_wallets(self) -> Dict[str, List[str]]:
        wallets_dir = Path.home() / ".bittensor" / "wallets"
        if not wallets_dir.exists():
            return {}
        result = {}
        for wallet_dir in sorted(wallets_dir.iterdir()):
            if wallet_dir.is_dir():
                hotkeys_dir = wallet_dir / "hotkeys"
                hotkeys = []
                if hotkeys_dir.exists():
                    hotkeys = [f.name for f in sorted(hotkeys_dir.iterdir()) if f.is_file()]
                result[wallet_dir.name] = hotkeys
        return result

    def _check_registration(self, wallet_name: str, wallet_hotkey: str) -> bool:
        """Check subnet registration with a timeout to avoid hanging on slow nodes."""
        def _do_check():
            import bittensor as bt
            if not hasattr(bt, "subtensor"):
                bt.subtensor = bt.Subtensor
            if not hasattr(bt, "wallet"):
                bt.wallet = bt.Wallet
            wallet = bt.wallet(name=wallet_name, hotkey=wallet_hotkey)
            subtensor = bt.subtensor(network=NETWORK)
            metagraph = subtensor.metagraph(NETUID)
            return wallet.hotkey.ss58_address in metagraph.hotkeys

        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_do_check)
                return future.result(timeout=60)
        except FuturesTimeoutError:
            self.console.print("  [dim]Registration check timed out (network may be slow).[/]")
            return False
        except ImportError:
            self.console.print("  [dim]bittensor not installed -- cannot check registration.[/]")
            return False
        except Exception as e:
            self.console.print(f"  [dim]Could not verify registration: {type(e).__name__}[/]")
            return False

    def _docker_image_exists(self, image: str) -> bool:
        try:
            r = subprocess.run(
                ["docker", "image", "inspect", image],
                capture_output=True, timeout=10,
            )
            return r.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _get_download_function(self):
        """Return a download function with one transparent retry on failure.

        Tries file_utils first, falls back to urllib. Wraps the chosen
        downloader so a single transient failure (network blip, slow first
        byte) doesn't fail the whole setup step.
        """
        try:
            sys.path.insert(0, str(BASE_DIR))
            from utils.file_utils import download_file

            def _download(url: str, path: Path) -> bool:
                result = download_file(url, path, use_cache=False, show_progress=True)
                return result is not None and result.exists()

            base_fn = _download
        except ImportError:
            # Fallback: urllib with progress reporting
            def _download_urllib(url: str, path: Path) -> bool:
                import urllib.request

                try:
                    self.console.print(f"  [dim]{url}[/]")

                    def _reporthook(block_num, block_size, total_size):
                        if total_size > 0:
                            downloaded = block_num * block_size
                            pct = min(100, downloaded * 100 // total_size)
                            mb_done = downloaded / (1024 * 1024)
                            mb_total = total_size / (1024 * 1024)
                            print(f"\r  {mb_done:.1f}/{mb_total:.1f} MB ({pct}%)", end="", flush=True)

                    urllib.request.urlretrieve(url, str(path), reporthook=_reporthook)
                    print()  # newline after progress
                    return path.exists()
                except Exception as e:
                    print()  # newline after progress
                    self.console.print(f"  [red]Download error: {e}[/]")
                    return False

            base_fn = _download_urllib

        def _with_retry(url: str, path: Path) -> bool:
            if base_fn(url, path):
                return True
            # Clean up any partial file so the retry starts fresh
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
            self.console.print("  [yellow]Download failed, retrying once in 3s...[/]")
            time.sleep(3)
            return base_fn(url, path)

        return _with_retry

    def _download_and_extract(self, download_fn, url: str, target_dir: Path) -> bool:
        """Download a tarball and extract it to target_dir (for RTG SDF directories)."""
        tmp_tarball = target_dir.with_suffix(".tar.gz.tmp")
        try:
            ok = download_fn(url, tmp_tarball)
            if not ok or not tmp_tarball.exists():
                return False

            # Extract tarball
            self.console.print(f"  [dim]Extracting to {target_dir.name}/...[/]")
            target_dir.parent.mkdir(parents=True, exist_ok=True)

            with tarfile.open(str(tmp_tarball), "r:gz") as tar:
                # Security: check for path traversal in tarball
                for member in tar.getmembers():
                    member_path = Path(member.name)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        self.console.print(f"  [red]Tarball contains unsafe path: {member.name}[/]")
                        return False
                tar.extractall(path=str(target_dir.parent))

            # Clean up tarball
            tmp_tarball.unlink()

            # Verify extraction
            if target_dir.exists() and target_dir.is_dir():
                return True
            else:
                self.console.print(f"  [yellow]Extracted but {target_dir.name}/ not found. Check archive structure.[/]")
                return False

        except tarfile.TarError as e:
            self.console.print(f"  [red]Extraction failed: {e}[/]")
            return False
        except Exception as e:
            self.console.print(f"  [red]Download/extract error: {e}[/]")
            return False
        finally:
            if tmp_tarball.exists():
                tmp_tarball.unlink()

    def _generate_systemd(self) -> StepResult:
        if platform.system() != "Linux":
            self.console.print("  [yellow]systemd is only available on Linux.[/]")
            return StepResult(skipped=True)

        role = self.state.role
        service_name = f"minos-{role}"
        python_path = sys.executable
        working_dir = str(BASE_DIR)
        user = os.getenv("USER") or os.getenv("LOGNAME") or "root"

        service = f"""[Unit]
Description=Minos {role.capitalize()} - Bittensor Subnet {NETUID}
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User={user}
WorkingDirectory={working_dir}
ExecStart={python_path} -m neurons.{role}
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal
EnvironmentFile={working_dir}/.env
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
"""

        self.console.print(Panel(
            service.strip(),
            title=f"{service_name}.service",
            border_style="cyan",
            padding=(0, 1),
        ))

        write = questionary.confirm(
            f"Write {service_name}.service?",
            default=True, style=CUSTOM_STYLE,
        ).ask()

        if write is None or not write:
            return StepResult(skipped=True)

        service_path = BASE_DIR / f"{service_name}.service"
        service_path.write_text(service)
        self.console.print(f"  [green]Written to {service_path}[/]")
        self.console.print()
        self.console.print("  To install and start:")
        self.console.print(f"  [dim]  sudo cp {service_path} /etc/systemd/system/[/]")
        self.console.print(f"  [dim]  sudo systemctl daemon-reload[/]")
        self.console.print(f"  [dim]  sudo systemctl enable {service_name}[/]")
        self.console.print(f"  [dim]  sudo systemctl start {service_name}[/]")
        self.console.print(f"  [dim]  journalctl -u {service_name} -f   # view logs[/]")

        return StepResult()

    def _generate_pm2(self) -> StepResult:
        role = self.state.role
        service_name = f"minos-{role}"
        start_sh = f"start-{role}.sh"
        config_name = f"ecosystem.{role}.config.js"

        config = f"""module.exports = {{
  apps: [{{
    name: "{service_name}",
    script: "./{start_sh}",
    interpreter: "bash",
    cwd: "{BASE_DIR}",
    autorestart: true,
    max_restarts: 10,
    restart_delay: 30000,
    kill_timeout: 15000,
    log_date_format: "YYYY-MM-DD HH:mm:ss Z",
    env: {{ PYTHONUNBUFFERED: "1" }},
  }}]
}};
"""

        self.console.print(Panel(
            config.strip(),
            title=config_name,
            border_style="cyan",
            padding=(0, 1),
        ))

        write = questionary.confirm(
            f"Write {config_name}?",
            default=True, style=CUSTOM_STYLE,
        ).ask()

        if write is None or not write:
            return StepResult(skipped=True)

        config_path = BASE_DIR / config_name
        config_path.write_text(config)
        self.console.print(f"  [green]Written to {config_path}[/]")
        self.console.print()
        self.console.print("  To start:")
        self.console.print(f"  [dim]  bash pm2-{role}.sh[/]")
        self.console.print(f"  [dim]  pm2 start {config_name}[/]")
        self.console.print(f"  [dim]  pm2 logs {service_name}   # view logs[/]")
        self.console.print(f"  [dim]  pm2 save                  # persist across reboots[/]")

        return StepResult()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    wizard = SetupWizard()

    if "--update-data-only" in sys.argv:
        # Non-interactive: refresh configured Docker images and reference data.
        # Restore is_demo + selected template from .env so demo installs don't
        # silently bloat back to all-chromosomes + all-templates on update.
        env_path = BASE_DIR / ".env"
        env_vars: Dict[str, str] = {}
        if env_path.exists():
            with open(env_path) as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#") or "=" not in stripped:
                        continue
                    key, _, value = stripped.partition("=")
                    env_vars[key.strip()] = value.strip()

        if "MINER_TEMPLATE" in env_vars:
            wizard.state.role = "miner"
            template = env_vars["MINER_TEMPLATE"].lower()
            if template in ("gatk", "deepvariant", "bcftools"):
                wizard.state.template = template
        elif env_vars:
            wizard.state.role = "validator"
        else:
            wizard.state.role = "validator"  # download all (superset)

        # Truthy MINER_DEMO → narrow reference data to chr20 + docker images
        # to the chosen template only (mirrors the wizard's Demo-miner branch).
        if env_vars.get("MINER_DEMO", "").strip().lower() in ("1", "true", "yes", "on"):
            wizard.state.is_demo = True

        scope = "demo " if wizard.state.is_demo else ""
        print(f"\n  Updating {scope}{wizard.state.role} Docker images and reference data...\n")
        wizard.step_docker_images(force_pull=True, assume_yes=True)
        print("\n  Checking reference data...\n")
        wizard.step_reference_data(assume_yes=True)
        print("\n  Update-data-only finished.\n")
    else:
        wizard.run()
