"""Minos Miner - Round-based variant calling submission."""

import sys
import os
import gzip
import json
import re
import secrets
import shutil
import traceback
import threading
from pathlib import Path

# Add parent directory to path so we can import base and utils
sys.path.insert(0, str(Path(__file__).parent.parent))

# --- Take --config back from bittensor, before importing it -------------------
# bittensor builds its logging config at IMPORT time
# (bittensor/utils/btlogging/__init__.py: LoggingMachine(LoggingMachine.config())),
# and that reads --config straight out of sys.argv expecting a YAML file. Our
# side modes use --config for the variant-caller .conf, so
# `--practice --config configs/gatk.conf` dies inside `import bittensor` with a
# YAML parse error naming neither this program nor the real problem — before a
# single line of our code runs.
#
# So the value is lifted out of argv here, ahead of the import, and handed to
# the side-mode parser instead. Only done when a side mode is actually present:
# the full miner has no --config of its own, and bittensor's meaning of the flag
# must keep working there.
_SIDE_MODE_FLAGS = ("--score", "--practice", "--demo")
_SIDE_MODE_CONFIG = None
if any(flag in sys.argv[1:] for flag in _SIDE_MODE_FLAGS):
    _argv = sys.argv[1:]
    _kept = []
    _i = 0
    while _i < len(_argv):
        _arg = _argv[_i]
        if _arg == "--config" and _i + 1 < len(_argv):
            _SIDE_MODE_CONFIG = _argv[_i + 1]
            _i += 2
            continue
        if _arg.startswith("--config="):
            _SIDE_MODE_CONFIG = _arg.split("=", 1)[1]
            _i += 1
            continue
        _kept.append(_arg)
        _i += 1
    sys.argv = [sys.argv[0]] + _kept

import time
from typing import Any, Dict, Optional
import asyncio
import bittensor as bt
from bittensor_wallet import Keypair
import argparse
import subprocess
from dotenv import load_dotenv

# Importing utils.bt_compat restores the lowercase bt.subtensor/wallet/config
# aliases on the shared bt module, and supplies the SDK-version wrappers below.
from utils import bt_compat
from utils import config_commit
from utils import submission_payment
from utils import scoring_version as scoring_version_util

from base import GENOMICS_CONFIG, MINER_CONFIG, is_docker_available, require_docker, BASE_DIR
from utils.file_utils import download_file_verified, download_file_with_fallback
from utils.platform_client import PaymentRequiredError, MinerPlatformClient, PlatformConfig, PlatformClientError
from utils.config_loader import extract_tool_options, get_tool_version

# Template system for pluggable variant callers
from templates import (
    DEPRECATED_TEMPLATES,
    get_template_path,
    load_template,
)
from templates.tool_params import validate_round_id
from neurons import CHROMOSOME_PATTERN, safe_chrom

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)
load_dotenv()

# Round timing constants
MIN_SUBMISSION_TIME_SECONDS = 600
# Minimum window the submission path requires before it spends anything: the
# payment extrinsic and the chain commitment each wait on a block.
MIN_SPEND_TIME_SECONDS = 30
POLL_INTERVAL_SECONDS = 30
MIN_VCF_SIZE_BYTES = 100


