"""Minos Miner - Round-based variant calling submission."""

import sys
import os
import gzip
import json
import shutil
import traceback
import threading
from pathlib import Path

# Add parent directory to path so we can import base and utils
sys.path.insert(0, str(Path(__file__).parent.parent))

import time
from typing import Dict, Any
import asyncio
import bittensor as bt
import argparse
import subprocess
from dotenv import load_dotenv

# Bittensor v9/v10 compatibility — v10 removed lowercase aliases
if not hasattr(bt, "subtensor"):
    bt.subtensor = bt.Subtensor
if not hasattr(bt, "wallet"):
    bt.wallet = bt.Wallet
if not hasattr(bt, "config"):
    bt.config = bt.Config

from base import GENOMICS_CONFIG, MINER_CONFIG, is_docker_available, BASE_DIR
from utils.file_utils import download_file_verified, download_file_with_fallback
from utils.platform_client import MinerPlatformClient, PlatformConfig, PlatformClientError
from utils.config_loader import extract_tool_options, get_tool_version

# Template system for pluggable variant callers
from templates import (
    get_template_path,
    load_template,
)
from templates.tool_params import validate_round_id

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)
load_dotenv()

# Round timing constants
MIN_SUBMISSION_TIME_SECONDS = 600
POLL_INTERVAL_SECONDS = 30
MIN_VCF_SIZE_BYTES = 100