class Miner:
    """Minos miner - round-based variant calling."""

    def __init__(self, config=None):
        self.config = config or self.get_config()

        bt.logging.info("Setting up miner...")
        bt.logging.set_trace(self.config.logging.trace)
        bt.logging.set_debug(self.config.logging.debug)

        try:
            require_docker()
        except RuntimeError as e:
            bt.logging.error(str(e))
            sys.exit(1)

        self.demo = bool(getattr(self.config, "demo", False))

        if self.demo:
            # Ephemeral keypair: no real wallet needed, no on-chain check.
            # Random URI keeps the ss58 unique per process so demo-side
            # rate-limit / log keys don't collide across local restarts.
            self.wallet = None
            self.keypair = Keypair.create_from_uri(f"//demo-{secrets.token_hex(4)}")
            self.subtensor = None
            self.metagraph = None
            self.is_registered = False
            self.my_subnet_uid = None
            self.hotkey_ss58 = self.keypair.ss58_address
            bt.logging.info(
                f"DEMO MODE — using ephemeral keypair {self.hotkey_ss58[:16]}... "
                "(no chain connection, no wallet required)"
            )
        else:
            self.wallet = bt_compat.make_wallet(config=self.config)
            self.keypair = self.wallet.hotkey
            self.hotkey_ss58 = self.keypair.ss58_address
            bt.logging.info(f"Wallet loaded: {self.hotkey_ss58}")

            bt.logging.info(f"Connecting to network: {self.config.subtensor.network}")
            self.subtensor = bt_compat.make_subtensor(config=self.config)

            bt.logging.info(f"Loading metagraph for netuid: {self.config.netuid}")
            self.metagraph = bt_compat.get_metagraph(self.subtensor, self.config.netuid)
            bt.logging.info(f"Metagraph loaded: {len(self.metagraph.hotkeys)} neurons")

            self.is_registered = self.hotkey_ss58 in self.metagraph.hotkeys
            if self.is_registered:
                self.my_subnet_uid = self.metagraph.hotkeys.index(self.hotkey_ss58)
                bt.logging.info(f"Miner registered with UID: {self.my_subnet_uid}")
            else:
                self.my_subnet_uid = None
                bt.logging.warning(
                    "Miner not registered on subnet 107. "
                    "Register with: btcli subnets register --netuid 107 --wallet.name miner --wallet.hotkey default"
                )
                bt.logging.info(
                    "To test your pipeline without registering, restart with --demo "
                    "(a walletless one-shot that scores your config on a fixed sample)."
                )

        self.setup_variant_caller()
        self.setup_platform_client()

        # Round tracking
        self.submitted_rounds: set = set()
        # Submissions accepted per round, for the free-allowance check.
        self.round_submit_counts: dict = {}
        # round_id -> submissions this miner's OWNER has used, as reported by the
        # platform. Authoritative over round_submit_counts, which only ever sees
        # this hotkey's own submissions.
        self._hotkey_submissions_used: dict = {}
        # round_id -> the price the platform quoted for this hotkey's next paid
        # submission. Escalates per coldkey within a round, so it is read rather
        # than derived from the base fee.
        self._quoted_fee_tao: dict = {}
        # Insertion order for submitted_rounds — a set has none.
        self._submit_order: list = []
        # round_id -> monotonic() instant the submission window closes. Monotonic
        # so a clock step during a long calling run cannot move the deadline.
        self._round_deadlines: dict = {}
        self._payment_ledger = submission_payment.PaymentLedger()

        bt.logging.info(f"Miner ready - template: {self.variant_caller}, docker: {is_docker_available()}")

    def _register_with_retry(self, max_retries: int = 3) -> bool:
        """Register on subnet with retry for 'Transaction Already Imported' errors."""
        for attempt in range(max_retries):
            try:
                # bt_compat.register normalizes the per-SDK return shapes to a bool.
                return bt_compat.register(
                    self.subtensor,
                    wallet=self.wallet,
                    netuid=self.config.netuid,
                    wait_for_finalization=True,
                    wait_for_inclusion=True,
                )
            except Exception as e:
                error_str = str(e)
                if "Already Imported" in error_str and attempt < max_retries - 1:
                    bt.logging.warning(f"Transaction already in mempool, waiting 30s... (attempt {attempt + 1}/{max_retries})")
                    time.sleep(30)
                else:
                    bt.logging.error(f"Registration failed: {e}")
                    return False
        return False

    def setup_platform_client(self):
        """Setup platform client for round-based API."""
        platform_url = os.getenv("PLATFORM_URL", "")
        if not platform_url:
            bt.logging.error("PLATFORM_URL not set - required for platform mode")
            sys.exit(1)

        try:
            config = PlatformConfig(
                base_url=platform_url,
                timeout=float(os.getenv("PLATFORM_TIMEOUT", "60"))
            )
            self.platform_client = MinerPlatformClient(
                keypair=self.keypair,
                config=config,
                demo=self.demo,
            )
            mode_label = "demo" if self.demo else "live"
            bt.logging.info(f"Platform client initialized ({mode_label}): {platform_url}")
        except Exception as e:
            bt.logging.error(f"Failed to initialize platform client: {e}")
            sys.exit(1)

    def setup_variant_caller(self):
        """Setup variant caller from MINER_TEMPLATE env var."""
        self.variant_caller = os.getenv("MINER_TEMPLATE", "").lower() or MINER_CONFIG.get("default_caller", "gatk")

        # Refuse to run with a deprecated template. The runner is still
        # registered (validators need it for in-flight pre-cutover rounds),
        # so a stray MINER_TEMPLATE value would otherwise resolve, run, and
        # waste compute on every round before the platform's HTTP 400.
        if self.variant_caller in DEPRECATED_TEMPLATES:
            bt.logging.error(
                f"MINER_TEMPLATE='{self.variant_caller}' is deprecated. "
                f"{DEPRECATED_TEMPLATES[self.variant_caller]}"
            )
            sys.exit(1)

        # Validate template exists
        try:
            get_template_path(self.variant_caller)
        except (ValueError, FileNotFoundError):
            bt.logging.error(f"Invalid template '{self.variant_caller}'. Available: gatk, deepvariant, bcftools")
            sys.exit(1)

    @staticmethod
    def get_config():
        """Get configuration from argparse and environment."""
        parser = argparse.ArgumentParser(description="Minos Miner", allow_abbrev=False)

        parser.add_argument("--netuid", type=int, default=int(os.getenv("NETUID", 107)), help="Subnet UID")
        parser.add_argument(
            "--variant_caller",
            type=str,
            choices=["gatk", "deepvariant", "bcftools"],
            default=MINER_CONFIG.get("default_caller", "gatk"),
            help="Variant calling template",
        )
        parser.add_argument(
            "--demo",
            action="store_true",
            default=False,
            help=(
                "One-shot onboarding self-score: no chain, no wallet. Downloads "
                "a single fixed, fully-answered sample (BAM + truth), runs your "
                "config, and prints the exact score a validator would compute — "
                "so a new operator can verify their pipeline and see its score "
                "without registering. Use --practice to score against any sample."
            ),
        )
        # --- Offline self-scoring (no chain, no platform) ---
        # Runs the miner's config against a local BAM + truth and prints the
        # EXACT combined_final a validator would compute. Requires only Docker
        # (GATK + hap.py) and a local reference. Handled in main() before the
        # full Miner (wallet/chain/platform) is ever constructed.
        parser.add_argument(
            "--score",
            action="store_true",
            default=False,
            help=(
                "Offline self-score: run your config against a local BAM + "
                "truth VCF and print the exact validator score. No chain, no "
                "platform, no wallet. Use with --bam/--truth/--region "
                "(+optional --mutations/--config/--reference)."
            ),
        )
        parser.add_argument(
            "--practice",
            action="store_true",
            default=False,
            help=(
                "Interactive practice mode: pick a fully-answered chr18/chr19/chr20/chr21/chr22 "
                "sample from the platform, download its BAM + truth + "
                "mutations (reused if already downloaded), run your config, and "
                "self-score with the validator scorer. No chain. Optionally "
                "pass --config to score immediately, or --sample_id to skip "
                "the picker."
            ),
        )
        parser.add_argument(
            "--sample_id", type=str, default=None,
            help="[--practice] Skip the interactive picker and use this sample id directly.",
        )
        parser.add_argument(
            "--resubmit",
            action="store_true",
            help=(
                "Submit again to a round this hotkey has already submitted to. "
                "Past the free allowance this COSTS TAO, so it is never taken "
                "automatically: the loop stops at the first submission and this "
                "flag is the only way past it. Requires "
                "MINER_PAY_FOR_RESUBMISSIONS to be set as well."
            ),
        )
        parser.add_argument("--bam", type=str, default=None, help="[--score] Path to input BAM.")
        parser.add_argument("--truth", type=str, default=None, help="[--score] Path to truth VCF (.vcf.gz).")
        parser.add_argument(
            "--mutations", type=str, default=None,
            help="[--score] Path to mutations-only VCF (.vcf.gz). Strongly recommended — the validator scores with it.",
        )
        parser.add_argument("--region", type=str, default=None, help="[--score] Region, e.g. chr20:45000000-50000000.")
        parser.add_argument(
            "--config", type=str, default=None,
            help="[--score] Config file to score. A GATK .conf (same format as configs/gatk.conf) or a JSON "
                 "of gatk_options. Defaults to configs/<tool>.conf.",
        )
        parser.add_argument(
            "--reference", type=str, default=None,
            help="[--score] Reference FASTA. Defaults to datasets/reference/<chrom>/<chrom>.fa.",
        )
        parser.add_argument(
            "--confident_bed", type=str, default=None,
            help="[--score] Optional confident-regions BED (matches the validator's confident_bed).",
        )
        parser.add_argument(
            "--reference_sdf", type=str, default=None,
            help="[--score] Optional RTG SDF for the reference (speeds up hap.py; built on the fly if omitted).",
        )

        bt_compat.add_subtensor_args(parser)
        bt.logging.add_args(parser)
        bt_compat.add_wallet_args(parser)

        config = bt_compat.make_config(parser)

        # Env overrides
        if os.getenv("NETWORK"):
            config.subtensor.network = os.getenv("NETWORK")
        if os.getenv("NETUID"):
            config.netuid = int(os.getenv("NETUID"))
        if os.getenv("WALLET_NAME"):
            config.wallet.name = os.getenv("WALLET_NAME")
        if os.getenv("WALLET_HOTKEY"):
            config.wallet.hotkey = os.getenv("WALLET_HOTKEY")
        # MINER_DEMO=1/true/yes opts in via env (PM2 / systemd convenience)
        if os.getenv("MINER_DEMO", "").strip().lower() in ("1", "true", "yes", "on"):
            config.demo = True

        return config

    async def execute_template(self, bam_path: Path, region: str, config: Dict[str, Any] = None) -> tuple:
        """Execute selected template for variant calling. Returns (vcf_content, vcf_path, variant_count)."""
        output_dir = bam_path.parent
        output_vcf = output_dir / "output.vcf.gz"

        # Extract chromosome from region (e.g. "chr16:10000000-15000000" -> "chr16")
        # The region is platform-supplied and chrom becomes a path component, so
        # it must match the contig allowlist before it is interpolated below.
        chrom = safe_chrom(region)
        if chrom is None:
            raise RuntimeError(
                f"Region {region!r} does not name a supported chromosome "
                f"(chr1-22, chrX, chrY, chrM)"
            )
        ref_path = BASE_DIR / "datasets" / "reference" / chrom / f"{chrom}.fa"
        if not ref_path.exists():
            # Fallback to old flat structure for backward compatibility
            ref_path_legacy = BASE_DIR / "datasets" / "reference" / "chr20.fa"
            if chrom == "chr20" and ref_path_legacy.exists():
                ref_path = ref_path_legacy
            else:
                raise RuntimeError(f"Reference not found: {ref_path}. Ensure reference data for {chrom} is downloaded.")

        bt.logging.info(f"Running {self.variant_caller} on {bam_path.name}, region={region}")

        # Merge system config with tool-specific config
        # NOTE: memory_gb is NOT set here — templates auto-detect available memory
        # using os.sysconf(), with tool-specific fallbacks (2GB GATK, 4GB DeepVariant)
        base_config = {
            "timeout": GENOMICS_CONFIG.get("variant_calling_timeout", 1800),
            "threads": MINER_CONFIG.get("num_threads", 4),
            "ref_build": "GRCh38"
        }

        # If config provided, merge it with base config (tool-specific options override)
        if config:
            base_config.update(config)

        # Load and run template
        template = load_template(self.variant_caller)
        result = template.variant_call(
            bam_path=bam_path,
            reference_path=ref_path,
            output_vcf_path=output_vcf,
            region=region,
            config=base_config  # Use merged config including tool options
        )

        if not result.get("success"):
            raise RuntimeError(f"Template failed: {result.get('error', 'Unknown error')}")

        variant_count = result.get("variant_count", 0)
        bt.logging.info(f"Template completed: {variant_count} variants")

        # Find VCF file
        vcf_path = output_vcf if output_vcf.exists() else None
        if not vcf_path:
            for ext in [".vcf.gz", ".vcf"]:
                alt = output_dir / f"output{ext}"
                if alt.exists():
                    vcf_path = alt
                    break

        # Read VCF content
        vcf_content = ""
        if vcf_path and vcf_path.exists():
            try:
                opener = gzip.open if str(vcf_path).endswith(".gz") else open
                with opener(vcf_path, "rt") as f:
                    vcf_content = f.read()
            except Exception as e:
                bt.logging.warning(f"Could not read VCF: {e}")

        return vcf_content, vcf_path, variant_count

    async def process_round(self) -> bool:
        """Check for active round and submit config if in submission window.

        Returns:
            True if participated in a round, False otherwise
        """
        # Pre-bind so the PlatformClientError handler below can reference it
        # safely if get_round_status() itself raises (e.g. transport error
        # before round_data is even built). Without this, a server-side
        # "demo mode" 4xx in the get_round_status path triggers a NameError
        # that masks the original PlatformClientError.
        round_id: Optional[str] = None
        try:
            # Get current round status
            round_data = await self.platform_client.get_round_status()

            if not round_data.get("has_active_round"):
                return False

            round_id = round_data.get("round_id")
            status = round_data.get("status")
            region = round_data.get("region")
            if not region:
                bt.logging.error("Round has no region specified — skipping")
                return False
            time_remaining = round_data.get("time_remaining_seconds", 0)

            # Validate round_id to prevent path traversal / shell injection
            rid_check = validate_round_id(round_id or "")
            if not rid_check["valid"]:
                bt.logging.error(f"process_round: invalid round_id '{round_id}': {rid_check['error']}")
                return False

            # Skip if not in submission window
            if status != "open":
                if status == "scoring":
                    bt.logging.debug(f"Round {round_id[:8]}... is in scoring phase")
                return False

            # Skip if already submitted to this round (in-memory check)
            if round_id in self.submitted_rounds:
                bt.logging.debug(f"Already submitted to round {round_id[:8]}...")
                return False

            # Skip if platform confirms we already submitted (restart recovery)
            hotkey_used = round_data.get("hotkey_submissions_used")
            if hotkey_used is not None:
                self._hotkey_submissions_used[round_id] = int(hotkey_used)

            # The price of the NEXT paid submission, quoted for this hotkey. It
            # is NOT the advertised base fee: the fee escalates with the owning
            # coldkey's paid submissions this round, so paying the base fee for
            # anything past the first is an underpayment — and the transfer is
            # already on chain when the platform refuses it, so the TAO is gone.
            quoted_fee = round_data.get("next_submission_fee_tao")
            if quoted_fee is not None:
                try:
                    self._quoted_fee_tao[round_id] = float(quoted_fee)
                except (TypeError, ValueError):
                    pass

            if round_data.get("has_submitted", False):
                if not getattr(self.config, "resubmit", False):
                    bt.logging.info(f"Already submitted to round {round_id[:8]}... (platform confirmed)")
                    self.submitted_rounds.add(round_id)
                    return False
                # Deliberate replacement. The quote read above is the price for
                # the NEXT paid submission for this hotkey's coldkey, fetched in
                # this same response -- so it already reflects the escalation
                # from every submission counted so far. Paying an older quote
                # underpays, and the transfer is on chain before the platform
                # refuses it.
                if quoted_fee is None:
                    bt.logging.error(
                        f"Round {round_id[:8]}...: --resubmit given but the "
                        f"platform quoted no fee for the next submission. "
                        f"Refusing to guess a price."
                    )
                    self.submitted_rounds.add(round_id)
                    return False
                bt.logging.warning(
                    f"Round {round_id[:8]}...: --resubmit given; this hotkey has "
                    f"already submitted. The next submission is quoted at "
                    f"{quoted_fee} TAO and will be PAID FOR if it goes ahead."
                )

            # Check if enough time remaining (need at least 10 minutes for variant calling)
            if time_remaining < MIN_SUBMISSION_TIME_SECONDS:
                bt.logging.warning(f"Only {time_remaining}s remaining in round - skipping")
                return False

            # Record when the window closes so _submit_result can re-check it
            # without another authenticated round trip.
            try:
                self._round_deadlines[round_id] = time.monotonic() + float(time_remaining)
            except (TypeError, ValueError):
                self._round_deadlines.pop(round_id, None)
            # Bound the map: one entry per detected round, dropped after an hour.
            cutoff = time.monotonic() - 3600
            self._round_deadlines = {
                r: d for r, d in self._round_deadlines.items() if d > cutoff
            }

            bt.logging.info(f"Active round found: {round_id[:8]}..., status={status}, region={region}")
            print(f"\n{'='*60}", flush=True)
            print(f"   ROUND DETECTED", flush=True)
            print(f"   Round ID: {round_id[:16]}...", flush=True)
            print(f"   Region: {region}", flush=True)
            print(f"   Time remaining: {time_remaining // 60} min", flush=True)
            print(f"{'='*60}", flush=True)

            # Download BAM file and index
            bam_path = self._download_bam(round_data, round_id)
            if bam_path is None:
                return False

            # Get tool config BEFORE running template (so we use what we submit)
            tool_config = self._get_tool_config()

            # Run variant calling (or reuse existing results)
            output_dir = bam_path.parent
            # In demo mode, always re-run variant calling so users can test their tools.
            # Detected by either the explicit --demo flag OR the static demo round_id
            # prefix (covers legacy PLATFORM_MODE=demo deployments that don't go
            # through the /v2/demo/* namespace).
            is_demo = self.demo or round_id.startswith("2026-01-01T00:00:00")
            variant_count, elapsed = await self._run_variant_calling(bam_path, region, tool_config, output_dir, force_rerun=is_demo)

            # Submit config to platform
            return await self._submit_result(round_id, tool_config, variant_count, elapsed)

        except PlatformClientError as e:
            bt.logging.warning(f"Round error: {e}")
            if "demo mode" in str(e).lower():
                self.submitted_rounds.add(round_id)
                print(f"\n{'='*60}", flush=True)
                print(f"   DEMO COMPLETE", flush=True)
                print(f"   Variant calling finished successfully!", flush=True)
                print(f"   Your system is ready to mine on Subnet 107.", flush=True)
                print(f"", flush=True)
                print(f"   Submission is disabled because the network is in", flush=True)
                print(f"   demo mode. When the network goes live, submissions", flush=True)
                print(f"   will be automatic — no code changes needed.", flush=True)
                print(f"", flush=True)
                print(f"   Next: register your hotkey to earn TAO when live.", flush=True)
                print(f"{'='*60}", flush=True)
            return False
        except Exception as e:
            bt.logging.error(f"Error processing round: {e}")
            bt.logging.debug(traceback.format_exc())
            return False

    def _download_bam(self, round_data, round_id):
        """Download BAM file and its index, creating the index locally if needed."""
        # Download BAM from platform (with Hippius backup fallback)
        _prefer_hippius = os.getenv("STORAGE_PRIMARY_BACKEND", "hippius").lower() != "aws_s3"
        bam_url_s3 = round_data.get("bam_presigned_url")
        bam_url_hip = round_data.get("bam_presigned_url_backup")
        bam_index_url_s3 = round_data.get("bam_index_presigned_url")
        bam_index_url_hip = round_data.get("bam_index_presigned_url_backup")

        if _prefer_hippius:
            bam_url, bam_url_backup = bam_url_hip, bam_url_s3
            bam_index_url, bam_index_url_backup = bam_index_url_hip, bam_index_url_s3
        else:
            bam_url, bam_url_backup = bam_url_s3, bam_url_hip
            bam_index_url, bam_index_url_backup = bam_index_url_s3, bam_index_url_hip

        # Never leave the primary slot None when only the backup is set:
        # download_file_with_fallback dereferences the primary URL before its
        # try/except, so a None primary would raise instead of falling back.
        bam_url, bam_url_backup = _coalesce_urls(bam_url, bam_url_backup)
        bam_index_url, bam_index_url_backup = _coalesce_urls(bam_index_url, bam_index_url_backup)

        if not bam_url and not bam_url_backup:
            bt.logging.error("Round has no BAM URL - cannot process")
            return None

        from utils.path_utils import safe_round_dir_name
        output_dir = BASE_DIR / "output" / safe_round_dir_name(round_id)
        output_dir.mkdir(parents=True, exist_ok=True)
        bam_path = output_dir / "input.bam"

        print(f"   Downloading BAM from platform...", flush=True)
        bam_sha256 = round_data.get("bam_sha256")
        downloaded = download_file_with_fallback(
            bam_url, bam_path, backup_url=bam_url_backup,
            expected_sha256=bam_sha256, show_progress=True
        )

        if not downloaded or not downloaded.exists():
            bt.logging.error("Failed to download BAM from platform (primary and backup)")
            return None

        bam_size_gb = downloaded.stat().st_size / (1024**3)
        print(f"   Downloaded: {bam_size_gb:.2f} GB", flush=True)

        # Download BAM index if available (with backup fallback)
        # Always clear old index to prevent stale index with re-downloaded BAM
        bam_index = Path(str(bam_path) + ".bai")
        if bam_index.exists():
            bam_index.unlink()
        if bam_index_url:
            # _coalesce_urls above guarantees bam_index_url is set whenever any
            # index URL (primary or backup) exists, so the fallback covers both.
            print(f"   Downloading BAM index...", flush=True)
            index_downloaded = download_file_with_fallback(
                bam_index_url, bam_index, backup_url=bam_index_url_backup, show_progress=False
            )
            if index_downloaded and index_downloaded.exists():
                print(f"   BAM index downloaded", flush=True)
            else:
                bam_index_url = None

        # Create local index if not downloaded
        if not bam_index.exists():
            print(f"   Creating BAM index locally...", flush=True)
            index_cmd = [
                "docker", "run", "--rm",
                "-v", f"{bam_path.parent}:/data",
                "quay.io/biocontainers/samtools:1.20--h50ea8bc_0",
                "samtools", "index", f"/data/{bam_path.name}",
            ]
            result = subprocess.run(index_cmd, capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                print(f"   BAM index created", flush=True)
            else:
                bt.logging.warning(f"Failed to create BAM index: {result.stderr}")

        return bam_path

    async def _run_variant_calling(self, bam_path, region, tool_config, output_dir, force_rerun=False):
        """Run variant calling or reuse existing results, returning (variant_count, elapsed)."""
        # Check if variant calling was already completed with same config (restart recovery)
        output_vcf = output_dir / "output.vcf.gz"
        vcf_meta = output_dir / "output.meta.json"
        skip_variant_calling = False
        variant_count = 0
        elapsed = 0.0

        if force_rerun:
            # Demo mode: always re-run so users can test their tools
            output_vcf.unlink(missing_ok=True)
            vcf_meta.unlink(missing_ok=True)

        if output_vcf.exists() and output_vcf.stat().st_size > MIN_VCF_SIZE_BYTES:
            # Check if the existing VCF was produced with the same config
            config_matches = False
            if vcf_meta.exists():
                try:
                    saved = json.loads(vcf_meta.read_text())
                    if saved.get("tool_config") == tool_config:
                        config_matches = True
                    else:
                        bt.logging.info("Config changed since last run, re-running variant calling")
                except Exception:
                    pass

            if config_matches:
                try:
                    with gzip.open(output_vcf, 'rt') as f:
                        for line in f:
                            if not line.startswith('#'):
                                variant_count += 1
                    if variant_count > 0:
                        skip_variant_calling = True
                        print(f"   Reusing existing VCF ({variant_count} variants, config unchanged)", flush=True)
                except Exception:
                    bt.logging.warning("Existing VCF corrupt, re-running variant calling")
                    output_vcf.unlink(missing_ok=True)
                    vcf_meta.unlink(missing_ok=True)

        if not skip_variant_calling:
            print(f"   Running variant calling with {self.variant_caller.upper()}...", flush=True)
            start_time = time.time()

            # Print elapsed time every 30s in a background thread
            # (variant_call blocks the event loop, so asyncio ticker won't work)
            ticker_stop = threading.Event()
            def _progress_ticker():
                while not ticker_stop.wait(POLL_INTERVAL_SECONDS):
                    mins, secs = divmod(int(time.time() - start_time), 60)
                    print(f"   Calling variants... {mins}m {secs}s", flush=True)

            ticker = threading.Thread(target=_progress_ticker, daemon=True)
            ticker.start()
            try:
                _, _, variant_count = await self.execute_template(bam_path, region, config=tool_config)
            finally:
                ticker_stop.set()

            elapsed = time.time() - start_time
            print(f"   Variant calling complete: {variant_count} variants in {elapsed:.1f}s", flush=True)

            # Save config metadata so we can detect config changes on restart
            try:
                vcf_meta.write_text(json.dumps({"tool_config": tool_config}))
            except Exception:
                pass

        return variant_count, elapsed

    def _make_commitment(self, round_id, tool_config):
        """Commit to the config about to be submitted, and publish it on chain.

        Returns ``(commitment, block, nonce)``; any may be None. Never raises —
        a commitment is supplementary evidence, not a precondition for mining.

        The nonce is returned so it can be REVEALED to the platform with the
        config. Without it the platform holds a digest it cannot check, so the
        commitment proves nothing to anyone but us. Revealing it costs nothing:
        it only opens this one commitment, over a config we are sending in the
        same request anyway.

        Two ordering invariants: the nonce is persisted before anything is
        published (a commitment whose nonce was lost can never be opened), and
        the chain write precedes the platform submission so the block timestamp
        predates what the platform saw.
        """
        try:
            nonce = config_commit.new_nonce()
            commitment = config_commit.compute_commitment(
                netuid=self.config.netuid,
                round_id=round_id,
                hotkey=self.wallet.hotkey.ss58_address,
                tool_name=self.variant_caller,
                tool_config=tool_config,
                nonce=nonce,
            )
        except Exception as e:
            bt.logging.warning(f"Round {round_id[:8]}...: commitment not computed ({e})")
            return None, None, None

        # Persist the nonce before publishing; see the docstring invariant.
        # The block is not known yet and is deliberately absent rather than
        # guessed from the chain head — the extrinsic lands a block or more
        # later, and a height sampled now names a block the commitment is not in.
        block = None
        try:
            config_commit.CommitmentLedger().record({
                "round_id": round_id,
                "hotkey": self.wallet.hotkey.ss58_address,
                "netuid": self.config.netuid,
                "tool_name": self.variant_caller,
                "nonce": nonce,
                "commitment": commitment,
                "block": None,
                "tool_config": config_commit.submission_config(tool_config),
            })
        except Exception as e:
            bt.logging.warning(
                f"Round {round_id[:8]}...: could not persist commitment nonce ({e}); "
                f"not publishing a commitment that could never be opened"
            )
            return None, None, None

        try:
            payload = config_commit.chain_payload(round_id, commitment)
            ok, reason, block = bt_compat.commit(
                self.subtensor,
                wallet=self.wallet,
                netuid=self.config.netuid,
                data=payload,
            )
            if ok:
                print(f"   Commitment published on chain: {commitment[:16]}...", flush=True)
                bt.logging.info(f"Round {round_id[:8]}...: commitment on chain at block {block}")
                # Append the height it actually landed at. Written after the
                # fact because only the receipt knows it; the nonce record above
                # is what makes the commitment openable, and that is already
                # safe on disk.
                try:
                    config_commit.CommitmentLedger().record({
                        "round_id": round_id,
                        "hotkey": self.wallet.hotkey.ss58_address,
                        "commitment": commitment,
                        "block": block,
                        "published": True,
                    })
                except Exception:  # noqa: BLE001 - the commitment is already on chain
                    pass
            else:
                # Rate limiting is normal — subtensor enforces a minimum block
                # interval between commitments from one hotkey.
                bt.logging.warning(
                    f"Round {round_id[:8]}...: chain commitment skipped ({reason}); "
                    f"still submitting the commitment to the platform"
                )
        except Exception as e:
            bt.logging.warning(f"Round {round_id[:8]}...: chain commitment failed ({e})")

        return commitment, block, nonce

    async def _config_commitment_enabled(self) -> bool:
        """Whether the platform is asking miners to commit on chain.

        Fails to False on any doubt: an unreadable policy, an unreachable
        platform, or a demo run. Committing costs an extrinsic per submission,
        so the safe direction when we cannot tell is not to.
        """
        if (
            getattr(self, "demo", False)
            or getattr(self, "subtensor", None) is None
            or getattr(self, "wallet", None) is None
        ):
            return False
        try:
            network_config = await self.platform_client.get_network_config()
        except Exception as e:  # noqa: BLE001 - a commitment is not worth a round
            bt.logging.warning(f"Could not read the commitment policy ({e}); not committing")
            return False
        if not isinstance(network_config, dict):
            return False
        return network_config.get("config_commitment_enabled") is True

    async def _submission_payment(self, round_id):
        """Buy an extra submission when this round's free allowance is used up.

        Returns ``(proof, blocked)``. ``blocked`` is True when payment was
        required but could not be made, and the caller must then not submit.

        Fee and destination come from the platform's policy; when no policy is
        advertised this returns ``(None, False)`` and nothing is paid.
        """
        # Demo mode has no wallet and no subtensor, so there is nothing to sign
        # with and nothing to spend. Reaching the payment path would crash on
        # wallet.coldkeypub; more importantly, a sandbox must never move real
        # money regardless of what the platform advertises.
        if getattr(self, "demo", False) or self.wallet is None or self.subtensor is None:
            return None, False

        # The allowance is per HOTKEY, and the platform's count is still the
        # authoritative one: a local tally is lost across a restart and drifts
        # from the platform whenever a submission is retried, and the miner would
        # only discover the shortfall by being rejected. Fall back to the local
        # count when the platform does not report one — an older platform, or the
        # policy off.
        already = self._hotkey_submissions_used.get(round_id)
        if already is None:
            already = self.round_submit_counts.get(round_id, 0)
        try:
            policy = submission_payment.SubmissionPolicy(
                await self.platform_client.get_network_config(),
                quoted_fee_tao=self._quoted_fee_tao.get(round_id),
            )
        except Exception as e:
            bt.logging.warning(f"Could not read submission policy ({e}); assuming none")
            return None, False

        # Remembered for the PaymentRequiredError resync in _submit_result,
        # which needs the allowance but not the rest of the policy.
        self._free_submissions_seen = policy.free_submissions

        if not policy.payment_required(already):
            return None, False

        hotkey = self.wallet.hotkey.ss58_address
        print(f"   Free submission for this round already used; paying "
              f"{policy.fee_tao} TAO to resubmit...", flush=True)
        proof = submission_payment.pay_for_resubmission(
            bt_compat=bt_compat, subtensor=self.subtensor, wallet=self.wallet,
            policy=policy, round_id=round_id, hotkey=hotkey,
            ledger=self._payment_ledger, logger=bt.logging,
        )
        if not proof:
            bt.logging.error(
                f"Round {round_id[:8]}...: resubmission payment did not go through; "
                f"skipping this submission rather than sending one that will be refused"
            )
            return None, True
        return proof, False

    def _round_time_remaining(self, round_id) -> Optional[float]:
        """Seconds left in the round's submission window, or None when no
        deadline was recorded. Callers must read None as "unknown", not as
        "closed", and proceed.
        """
        deadline = self._round_deadlines.get(round_id)
        if deadline is None:
            return None
        return deadline - time.monotonic()

    async def _submit_result(self, round_id, tool_config, variant_count, elapsed):
        """Submit variant calling config to the platform and handle the response."""
        # Re-check the window before anything is spent: the gate in process_round
        # runs before the download and the calling run. Uses the recorded deadline
        # rather than re-fetching status, which would delay this submission.
        remaining = self._round_time_remaining(round_id)
        if remaining is not None and remaining < MIN_SPEND_TIME_SECONDS:
            bt.logging.warning(
                f"Round {round_id[:8]}...: submission window closed while calling "
                f"variants ({int(remaining)}s left); not paying a fee or publishing "
                f"a commitment for a submission that cannot be accepted"
            )
            print(f"   Round closed before submission — skipping (no fee paid)", flush=True)
            return False

        payment_proof, blocked = await self._submission_payment(round_id)
        if blocked:
            return

        # The platform decides whether miners commit on chain. Absence means
        # disabled, so an older platform and a deliberately-disabled one behave
        # the same — and a platform we cannot reach never starts us spending
        # extrinsics we were not asked for.
        commitment = commitment_block = commitment_nonce = None
        if await self._config_commitment_enabled():
            commitment, commitment_block, commitment_nonce = self._make_commitment(
                round_id, tool_config
            )

        # Submit config to platform
        print(f"   Submitting config to platform...", flush=True)
        try:
            result = await self.platform_client.submit_config(
                round_id=round_id,
                tool_name=self.variant_caller,
                tool_config=tool_config,
                variant_count=variant_count,
                runtime_seconds=elapsed,
                config_commitment=commitment,
                commitment_block=commitment_block,
                config_nonce=commitment_nonce,
                payment_proof=payment_proof,
            )
        except PaymentRequiredError as e:
            # Trust the platform's verdict over the local count. Resync to the
            # allowance, not to 1: payment_required() fires at
            # `count >= free_submissions`, so a smaller bump never reaches it.
            required = max(1, getattr(self, "_free_submissions_seen", 1) or 1)
            self.round_submit_counts[round_id] = max(
                self.round_submit_counts.get(round_id, 0), required
            )
            bt.logging.warning(
                f"Round {round_id[:8]}...: payment required ({e}); "
                f"local count resynced to {self.round_submit_counts[round_id]}"
            )
            return

        if result.get("success"):
            # Keyed on the platform's verdict, not the HTTP status: a 200 carrying
            # success:false consumes neither the submit count nor the proof.
            self.round_submit_counts[round_id] = (
                self.round_submit_counts.get(round_id, 0) + 1
            )
            if payment_proof:
                self._payment_ledger.mark_spent(
                    round_id, self.wallet.hotkey.ss58_address, payment_proof
                )
            self.submitted_rounds.add(round_id)
            if round_id in self._submit_order:
                self._submit_order.remove(round_id)
            self._submit_order.append(round_id)
            submission_id = result.get("submission_id", "unknown")
            print(f"   Config submitted successfully", flush=True)
            print(f"   Submission ID: {str(submission_id)[:16]}...", flush=True)
            bt.logging.info(f"Round {round_id[:8]}... submitted: {variant_count} variants")

            # /v2/demo/submit-result returns is_demo=true on success. Surface
            # the friendly DEMO COMPLETE banner so a new operator sees a
            # clear "your pipeline works" signal instead of just a generic
            # "submitted" log. Polling keeps running but submitted_rounds
            # dedupes — subsequent loops will skip until process restart.
            if result.get("is_demo"):
                print(f"\n{'='*60}", flush=True)
                print(f"   DEMO COMPLETE", flush=True)
                print(f"   Variant calling finished successfully!", flush=True)
                print(f"   Your system is ready to mine on Subnet 107.", flush=True)
                print(f"", flush=True)
                print(f"   Submission was accepted by the demo sandbox — nothing", flush=True)
                print(f"   is persisted, no score is computed, no TAO is earned.", flush=True)
                print(f"", flush=True)
                print(f"   To actually SCORE and improve your config, use practice", flush=True)
                print(f"   mode — it serves fully-answered chr18/chr19/chr20/chr21/chr22 samples and", flush=True)
                print(f"   scores your config with the exact validator scorer:", flush=True)
                print(f"     python neurons/miner.py --practice --config configs/{self.variant_caller}.conf", flush=True)
                print(f"", flush=True)
                print(f"   Register your hotkey on subnet 107 to participate in", flush=True)
                print(f"   live rounds:", flush=True)
                print(f"     btcli subnets register --netuid 107 \\", flush=True)
                print(f"       --wallet.name <name> --wallet.hotkey <hotkey>", flush=True)
                print(f"{'='*60}", flush=True)

            # Cleanup old rounds from tracking (keep last 10)
            if len(self.submitted_rounds) > 10:
                # submitted_rounds is a set and carries no order; _submit_order
                # supplies it so the ten kept are the ten most recent.
                keep = [r for r in self._submit_order if r in self.submitted_rounds][-10:]
                self.submitted_rounds = set(keep)
                self._submit_order = keep
                self.round_submit_counts = {
                    k: v for k, v in self.round_submit_counts.items() if k in self.submitted_rounds
                }

            return True
        else:
            bt.logging.warning(f"Config submission failed: {result}")
            return False

    def _get_tool_config(self) -> Dict[str, Any]:
        """Get the tool configuration for the current variant caller.

        Loads parameters from configs/{tool}.conf files.
        Only includes QUALITY-AFFECTING parameters that are whitelisted in templates/tool_params.py.
        System parameters (threads, memory, timeout) are handled separately and NOT submitted to platform
        to prevent exploitation (e.g., miner submitting threads=999 to crash validators).
        """
        base_config = {
            "tool": self.variant_caller,
            "version": get_tool_version(self.variant_caller),
        }

        # Load tool-specific parameters from config files
        # Miners can customize configs by editing configs/{tool}.conf
        try:
            tool_options = extract_tool_options(self.variant_caller)

            # Wrap options in tool-specific key for compatibility with templates
            if self.variant_caller == "gatk":
                base_config["gatk_options"] = tool_options
            elif self.variant_caller == "deepvariant":
                base_config["deepvariant_options"] = tool_options
            elif self.variant_caller == "bcftools":
                base_config["bcftools_options"] = tool_options

            bt.logging.info(f"Loaded {len(tool_options)} parameters from {self.variant_caller}.conf")

        except (FileNotFoundError, ValueError) as e:
            bt.logging.warning(f"Could not load config file for {self.variant_caller}: {e}")
            bt.logging.warning("Using minimal default configuration")

            # Minimal fallback configs if config files are missing
            if self.variant_caller == "gatk":
                base_config["gatk_options"] = {"min_base_quality_score": 10}
            elif self.variant_caller == "deepvariant":
                base_config["deepvariant_options"] = {"model_type": "WGS"}
            elif self.variant_caller == "bcftools":
                base_config["bcftools_options"] = {"min_BQ": 1}

        return base_config

    def _cleanup_old_files(self, max_age_hours: int = 2):
        """Remove old task output directories."""
        cutoff_time = time.time() - (max_age_hours * 3600)
        output_dir = BASE_DIR / "output"

        if not output_dir.exists():
            return

        total_cleaned = 0
        total_bytes = 0

        for task_dir in output_dir.iterdir():
            try:
                if task_dir.is_dir() and task_dir.stat().st_mtime < cutoff_time:
                    dir_size = sum(f.stat().st_size for f in task_dir.rglob('*') if f.is_file())
                    shutil.rmtree(task_dir)
                    total_cleaned += 1
                    total_bytes += dir_size
            except Exception as e:
                bt.logging.debug(f"Cleanup failed for {task_dir}: {e}")

        if total_cleaned > 0:
            bt.logging.info(f"Cleaned {total_cleaned} task directories ({total_bytes / (1024**3):.2f} GB)")

    async def run_async(self):
        """Run the miner."""
        bt.logging.info("Starting miner...")

        print(f"\n{'='*60}", flush=True)
        print(f"MINOS MINER" + ("  [DEMO MODE]" if self.demo else ""), flush=True)
        print(f"{'='*60}", flush=True)
        print(f"   Hotkey: {self.hotkey_ss58[:16]}...", flush=True)
        if self.demo:
            print(f"   Mode: demo (ephemeral keypair, /v2/demo/* sandbox)", flush=True)
            print(f"   No chain connection, no TAO earned", flush=True)
        else:
            print(f"   UID: {self.my_subnet_uid}", flush=True)
            print(f"   Network: {self.config.subtensor.network}", flush=True)
            print(f"   Netuid: {self.config.netuid}", flush=True)
        print(f"   Variant Caller: {self.variant_caller}", flush=True)
        print(f"   Config: configs/{self.variant_caller}.conf", flush=True)
        print(f"   Docker: {'Available' if is_docker_available() else 'Not Available'}", flush=True)
        print(f"   Platform: {os.getenv('PLATFORM_URL')}", flush=True)
        print(f"{'='*60}", flush=True)

        # Test platform connectivity
        print(f"\n   Testing platform connection...", flush=True)
        # A failed check must not end the process: the poll loop tolerates platform
        # errors per round, so enter it regardless and let a later round recover.
        try:
            healthy, reason = await self.platform_client.health_check_detail()
            if healthy:
                print(f"   Platform connection: OK", flush=True)
            else:
                print(f"   Platform connection: FAILED ({reason}) - "
                      f"starting anyway, will retry each round", flush=True)
                bt.logging.warning(f"Platform unreachable at startup ({reason}); "
                                   f"entering poll loop and retrying per round")
        except Exception as e:
            print(f"   Platform connection: ERROR - {e} - starting anyway", flush=True)
            bt.logging.warning(f"Platform health check raised {type(e).__name__}: {e}; "
                               f"entering poll loop and retrying per round")

        print(f"\n   Round Mode: ENABLED (polling every {POLL_INTERVAL_SECONDS}s)", flush=True)
        print(f"   Rounds: 72-minute continuous cycles (Bittensor tempo)", flush=True)
        print(f"   Press Ctrl+C to stop\n", flush=True)

        poll_interval = POLL_INTERVAL_SECONDS
        sync_count = 0
        rounds_participated = 0

        try:
            while True:
                # Poll for rounds
                try:
                    participated = await self.process_round()
                    if participated:
                        rounds_participated += 1
                        print(f"   Total rounds participated: {rounds_participated}", flush=True)
                except Exception as e:
                    bt.logging.warning(f"Round polling error: {e}")

                await asyncio.sleep(poll_interval)
                sync_count += 1

                # Sync metagraph every 2 minutes (skipped in demo — no chain conn)
                if sync_count % 4 == 0 and self.metagraph is not None:
                    # sync_metagraph propagates by design; a dropped websocket must
                    # not take the poll loop down, and a stale metagraph is harmless.
                    try:
                        bt_compat.sync_metagraph(self.metagraph, self.subtensor)
                    except Exception as e:
                        bt.logging.warning(f"Metagraph sync failed (continuing): {e}")

                # Heartbeat every 5 minutes
                if sync_count % 10 == 0:
                    uptime_min = (sync_count * poll_interval) // 60
                    print(f"   Heartbeat | {time.strftime('%H:%M:%S')} | Uptime: {uptime_min} min | Rounds: {rounds_participated}", flush=True)

                # Cleanup every 10 minutes
                if sync_count % 20 == 0:
                    self._cleanup_old_files(max_age_hours=4)

        except KeyboardInterrupt:
            print(f"\n{'='*60}", flush=True)
            print(f"   MINER SHUTTING DOWN", flush=True)
            print(f"{'='*60}", flush=True)
            print(f"   Total uptime: {(sync_count * poll_interval) // 60} minutes", flush=True)
            print(f"   Rounds participated: {rounds_participated}", flush=True)

    def run(self):
        """Run the miner (sync wrapper)."""
        asyncio.run(self.run_async())


# ---------------------------------------------------------------------------
# Practice-mode UI helpers. Match the setup wizard's look (rich + questionary)
# when the terminal is interactive, and degrade gracefully to plain text when
# it isn't (piped input, non-TTY, or the libs are missing) so scripted runs
# and --sample_id still work.
# ---------------------------------------------------------------------------
class _PracticeUI:
    def __init__(self):
        self.console = None
        self.questionary = None
        self.Panel = self.Table = self.Text = self.Align = None
        # Interactive only when stdin AND stdout are real TTYs.
        self.interactive = sys.stdin.isatty() and sys.stdout.isatty()
        try:
            from rich.console import Console
            from rich.panel import Panel
            from rich.table import Table
            from rich.text import Text
            from rich.align import Align
            self.console = Console()
            self.Panel, self.Table, self.Text, self.Align = Panel, Table, Text, Align
        except Exception:
            pass
        try:
            import questionary
            from questionary import Style as QStyle
            self.questionary = questionary
            self._qstyle = QStyle([
                ("qmark", "fg:cyan bold"),
                ("question", "bold"),
                ("answer", "fg:green bold"),
                ("pointer", "fg:cyan bold"),
                ("highlighted", "fg:cyan bold"),
                ("selected", "fg:green"),
            ])
        except Exception:
            self._qstyle = None

    def banner(self, tool):
        if self.console and self.Panel:
            self.console.print()
            self.console.print(self.Panel(
                "[bold cyan]MINOS PRACTICE MODE[/]\n"
                "[dim]Run your config on fully-answered samples and see the exact "
                "score a validator would give.[/]\n"
                f"[dim]Tool:[/] [bold green]{tool}[/]   [dim]•  no wallet, no chain[/]",
                border_style="cyan", padding=(1, 2),
            ))
        else:
            print(f"\n{'='*60}\n   PRACTICE MODE  ({tool})\n{'='*60}", flush=True)

    async def select(self, message, choices, plain_prompt):
        """choices: list of (label, value). Returns a value, or None to quit.
        plain_prompt: fn(choices)->value for the non-interactive fallback.

        Async because run_practice runs inside an event loop — questionary's
        blocking .ask() calls asyncio.run() internally, which raises "cannot be
        called from a running event loop". .ask_async() awaits on the loop we
        already have.
        """
        if self.interactive and self.questionary:
            qchoices = [self.questionary.Choice(label, value=val) for label, val in choices]
            ans = await self.questionary.select(message, choices=qchoices, style=self._qstyle).ask_async()
            return ans  # None if the user hit Ctrl-C / esc
        return plain_prompt(choices)

    async def confirm_tool(self, default_tool):
        if self.interactive and self.questionary:
            ans = await self.questionary.select(
                f"Which tool should score your config?",
                choices=[
                    self.questionary.Choice(f"{default_tool}  (from your .env — recommended)", value=default_tool),
                    *[self.questionary.Choice(t, value=t)
                      for t in ("gatk", "deepvariant", "bcftools") if t != default_tool],
                ],
                style=self._qstyle,
            ).ask_async()
            return ans or default_tool
        return default_tool

    def info(self, msg):
        if self.console:
            self.console.print(f"   {msg}")
        else:
            print(f"   {msg}", flush=True)

    def sample_table(self, samples, allow_all):
        if self.console and self.Table:
            t = self.Table(show_header=True, header_style="bold cyan", border_style="cyan", padding=(0, 1))
            t.add_column("#", justify="right", style="bold")
            t.add_column("Sample", style="bold white")
            t.add_column("Chr")
            t.add_column("Region")
            t.add_column("Mutations", justify="right")
            for i, s in enumerate(samples, 1):
                t.add_row(str(i), s.get("sample_id", ""), s.get("chromosome", ""),
                          s.get("region", ""), str(s.get("num_mutations", "")))
            self.console.print(self.Panel(t, title="Practice samples", border_style="cyan", padding=(0, 1)))
        else:
            print("\n   Available practice samples:", flush=True)
            for i, s in enumerate(samples, 1):
                print(f"     {i}. {s.get('sample_id')}  [{s.get('chromosome')} {s.get('region')}]  "
                      f"mutations={s.get('num_mutations')}", flush=True)
            if allow_all:
                print(f"     a. ALL — download every sample", flush=True)

    def result_panel(self, metrics, advanced_score, combined_final, would_record, zero_input,
                     scoring_version=None):
        if not (self.console and self.Panel and self.Table):
            # Plain fallback
            print(f"\n{'='*60}\n   RESULT\n{'='*60}", flush=True)
            print(f"   SNP    F1={metrics.get('f1_snp', 0):.4f}  recall={metrics.get('recall_snp', 0):.4f}  "
                  f"prec={metrics.get('precision_snp', 0):.4f}  FP={metrics.get('fp_snp', 0)}", flush=True)
            print(f"   INDEL  F1={metrics.get('f1_indel', 0):.4f}  recall={metrics.get('recall_indel', 0):.4f}  "
                  f"prec={metrics.get('precision_indel', 0):.4f}  FP={metrics.get('fp_indel', 0)}", flush=True)
            _v = f"  (scoring {scoring_version})" if scoring_version else ""
            print(f"\n   ADVANCED SCORE:   {advanced_score:.4f} / 100{_v}", flush=True)
            if would_record:
                print(f"   COMBINED_FINAL:   {combined_final:.6f}   <-- what a validator records", flush=True)
            elif zero_input:
                print(f"   COMBINED_FINAL:   {combined_final:.6f}   <-- ZERO-INPUT: a validator discards this "
                      f"(called nothing on-target).", flush=True)
            else:
                print(f"   COMBINED_FINAL:   {combined_final:.6f}   <-- out of range; a validator discards this.",
                      flush=True)
            print(f"{'='*60}\n", flush=True)
            return

        # Rich metrics table
        t = self.Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 2))
        t.add_column("Type", style="bold white")
        t.add_column("F1", justify="right")
        t.add_column("Recall", justify="right")
        t.add_column("Precision", justify="right")
        t.add_column("FP", justify="right")
        for label, k in (("SNP", "snp"), ("INDEL", "indel")):
            f1 = metrics.get(f"f1_{k}", 0)
            f1c = "green" if f1 >= 0.95 else "yellow" if f1 >= 0.80 else "red"
            t.add_row(label, f"[{f1c}]{f1:.4f}[/]",
                      f"{metrics.get(f'recall_{k}', 0):.4f}",
                      f"{metrics.get(f'precision_{k}', 0):.4f}",
                      str(metrics.get(f"fp_{k}", 0)))

        # Big score line, color-banded
        band = "green" if combined_final >= 0.90 else "yellow" if combined_final >= 0.70 else "red"
        if would_record:
            _v = f" (scoring {scoring_version})" if scoring_version else ""
            verdict = (
                f"[bold {band}]COMBINED_FINAL  {combined_final:.6f}[/]\n"
                f"[dim]This is exactly what a validator would record{_v}.[/]"
            )
            border = band
        elif zero_input:
            verdict = (f"[bold red]COMBINED_FINAL  {combined_final:.6f}[/]\n"
                       "[red]ZERO-INPUT — a validator DISCARDS this.[/] "
                       "[dim]Your config called nothing on-target; it earns no on-chain score.[/]")
            border = "red"
        else:
            verdict = (f"[bold red]COMBINED_FINAL  {combined_final:.6f}[/]\n"
                       "[red]Out of range — a validator discards this.[/]")
            border = "red"

        from rich.console import Group
        body = Group(
            t,
            self.Text(""),
            self.console.render_str(f"[dim]Advanced score:[/] [bold]{advanced_score:.4f}[/] / 100"),
            self.Text(""),
            self.console.render_str(verdict),
        )
        self.console.print()
        self.console.print(self.Panel(body, title="Your score", border_style=border, padding=(1, 2)))
        self.console.print()


# Single shared UI instance for the process.
_UI = _PracticeUI()


def _load_config_for_scoring(config_path: Optional[str], tool: str) -> Dict[str, Any]:
    """Load a config file into the {tool}_options dict the templates expect.

    Accepts either a GATK-style `.conf` (key=value lines, same format as
    configs/gatk.conf) or a JSON file. JSON may be either a bare options dict
    ({"min_base_quality_score": 10, ...}) or a full config already wrapping
    "<tool>_options". Falls back to configs/<tool>.conf when no path is given —
    exactly the source the live miner submits from.
    """
    opts_key = f"{tool}_options"

    if not config_path:
        # Same path _get_tool_config() uses for a live submission.
        return {opts_key: extract_tool_options(tool)}

    p = Path(config_path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {p}")

    text = p.read_text()
    # Try JSON first (a leading brace is unambiguous vs. key=value .conf).
    stripped = text.lstrip()
    if stripped.startswith("{"):
        data = json.loads(text)
        if opts_key in data and isinstance(data[opts_key], dict):
            return {opts_key: data[opts_key]}
        # Bare options dict — strip any non-option scalars defensively.
        return {opts_key: {k: v for k, v in data.items()
                           if not isinstance(v, (dict, list))}}

    # Otherwise parse as a .conf (key=value, # comments, blank lines).
    options: Dict[str, Any] = {}
    for line_num, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"{p}:{line_num}: expected key=value, got: {raw!r}")
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        # Coerce to the same scalar types extract_tool_options produces.
        if val.lower() in ("true", "false"):
            options[key] = (val.lower() == "true")
        else:
            try:
                options[key] = int(val)
            except ValueError:
                try:
                    options[key] = float(val)
                except ValueError:
                    options[key] = val
    return {opts_key: options}