class Miner:
    """Minos miner - round-based variant calling."""

    def __init__(self, config=None):
        self.config = config or self.get_config()

        bt.logging.info("Setting up miner...")
        bt.logging.set_trace(self.config.logging.trace)
        bt.logging.set_debug(self.config.logging.debug)

        self.wallet = bt.wallet(config=self.config)
        bt.logging.info(f"Wallet loaded: {self.wallet.hotkey.ss58_address}")

        bt.logging.info(f"Connecting to network: {self.config.subtensor.network}")
        self.subtensor = bt.subtensor(config=self.config)

        bt.logging.info(f"Loading metagraph for netuid: {self.config.netuid}")
        self.metagraph = self.subtensor.metagraph(self.config.netuid)
        bt.logging.info(f"Metagraph loaded: {len(self.metagraph.hotkeys)} neurons")

        self.is_registered = self.wallet.hotkey.ss58_address in self.metagraph.hotkeys
        if self.is_registered:
            self.my_subnet_uid = self.metagraph.hotkeys.index(self.wallet.hotkey.ss58_address)
            bt.logging.info(f"Miner registered with UID: {self.my_subnet_uid}")
        else:
            self.my_subnet_uid = None
            bt.logging.warning(
                "Miner not registered on subnet 107. "
                "Register with: btcli subnets register --netuid 107 --wallet.name miner --wallet.hotkey default"
            )
            bt.logging.info("Continuing in demo/unregistered mode — variant calling will still run")

        self.setup_variant_caller()
        self.setup_platform_client()

        # Round tracking
        self.submitted_rounds: set = set()

        bt.logging.info(f"Miner ready - template: {self.variant_caller}, docker: {is_docker_available()}")

    def _register_with_retry(self, max_retries: int = 3) -> bool:
        """Register on subnet with retry for 'Transaction Already Imported' errors."""
        for attempt in range(max_retries):
            try:
                result = self.subtensor.register(
                    wallet=self.wallet,
                    netuid=self.config.netuid,
                    wait_for_finalization=True,
                    wait_for_inclusion=True,
                )
                # bt v10 returns ExtrinsicResponse (always truthy), v9 returns bool
                return result.success if hasattr(result, "success") else bool(result)
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
                keypair=self.wallet.hotkey,
                config=config
            )
            bt.logging.info(f"Platform client initialized: {platform_url}")
        except Exception as e:
            bt.logging.error(f"Failed to initialize platform client: {e}")
            sys.exit(1)

    def setup_variant_caller(self):
        """Setup variant caller from MINER_TEMPLATE env var. Docker is required."""
        if not is_docker_available():
            bt.logging.error("Docker is required but not available. Please install Docker and try again.")
            sys.exit(1)

        self.variant_caller = os.getenv("MINER_TEMPLATE", "").lower() or MINER_CONFIG.get("default_caller", "gatk")

        # Validate template exists
        try:
            get_template_path(self.variant_caller)
        except (ValueError, FileNotFoundError):
            bt.logging.error(f"Invalid template '{self.variant_caller}'. Available: gatk, deepvariant, freebayes, bcftools")
            sys.exit(1)

    @staticmethod
    def get_config():
        """Get configuration from argparse and environment."""
        parser = argparse.ArgumentParser(description="Minos Miner", allow_abbrev=False)

        parser.add_argument("--netuid", type=int, default=int(os.getenv("NETUID", 107)), help="Subnet UID")
        parser.add_argument(
            "--variant_caller",
            type=str,
            choices=["gatk", "deepvariant", "freebayes", "bcftools"],
            default=MINER_CONFIG.get("default_caller", "gatk"),
            help="Variant calling template",
        )

        bt.subtensor.add_args(parser)
        bt.logging.add_args(parser)
        bt.wallet.add_args(parser)

        config = bt.config(parser)

        # Env overrides
        if os.getenv("NETWORK"):
            config.subtensor.network = os.getenv("NETWORK")
        if os.getenv("NETUID"):
            config.netuid = int(os.getenv("NETUID"))
        if os.getenv("WALLET_NAME"):
            config.wallet.name = os.getenv("WALLET_NAME")
        if os.getenv("WALLET_HOTKEY"):
            config.wallet.hotkey = os.getenv("WALLET_HOTKEY")

        return config

    async def execute_template(self, bam_path: Path, region: str, config: Dict[str, Any] = None) -> tuple:
        """Execute selected template for variant calling. Returns (vcf_content, vcf_path, variant_count)."""
        output_dir = bam_path.parent
        output_vcf = output_dir / "output.vcf.gz"

        # Extract chromosome from region (e.g. "chr16:10000000-15000000" -> "chr16")
        chrom = region.split(":")[0] if region else "chr20"
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
            if round_data.get("has_submitted", False):
                bt.logging.info(f"Already submitted to round {round_id[:8]}... (platform confirmed)")
                self.submitted_rounds.add(round_id)
                return False

            # Check if enough time remaining (need at least 10 minutes for variant calling)
            if time_remaining < MIN_SUBMISSION_TIME_SECONDS:
                bt.logging.warning(f"Only {time_remaining}s remaining in round - skipping")
                return False

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
            # In demo mode, always re-run variant calling so users can test their tools
            is_demo = round_id.startswith("2026-01-01T00:00:00")
            variant_count, elapsed = await self._run_variant_calling(bam_path, region, tool_config, output_dir, force_rerun=is_demo)

            # Submit config to platform
            return await self._submit_result(round_id, tool_config, variant_count, elapsed)

        except PlatformClientError as e:
            bt.logging.warning(f"Round error: {e}")
            # In demo mode, submission is blocked — don't retry the same round
            if "demo mode" in str(e).lower():
                self.submitted_rounds.add(round_id)
                print("   Demo mode — variant calling complete. Submission disabled.", flush=True)
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
        if bam_index_url or bam_index_url_backup:
            print(f"   Downloading BAM index...", flush=True)
            index_downloaded = download_file_with_fallback(
                bam_index_url, bam_index, backup_url=bam_index_url_backup, show_progress=False
            ) if bam_index_url else download_file_verified(bam_index_url_backup, bam_index, show_progress=False)
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

    async def _submit_result(self, round_id, tool_config, variant_count, elapsed):
        """Submit variant calling config to the platform and handle the response."""
        # Submit config to platform
        print(f"   Submitting config to platform...", flush=True)
        result = await self.platform_client.submit_config(
            round_id=round_id,
            tool_name=self.variant_caller,
            tool_config=tool_config,
            variant_count=variant_count,
            runtime_seconds=elapsed
        )

        if result.get("success"):
            self.submitted_rounds.add(round_id)
            submission_id = result.get("submission_id", "unknown")
            print(f"   Config submitted successfully", flush=True)
            print(f"   Submission ID: {submission_id[:16]}...", flush=True)
            bt.logging.info(f"Round {round_id[:8]}... submitted: {variant_count} variants")

            # Cleanup old rounds from tracking (keep last 10)
            if len(self.submitted_rounds) > 10:
                self.submitted_rounds = set(list(self.submitted_rounds)[-10:])

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
            elif self.variant_caller == "freebayes":
                base_config["freebayes_options"] = tool_options
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
            elif self.variant_caller == "freebayes":
                base_config["freebayes_options"] = {"min_mapping_quality": 1}
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
        print(f"MINOS MINER", flush=True)
        print(f"{'='*60}", flush=True)
        print(f"   Hotkey: {self.wallet.hotkey.ss58_address[:16]}...", flush=True)
        print(f"   UID: {self.my_subnet_uid}", flush=True)
        print(f"   Network: {self.config.subtensor.network}", flush=True)
        print(f"   Netuid: {self.config.netuid}", flush=True)
        print(f"   Variant Caller: {self.variant_caller}", flush=True)
        print(f"   Docker: {'Available' if is_docker_available() else 'Not Available'}", flush=True)
        print(f"   Platform: {os.getenv('PLATFORM_URL')}", flush=True)
        print(f"{'='*60}", flush=True)

        # Test platform connectivity
        print(f"\n   Testing platform connection...", flush=True)
        try:
            if await self.platform_client.health_check():
                print(f"   Platform connection: OK", flush=True)
            else:
                print(f"   Platform connection: FAILED", flush=True)
                bt.logging.error("Cannot connect to platform")
                return
        except Exception as e:
            print(f"   Platform connection: ERROR - {e}", flush=True)
            bt.logging.error(f"Platform connection error: {e}")
            return

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

                # Sync metagraph every 2 minutes
                if sync_count % 4 == 0:
                    self.metagraph.sync(subtensor=self.subtensor)

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


def main():
    """Main entry point."""
    miner = Miner()
    miner.run()


if __name__ == "__main__":
    main()