def run_offline_score(cfg) -> int:
    """Score a config against a local BAM+truth, exactly as a validator would.

    Pipeline mirrors validator._run_miner_tool -> HappyScorer.score_vcf ->
    AdvancedScorer.compute_advanced_score -> /100 -> _valid_round_score. No
    chain, no wallet, no platform. Returns a process exit code. The run+score
    core is shared with --practice via _run_and_score().
    """
    tool = (getattr(cfg, "variant_caller", None) or os.getenv("MINER_TEMPLATE") or "gatk").lower()

    # --- Validate required inputs ---
    missing = [f for f in ("bam", "truth", "region") if not getattr(cfg, f, None)]
    if missing:
        print(f"ERROR: --score requires {', '.join('--' + m for m in missing)}", flush=True)
        return 2

    bam_path = Path(cfg.bam).resolve()
    truth_path = Path(cfg.truth).resolve()
    region = cfg.region
    mutations_path = Path(cfg.mutations).resolve() if cfg.mutations else None

    # Structurally validate the region before it is interpolated into the
    # variant-caller/Docker command. Expected: <contig>:<start>-<end>.
    if not re.match(r"^[A-Za-z0-9_.]+:\d+-\d+$", region or ""):
        print(f"ERROR: --region must look like chr20:45000000-50000000 (got: {region!r})", flush=True)
        return 2

    if not bam_path.exists():
        print(f"ERROR: BAM not found: {bam_path}", flush=True)
        return 2
    if not truth_path.exists():
        print(f"ERROR: truth VCF not found: {truth_path}", flush=True)
        return 2
    if mutations_path and not mutations_path.exists():
        print(f"ERROR: mutations VCF not found: {mutations_path}", flush=True)
        return 2

    try:
        require_docker()
    except RuntimeError as e:
        print(f"ERROR: {e}", flush=True)
        return 2

    # --- Resolve reference (same convention as execute_template) ---
    chrom = region.split(":")[0] if region else "chr20"
    if cfg.reference:
        ref_path = Path(cfg.reference).resolve()
    else:
        ref_path = BASE_DIR / "datasets" / "reference" / chrom / f"{chrom}.fa"
        if not ref_path.exists():
            legacy = BASE_DIR / "datasets" / "reference" / "chr20.fa"
            if chrom == "chr20" and legacy.exists():
                ref_path = legacy
    if not ref_path.exists():
        print(f"ERROR: reference not found: {ref_path}\n"
              f"       Pass --reference, or place it at datasets/reference/{chrom}/{chrom}.fa", flush=True)
        return 2

    # --- Ensure indexes the template requires (.bai / .fai) ---
    if not _ensure_bam_index(bam_path):
        return 2
    if not _ensure_fasta_index(ref_path):
        return 2

    # --- Load the config to score, wrapped as templates expect ---
    try:
        tool_config = _load_config_for_scoring(cfg.config, tool)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        print(f"ERROR: could not load config: {e}", flush=True)
        return 2

    opts = tool_config.get(f"{tool}_options", {})
    print(f"\n{'='*60}", flush=True)
    print(f"   OFFLINE SELF-SCORE  ({tool})", flush=True)
    print(f"{'='*60}", flush=True)
    print(f"   Region:    {region}", flush=True)
    print(f"   BAM:       {bam_path.name}", flush=True)
    print(f"   Truth:     {truth_path.name}", flush=True)
    print(f"   Mutations: {mutations_path.name if mutations_path else '(none — score may differ from validator)'}", flush=True)
    print(f"   Reference: {ref_path.name}", flush=True)
    print(f"   Config:    {cfg.config or f'configs/{tool}.conf'}  ({len(opts)} params)", flush=True)

    score = _run_and_score(
        tool=tool,
        tool_config=tool_config,
        bam_path=bam_path,
        ref_path=ref_path,
        truth_path=truth_path,
        mutations_path=mutations_path,
        region=region,
        confident_bed=cfg.confident_bed if cfg.confident_bed else None,
        reference_sdf=cfg.reference_sdf if cfg.reference_sdf else None,
        # --score is fully offline, so there is no platform to ask. Use the
        # version this machine last saw the network on; a box that has never
        # reached the platform falls back to v1, the live formula.
        scoring_version=scoring_version_util.resolve(None),
    )
    return 0 if score is not None else 1


def _run_and_score(tool, tool_config, bam_path, ref_path, truth_path,
                   mutations_path, region, confident_bed=None, reference_sdf=None,
                   scoring_version=None):
    """Run a config and score it exactly as a validator would.

    Shared by --score and --practice. Runs the template, then scores with
    HappyScorer + AdvancedScorer and prints the validator's combined_final
    plus the component breakdown. Returns combined_final (float) on success,
    or None on any failure (variant calling failed, no VCF, hap.py returned
    no metrics).

    ``scoring_version`` selects the formula, and must be the one the PLATFORM
    advertises. This output tells an operator it is "exactly what a validator
    would record", so scoring with v1 while the network runs v2 would make that
    sentence false by several points — the two are different scales.
    """
    from utils.scoring import HappyScorer, AdvancedScorer
    from templates import load_template

    # --- Run variant calling (validator._run_miner_tool equivalent) ---
    out_dir = bam_path.parent / "score_run"
    out_dir.mkdir(parents=True, exist_ok=True)
    output_vcf = out_dir / "output.vcf.gz"

    run_config = {
        **tool_config,
        "timeout": GENOMICS_CONFIG.get("variant_calling_timeout", 1800),
        "threads": MINER_CONFIG.get("num_threads", 4),
        "ref_build": "GRCh38",
    }

    print(f"\n   Running {tool.upper()}...", flush=True)
    t0 = time.time()
    template = load_template(tool)
    result = template.variant_call(
        bam_path=bam_path,
        reference_path=ref_path,
        output_vcf_path=output_vcf,
        region=region,
        config=run_config,
    )
    if not result.get("success"):
        print(f"   ERROR: variant calling failed: {result.get('error', 'unknown')}", flush=True)
        return None
    variant_count = result.get("variant_count", 0)
    print(f"   Variants called: {variant_count}  ({time.time() - t0:.0f}s)", flush=True)

    query_vcf = output_vcf if output_vcf.exists() else None
    if not query_vcf:
        for ext in (".vcf.gz", ".vcf"):
            alt = out_dir / f"output{ext}"
            if alt.exists():
                query_vcf = alt
                break
    if not query_vcf:
        print("   ERROR: no output VCF produced", flush=True)
        return None

    # --- Resolve the RTG SDF (required by vcfeval for deterministic scoring) ---
    # If not passed explicitly, look for it next to the reference using the same
    # convention the validator uses: datasets/reference/<chrom>/<chrom>.sdf.
    sdf_arg = reference_sdf
    if not sdf_arg:
        chrom = region.split(":")[0] if region else "chr20"
        for cand in (
            BASE_DIR / "datasets" / "reference" / chrom / f"{chrom}.sdf",
            BASE_DIR / "datasets" / "reference" / f"{chrom}.sdf",
            ref_path.parent / f"{chrom}.sdf",
        ):
            if cand.is_dir():
                sdf_arg = str(cand)
                break
    if not sdf_arg:
        chrom = region.split(":")[0] if region else "chr20"
        print(f"   ERROR: RTG SDF not found for {chrom} (needed to score).", flush=True)
        print(f"          Expected at datasets/reference/{chrom}/{chrom}.sdf — "
              f"run practice via start-miner.sh, which fetches it.", flush=True)
        return None

    # --- Score exactly as the validator does ---
    print(f"\n   Scoring with hap.py...", flush=True)
    scorer = HappyScorer()
    metrics = scorer.score_vcf(
        truth_vcf=str(truth_path),
        query_vcf=str(query_vcf),
        reference_fasta=str(ref_path),
        confident_bed=confident_bed,
        region=region,
        reference_sdf=sdf_arg,
        mutations_vcf=str(mutations_path) if mutations_path else None,
    )
    if metrics is None:
        print("   ERROR: hap.py returned no valid metrics", flush=True)
        return None

    version = scoring_version or scoring_version_util.V1
    advanced_score = AdvancedScorer.compute_advanced_score(metrics)
    if version == scoring_version_util.V2:
        # v2 needs per-variant records, which live in the annotated hap.py VCF.
        score_v2 = None
        happy_vcf = metrics.get("happy_vcf_path")
        if happy_vcf and Path(happy_vcf).exists():
            try:
                from utils.scoring import parse_happy_vcf, difficulty_class_counts

                records = parse_happy_vcf(happy_vcf, truth_vcf_path=str(truth_path))
                if records is None:
                    print("   v2 score unavailable: hap.py VCF could not be parsed "
                          "completely (a partial parse would inflate the score)", flush=True)
                elif records:
                    score_v2 = AdvancedScorer.compute_score_v2(
                        metrics, difficulty_class_counts(records)
                    )
            except Exception as e:  # noqa: BLE001
                print(f"   v2 score unavailable ({e})", flush=True)
        if score_v2 is None:
            # The validator skips a miner outright here rather than mixing
            # scales. Say so instead of printing a v1 number under a v2 banner.
            print(
                "   ERROR: the network scores with v2, but v2 could not be "
                "computed for this run — a validator would not record a score.",
                flush=True,
            )
            return None
        advanced_score = score_v2
    combined_final = advanced_score / 100.0

    # Two guards match what a validator applies before recording a score:
    #   1. combined_final must be a finite number in (0, 1].
    #   2. a config that calls nothing on-target (SNP and indel F1 both 0) is
    #      not a scorable result — it is discarded rather than recorded, so it
    #      earns no on-chain score. Report that honestly instead of implying
    #      the number would count.
    valid_range = (isinstance(combined_final, float) and combined_final == combined_final
                   and 0.0 < combined_final <= 1.0)
    # Match the validator's empty-on-target discard EXACTLY (see the validator's
    # _is_zero_input_advanced_fingerprint): both F1s zero AND the fused score in
    # the all-zero band. Using only the F1s would over-report "discarded" for a
    # zero-F1 config that still made calls (germline/FP) — the validator records
    # those, so the local preview must not claim otherwise.
    zero_input = (
        (metrics.get("f1_snp") or 0.0) == 0.0
        and (metrics.get("f1_indel") or 0.0) == 0.0
        and 0.24999 <= combined_final <= 0.25001
    )
    would_record = valid_range and not zero_input

    # --- Report (mirror the validator's component breakdown), styled ---
    _UI.result_panel(metrics, advanced_score, combined_final, would_record, zero_input,
                     scoring_version=version)
    return combined_final


def _ensure_bam_index(bam_path: Path) -> bool:
    """Create a .bai next to the BAM if none exists (samtools via Docker)."""
    if Path(f"{bam_path}.bai").exists() or bam_path.with_suffix(".bam.bai").exists():
        return True
    print(f"   Creating BAM index for {bam_path.name}...", flush=True)
    try:
        subprocess.run(
            ["docker", "run", "--rm", "-v", f"{bam_path.parent}:/data",
             "quay.io/biocontainers/samtools:1.20--h50ea8bc_0",
             "samtools", "index", f"/data/{bam_path.name}"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600,
        )
        return Path(f"{bam_path}.bai").exists() or bam_path.with_suffix(".bam.bai").exists()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"   ERROR: failed to index BAM: {e}", flush=True)
        return False


def _ensure_fasta_index(ref_path: Path) -> bool:
    """Create a .fai next to the reference if none exists (samtools via Docker)."""
    if Path(f"{ref_path}.fai").exists():
        return True
    print(f"   Creating reference index for {ref_path.name}...", flush=True)
    try:
        subprocess.run(
            ["docker", "run", "--rm", "-v", f"{ref_path.parent}:/data",
             "quay.io/biocontainers/samtools:1.20--h50ea8bc_0",
             "samtools", "faidx", f"/data/{ref_path.name}"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600,
        )
        return Path(f"{ref_path}.fai").exists()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"   ERROR: failed to index reference: {e}", flush=True)
        return False


def _resolve_reference_for(chrom: str) -> Optional[Path]:
    """Locate the reference FASTA for a chromosome (same convention as
    execute_template). Returns None if the contig is not allowlisted or the file
    is not present; chrom is interpolated into the path, so it must be
    validated here."""
    if not isinstance(chrom, str) or not CHROMOSOME_PATTERN.match(chrom):
        return None
    ref_path = BASE_DIR / "datasets" / "reference" / chrom / f"{chrom}.fa"
    if ref_path.exists():
        return ref_path
    legacy = BASE_DIR / "datasets" / "reference" / "chr20.fa"
    if chrom == "chr20" and legacy.exists():
        return legacy
    return None


def _coalesce_urls(primary, backup):
    """Ensure the primary slot is never None when a usable URL exists.

    download_file_with_fallback -> download_file does `url.startswith(...)`
    BEFORE its try/except, so a None primary raises an uncaught AttributeError
    instead of falling back. If primary is falsy, promote the backup into the
    primary slot. Returns (primary, backup) with primary guaranteed non-None
    whenever either input was set.
    """
    if not primary and backup:
        return backup, None
    return primary, backup


def _reuse_ok(path: Path, expected_sha256) -> bool:
    """Whether an already-on-disk file may be reused without re-downloading.

    Requires the file to exist and be non-empty. When the sample carries a
    sha256, the file must also match it — so a truncated file left by an
    interrupted prior run is NOT silently reused (which would corrupt the
    self-score). With no expected sha256, a non-empty file is accepted.
    """
    try:
        if not path.exists() or path.stat().st_size == 0:
            return False
    except OSError:
        return False
    if expected_sha256:
        try:
            from utils.file_utils import compute_sha256
            if compute_sha256(path) != expected_sha256:
                print(f"   {path.name} on disk fails sha256 — will re-download.", flush=True)
                return False
        except Exception:
            # If we can't verify, be safe and re-download.
            return False
    return True


def _download_index_best_effort(url, backup, dest: Path) -> None:
    """Download an index file (.bai/.tbi) if a URL slot exists; never raises.

    Index files are optional (hap.py/samtools can rebuild them), so any
    failure here is non-fatal. Coalesces the primary slot so a None primary
    with a set backup doesn't crash download_file_with_fallback.
    """
    if not url and not backup:
        return
    primary, bkp = _coalesce_urls(url, backup)
    try:
        download_file_with_fallback(primary, dest, backup_url=bkp, show_progress=False)
    except Exception as e:
        bt.logging.debug(f"Index download for {dest.name} failed (non-fatal): {e}")


def _download_practice_files(sample: dict) -> Optional[dict]:
    """Download a practice sample's BAM + truth + mutations, reusing any
    already-downloaded files. Returns paths dict, or None on failure.

    Files land in datasets/practice/<sample_id>/. Uses the primary URL the
    platform returns, with the backup URL as fallback — same ordering as the
    round path.
    """
    from utils.path_utils import safe_round_dir_name

    sample_id = sample["sample_id"]
    out_dir = BASE_DIR / "datasets" / "practice" / safe_round_dir_name(sample_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    _prefer_hippius = os.getenv("STORAGE_PRIMARY_BACKEND", "hippius").lower() != "aws_s3"

    bam_path = out_dir / "input.bam"
    truth_path = out_dir / "truth.vcf.gz"
    mutations_path = out_dir / "mutations.vcf.gz"

    # --- BAM (required) ---
    # Reuse only if the BAM is non-empty (+ sha256 match when provided) AND its
    # index is present — a truncated BAM or a missing index forces a redownload.
    _bam_indexed = (Path(f"{bam_path}.bai").exists()
                    or bam_path.with_suffix(".bam.bai").exists())
    if _reuse_ok(bam_path, sample.get("bam_sha256")) and _bam_indexed:
        print(f"   BAM already downloaded — reusing {bam_path.name}", flush=True)
    else:
        # The platform returns a primary URL and a backup URL. Use the primary
        # by default; swap to the backup as primary only when the operator sets
        # STORAGE_PRIMARY_BACKEND (same knob the round path honors).
        if _prefer_hippius:
            bam_url = sample.get("bam_presigned_url_backup") or sample.get("bam_presigned_url")
            bam_backup = sample.get("bam_presigned_url")
        else:
            bam_url = sample.get("bam_presigned_url")
            bam_backup = sample.get("bam_presigned_url_backup")
        if not bam_url and not bam_backup:
            print("   ERROR: sample has no BAM URL", flush=True)
            return None
        print(f"   Downloading BAM...", flush=True)
        primary, backup = _coalesce_urls(bam_url, bam_backup)
        got = download_file_with_fallback(
            primary, bam_path, backup_url=backup,
            expected_sha256=sample.get("bam_sha256"), show_progress=True,
        )
        if not got or not got.exists():
            print("   ERROR: failed to download BAM", flush=True)
            return None
        # BAM index — best-effort; hap.py/GATK need it, but we can build it
        # locally if the download slot is absent or fails.
        bam_index = Path(str(bam_path) + ".bai")
        _download_index_best_effort(
            sample.get("bam_index_presigned_url"),
            sample.get("bam_index_presigned_url_backup"),
            bam_index,
        )
        if not bam_index.exists():
            if not _ensure_bam_index(bam_path):
                return None

    # --- Truth VCF (required) ---
    # Reuse only if the file is non-empty AND (when the sample carries a
    # sha256) matches it — a truncated file from an interrupted prior run must
    # NOT be silently reused, or the self-score is wrong.
    # The .tbi must be present too: it cannot be rebuilt here (tabix may be off
    # PATH) and the scoring slice fails closed without it.
    _truth_indexed = Path(str(truth_path) + ".tbi").exists()
    if _reuse_ok(truth_path, sample.get("truth_vcf_sha256")) and _truth_indexed:
        print(f"   Truth already downloaded — reusing {truth_path.name}", flush=True)
    else:
        truth_url = sample.get("truth_vcf_presigned_url")
        truth_backup = sample.get("truth_vcf_presigned_url_backup")
        if not truth_url and not truth_backup:
            print("   ERROR: sample has no truth VCF URL", flush=True)
            return None
        print(f"   Downloading truth VCF...", flush=True)
        primary, backup = _coalesce_urls(truth_url, truth_backup)
        got = download_file_with_fallback(
            primary, truth_path, backup_url=backup,
            expected_sha256=sample.get("truth_vcf_sha256"), show_progress=True,
        )
        if not got or not got.exists():
            print("   ERROR: failed to download truth VCF", flush=True)
            return None
        # Truth index (.tbi) — nice to have; hap.py can build it if absent
        _download_index_best_effort(
            sample.get("truth_vcf_index_presigned_url"),
            sample.get("truth_vcf_index_presigned_url_backup"),
            Path(str(truth_path) + ".tbi"),
        )

    # --- Mutations VCF (optional but strongly recommended) ---
    # Verify its sha256 like the BAM and truth — the validator downloads the
    # mutations VCF with the same checksum, and it defines the scoring scope,
    # so a corrupt copy would silently change the score.
    have_mutations = False
    _mut_indexed = Path(str(mutations_path) + ".tbi").exists()
    if _reuse_ok(mutations_path, sample.get("mutations_vcf_sha256")) and _mut_indexed:
        print(f"   Mutations already downloaded — reusing {mutations_path.name}", flush=True)
        have_mutations = True
    else:
        mut_url = sample.get("mutations_vcf_presigned_url")
        mut_backup = sample.get("mutations_vcf_presigned_url_backup")
        if mut_url or mut_backup:
            print(f"   Downloading mutations VCF...", flush=True)
            primary, backup = _coalesce_urls(mut_url, mut_backup)
            got = download_file_with_fallback(
                primary, mutations_path, backup_url=backup,
                expected_sha256=sample.get("mutations_vcf_sha256"), show_progress=True,
            )
            if got and got.exists():
                have_mutations = True
                _download_index_best_effort(
                    sample.get("mutations_vcf_index_presigned_url"),
                    sample.get("mutations_vcf_index_presigned_url_backup"),
                    Path(str(mutations_path) + ".tbi"),
                )

    return {
        "bam": bam_path,
        "truth": truth_path,
        "mutations": mutations_path if have_mutations else None,
        "dir": out_dir,
    }


async def run_practice(cfg) -> int:
    """Interactive practice mode: pick a sample, download it, run + self-score.

    No chain. Uses an ephemeral keypair against the platform's /v2/practice/*
    namespace (which the platform 404s unless practice mode is enabled). The
    run+score core is shared with --score via _run_and_score().
    """
    tool = (getattr(cfg, "variant_caller", None) or os.getenv("MINER_TEMPLATE") or "gatk").lower()

    try:
        require_docker()
    except RuntimeError as e:
        print(f"ERROR: {e}", flush=True)
        return 2

    platform_url = os.getenv("PLATFORM_URL", "")
    if not platform_url:
        print("ERROR: PLATFORM_URL not set — required for practice mode.", flush=True)
        return 2

    keypair = Keypair.create_from_uri(f"//practice-{secrets.token_hex(4)}")
    try:
        # PlatformClient.__init__ raises ValueError for a non-https,
        # non-localhost PLATFORM_URL — catch it here so a misconfigured URL
        # prints a clean error instead of a raw traceback.
        client = MinerPlatformClient(
            keypair=keypair,
            config=PlatformConfig(base_url=platform_url,
                                  timeout=float(os.getenv("PLATFORM_TIMEOUT", "60"))),
            demo=False,
        )
    except ValueError as e:
        print(f"ERROR: invalid PLATFORM_URL: {e}", flush=True)
        return 2

    _UI.banner(tool)

    # The platform decides which formula the network scores with. Ask it, so
    # this preview matches what a validator would actually record — the output
    # says exactly that, and v1 and v2 differ by several points on the same
    # callset. A platform that cannot be reached falls back to the version this
    # machine last saw, then to v1.
    practice_scoring_version = scoring_version_util.V1
    try:
        practice_scoring_version = scoring_version_util.resolve(
            await client.get_network_config()
        )
    except Exception:  # noqa: BLE001 - a preview is not worth failing over
        practice_scoring_version = scoring_version_util.resolve(None)

    # --- Fetch the sample menu ---
    try:
        listing = await client.list_practice_samples()
    except PlatformClientError as e:
        _UI.info(f"[red]ERROR:[/] {e}" if _UI.console else f"ERROR: {e}")
        return 1
    samples = listing.get("samples", [])
    if not samples:
        _UI.info("No practice samples available on this platform.")
        return 1

    # Flags let a caller shortcut the menus (non-interactive use):
    #   --config X  -> action defaults to "score" (with that config)
    #   --sample_id -> that sample, single-shot
    flag_sample_id = getattr(cfg, "sample_id", None)
    flag_config = cfg.config
    single_shot = bool(flag_sample_id)

    async def _fetch_and_download(sample_id):
        """Fetch a sample's URLs and download its files. Returns files dict or None."""
        print(f"\n   Fetching '{sample_id}'...", flush=True)
        try:
            full = await client.get_practice_sample(sample_id)
        except PlatformClientError as e:
            print(f"   ERROR: {e}", flush=True)
            return None
        files = _download_practice_files(full)
        if files is None:
            return None
        return full, files

    def _score_one(full, files, score_tool, score_config_src):
        """Resolve ref+SDF and score one downloaded sample with the given tool/config."""
        region = full.get("region")
        chrom = safe_chrom(region)
        if chrom is None:
            print(f"   ERROR: sample region {region!r} does not name a supported "
                  f"chromosome (chr1-22, chrX, chrY, chrM)", flush=True)
            return False
        ref_path = _resolve_reference_for(chrom)
        if ref_path is None:
            print(f"   ERROR: reference not found for {chrom}. "
                  f"Place it at datasets/reference/{chrom}/{chrom}.fa", flush=True)
            return False
        if not _ensure_fasta_index(ref_path):
            return False
        try:
            tool_config = _load_config_for_scoring(score_config_src, score_tool)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
            print(f"   ERROR: could not load config: {e}", flush=True)
            return False
        opts = tool_config.get(f"{score_tool}_options", {})
        cfg_name = score_config_src or f'configs/{score_tool}.conf'
        _UI.info(f"[cyan]Scoring[/] with tool=[bold]{score_tool}[/], config=[bold]{cfg_name}[/]  [dim]({len(opts)} params)[/]"
                 if _UI.console else f"Scoring with tool={score_tool}, config={cfg_name}  ({len(opts)} params)")
        if files["mutations"] is None:
            _UI.info("[yellow]NOTE:[/] no mutations VCF for this sample — score may differ slightly from the validator."
                     if _UI.console else "NOTE: no mutations VCF for this sample — score may differ slightly from the validator.")
        # _run_and_score returns the combined_final on success, or None if the
        # variant caller, hap.py, SDF resolution, or scoring failed. Surface
        # that as the outcome so a failed score is reported as failure (and a
        # single-shot / --demo run exits non-zero) instead of a false success.
        result = _run_and_score(
            tool=score_tool,
            tool_config=tool_config,
            bam_path=files["bam"],
            ref_path=ref_path,
            truth_path=files["truth"],
            mutations_path=files["mutations"],
            region=region,
            scoring_version=practice_scoring_version,
        )
        if result is None:
            print("   ERROR: scoring did not complete — see the messages above.", flush=True)
            return False
        return True

    def _plain_action(_choices):
        print("\n   What do you want to do?", flush=True)
        print("     1. Score  — run your config on a sample and see your score", flush=True)
        print("     2. Download — just fetch sample files (BAM + truth + mutations)", flush=True)
        raw = input("\n   Pick 1 or 2 (or 'q' to quit): ").strip().lower()
        if raw in ("q", "quit", "exit"):
            return None
        if raw in ("1", "score", "s"):
            return "score"
        if raw in ("2", "download", "d"):
            return "download"
        return "__invalid__"

    while True:
        # --- Step 1: choose the action (score or download) ---
        if flag_config or single_shot:
            action = "score"
        else:
            try:
                action = await _UI.select(
                    "What do you want to do?",
                    [("Score — run your config and see your score", "score"),
                     ("Download — just fetch a sample's files", "download")],
                    _plain_action,
                )
            except EOFError:
                return 0
            if action is None:
                return 0
            if action == "__invalid__":
                _UI.info("Invalid selection.")
                continue

        # --- Step 1b (score only): confirm the tool (default .env MINER_TEMPLATE) ---
        run_tool = tool
        if action == "score" and not single_shot and not flag_config:
            try:
                run_tool = await _UI.confirm_tool(tool)
            except EOFError:
                return 0
            if not run_tool:
                return 0

        # --- Step 2: choose the sample (with an 'all' option for download) ---
        allow_all = (action == "download")
        if flag_sample_id:
            match = next((s for s in samples if s.get("sample_id") == flag_sample_id), None)
            if not match:
                _UI.info(f"ERROR: sample_id '{flag_sample_id}' not in menu: "
                         f"{[s.get('sample_id') for s in samples]}")
                return 1
            chosen = [flag_sample_id]
        else:
            def _plain_sample(_choices):
                _UI.sample_table(samples, allow_all)
                prompt = ("\n   Pick a sample number"
                          + (" or 'a' for all" if allow_all else "")
                          + " (or 'q' to quit): ")
                raw = input(prompt).strip().lower()
                if raw in ("q", "quit", "exit"):
                    return None
                if allow_all and raw in ("a", "all"):
                    return "__all__"
                if raw.isdigit() and 1 <= int(raw) <= len(samples):
                    return samples[int(raw) - 1]["sample_id"]
                return "__invalid__"

            # Interactive: a rich table for context, then an arrow-key picker.
            if _UI.interactive and _UI.questionary:
                _UI.sample_table(samples, allow_all=False)
            choices = [
                (f"{s.get('sample_id')}  ·  {s.get('chromosome')}  ·  {s.get('num_mutations')} mutations",
                 s["sample_id"])
                for s in samples
            ]
            if allow_all:
                choices.append(("ALL — download every sample", "__all__"))
            try:
                picked = await _UI.select("Pick a sample:", choices, _plain_sample)
            except EOFError:
                return 0
            if picked is None:
                return 0
            if picked == "__invalid__":
                _UI.info("Invalid selection.")
                continue
            chosen = [s["sample_id"] for s in samples] if picked == "__all__" else [picked]

        # --- Act on each chosen sample ---
        for sid in chosen:
            result = await _fetch_and_download(sid)
            if result is None:
                if single_shot:
                    return 1
                continue
            full, files = result
            if action == "download":
                if _UI.console:
                    _UI.console.print(f"   [green]✓[/] Downloaded [bold]{sid}[/] → [dim]{files['dir']}[/]")
                    _UI.console.print(f"       [dim]BAM[/] {files['bam'].name}   [dim]truth[/] {files['truth'].name}"
                                      + (f"   [dim]mutations[/] {files['mutations'].name}" if files['mutations'] else ""))
                else:
                    print(f"   Downloaded '{sid}' to {files['dir']}", flush=True)
                    print(f"     BAM:       {files['bam']}", flush=True)
                    print(f"     Truth:     {files['truth']}", flush=True)
                    print(f"     Mutations: {files['mutations'] if files['mutations'] else '(none)'}", flush=True)
            else:
                ok = _score_one(full, files, run_tool, flag_config)
                if not ok and single_shot:
                    return 1

        # --- Loop unless a flag forced a single-shot run ---
        if single_shot or flag_config:
            return 0
        try:
            if _UI.interactive and _UI.questionary:
                again = bool(await _UI.questionary.confirm("Do another?", default=False, style=_UI._qstyle).ask_async())
            else:
                again = input("\n   Do another? [y/N]: ").strip().lower() in ("y", "yes")
        except EOFError:
            return 0
        if not again:
            if _UI.console:
                _UI.console.print("\n   [dim]Done. Happy tuning![/]\n")
            return 0


def _parse_side_mode_args(argv):
    """Parse --score / --practice / --demo and their sub-args from raw argv.

    bittensor's bt.config() does NOT surface plain store_true flags like
    --practice/--score/--demo as top-level config attributes (and it has its
    OWN --config flag that collides with ours), so we must NOT rely on the
    bittensor Config for these. Parse them from sys.argv directly with a
    plain argparse that ignores everything else. Returns a namespace with
    .score/.practice/.demo and the sub-args (bam/truth/region/config/etc.).
    """
    p = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    p.add_argument("--score", action="store_true", default=False)
    p.add_argument("--practice", action="store_true", default=False)
    p.add_argument("--demo", action="store_true", default=False)
    p.add_argument("--bam", type=str, default=None)
    p.add_argument("--truth", type=str, default=None)
    p.add_argument("--mutations", type=str, default=None)
    p.add_argument("--region", type=str, default=None)
    p.add_argument("--config", type=str, default=None)
    p.add_argument("--reference", type=str, default=None)
    p.add_argument("--confident_bed", type=str, default=None)
    p.add_argument("--reference_sdf", type=str, default=None)
    p.add_argument("--sample_id", type=str, default=None)
    p.add_argument("--variant_caller", type=str, default=None)
    ns, _unknown = p.parse_known_args(argv)
    # --config was lifted out of argv before bittensor was imported; see the
    # note at the top of this module.
    if ns.config is None and _SIDE_MODE_CONFIG is not None:
        ns.config = _SIDE_MODE_CONFIG
    return ns


# Default sample --demo scores against. Env-overridable so ops can rotate it
# without a code change. A one-shot onboarding run: --demo is just the practice
# self-scorer pinned to this single fixed sample (no picker).
DEMO_SAMPLE_ID = os.getenv("DEMO_SAMPLE_ID", "d7b99f4a-061c-4202-8b12-2c1425bd4974")


def main():
    """Main entry point."""
    # Detect side-modes from raw argv BEFORE bittensor's config layer (which
    # drops these custom flags). --score/--practice/--demo bypass the full miner.
    side = _parse_side_mode_args(sys.argv[1:])
    if side.score:
        sys.exit(run_offline_score(side))
    if side.demo:
        # --demo == the practice self-scorer pinned to one fixed sample: no
        # chain/wallet, download BAM + truth, run the config, print the exact
        # score a validator would compute. Pin the sample and default the
        # config so a brand-new operator gets a score in one command.
        side.practice = True
        if not getattr(side, "sample_id", None):
            side.sample_id = DEMO_SAMPLE_ID
        if not getattr(side, "config", None):
            tool = side.variant_caller or os.getenv("MINER_TEMPLATE") or "gatk"
            side.config = f"configs/{tool}.conf"
    if side.practice:
        # Ephemeral keypair + /v2/practice/*; no chain/wallet/metagraph.
        sys.exit(asyncio.run(run_practice(side)))

    cfg = Miner.get_config()
    miner = Miner(config=cfg)
    miner.run()


if __name__ == "__main__":
    main()
