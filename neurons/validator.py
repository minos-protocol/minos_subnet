"""Minos Validator - scores miner VCF results with hap.py via platform rounds."""

import sys
import os
import gzip
import shutil
import traceback
import subprocess
from pathlib import Path

# Add parent directory to path so we can import base and utils
sys.path.insert(0, str(Path(__file__).parent.parent))

import time
import bittensor as bt
import argparse
import numpy as np
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Bittensor v9/v10 compatibility — v10 removed lowercase aliases
if not hasattr(bt, "subtensor"):
    bt.subtensor = bt.Subtensor
if not hasattr(bt, "wallet"):
    bt.wallet = bt.Wallet
if not hasattr(bt, "config"):
    bt.config = bt.Config

from base import GENOMICS_CONFIG, VALIDATOR_CONFIG, is_docker_available, BASE_DIR
from utils import (
    HappyScorer,
    ScoreTracker,
    AdvancedScorer,
)
from utils.scoring import parse_happy_vcf, BCFTOOLS_DOCKER_IMAGE
from utils.file_utils import compute_sha256
from utils.file_utils import download_file_with_fallback
from utils.path_utils import safe_round_dir_name
from utils.platform_client import ValidatorPlatformClient, PlatformConfig, PlatformClientError
from templates import load_template
from templates.tool_params import validate_round_id

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)
load_dotenv()

# Neurons with validator_permit are excluded from the miner weight list
# (only miners — those without permit — receive weight/emissions)

# Round timing constants
MAX_WAIT_SECONDS = 14400
MIN_WAIT_SECONDS = 60
BITTENSOR_BLOCK_TIME_SECONDS = 12
MAX_SLEEP_SECONDS = 120


class Validator:
    """Minos validator for genomics variant calling tasks."""

    def __init__(self, config=None):
        self.config = config or self.get_config()

        bt.logging.info("Setting up validator...")
        bt.logging.set_trace(self.config.logging.trace)
        bt.logging.set_debug(self.config.logging.debug)

        bt.logging.info(f"Loading wallet: {self.config.wallet.name}/{self.config.wallet.hotkey}")
        self.wallet = bt.wallet(config=self.config)
        bt.logging.info(f"Wallet loaded: {self.wallet.hotkey.ss58_address}")

        bt.logging.info(f"Connecting to network: {self.config.subtensor.network}")
        self.subtensor = bt.subtensor(config=self.config)
        bt.logging.info(f"Connected to {self.config.subtensor.network}")

        bt.logging.info(f"Loading metagraph for netuid: {self.config.netuid}")
        self.metagraph = self.subtensor.metagraph(self.config.netuid)
        bt.logging.info(f"Metagraph loaded: {len(self.metagraph.hotkeys)} neurons")

        self.is_registered = self.wallet.hotkey.ss58_address in self.metagraph.hotkeys
        if self.is_registered:
            self.my_subnet_uid = self.metagraph.hotkeys.index(
                self.wallet.hotkey.ss58_address
            )
            bt.logging.info(f"Validator registered with UID: {self.my_subnet_uid}")
        else:
            self.my_subnet_uid = None
            bt.logging.warning(
                "Validator not registered on subnet 107. "
                "Register with: btcli subnets register --netuid 107 "
                "--wallet.name validator --wallet.hotkey default"
            )
            bt.logging.info("Continuing in demo/unregistered mode — scoring will still run but weights cannot be set")

        self.score_tracker = ScoreTracker(
            alpha=GENOMICS_CONFIG["ema_alpha"],
        )
        bt.logging.info(f"Score tracker initialized (EMA alpha={GENOMICS_CONFIG['ema_alpha']}, "
                       f"min_rounds={self.score_tracker.min_rounds})")

        self.setup_genomics_components()
        self.setup_platform_client()

        self.use_platform = self.platform_client is not None

        bt.logging.info(f"Validator initialization complete")
        bt.logging.info(f"Network: {self.config.subtensor.network}, Netuid: {self.config.netuid}")
        bt.logging.info(f"Docker: {is_docker_available()}, Platform mode: {self.use_platform}")

    def setup_platform_client(self):
        """Setup platform client for task management and scoring."""
        platform_url = os.getenv("PLATFORM_URL", "")
        if not platform_url:
            # Standalone mode is a graceful fallback, not a production deployment path.
            # Without the platform there are no rounds to score — the validator will
            # only maintain its metagraph sync and weight-setting loop.
            bt.logging.info("PLATFORM_URL not set - running in standalone mode (no scoring)")
            self.platform_client = None
            return

        try:
            config = PlatformConfig(
                base_url=platform_url,
                timeout=float(os.getenv("PLATFORM_TIMEOUT", "60"))
            )
            self.platform_client = ValidatorPlatformClient(
                keypair=self.wallet.hotkey,
                config=config
            )
            bt.logging.info(f"Platform client initialized: {platform_url}")
        except Exception as e:
            bt.logging.warning(f"Failed to initialize platform client: {e}")
            self.platform_client = None

    def setup_genomics_components(self):
        """Initialize hap.py scorer."""
        self.happy_scorer = HappyScorer(
            docker_image=GENOMICS_CONFIG.get("happy_docker_image")
        )

        self.scored_rounds = set()  # Track rounds we've already scored

        self._cleanup_old_files()

    def _cleanup_old_files(self, max_age_hours: int = 5):
        """Remove mutated BAMs, truth VCFs, and scoring files older than max_age_hours."""
        cutoff_time = time.time() - (max_age_hours * 3600)
        # Directories with individual files to clean
        file_dirs = [
            BASE_DIR / "output" / "mutated_bams",
            BASE_DIR / "output" / "merged_truth",
            BASE_DIR / "output" / "scoring",
        ]

        total_cleaned = 0
        total_bytes = 0

        for cleanup_dir in file_dirs:
            if not cleanup_dir.exists():
                continue

            for file_path in cleanup_dir.iterdir():
                try:
                    if file_path.is_file() and file_path.stat().st_mtime < cutoff_time:
                        file_size = file_path.stat().st_size
                        file_path.unlink()
                        total_cleaned += 1
                        total_bytes += file_size
                except Exception as e:
                    bt.logging.debug(f"Cleanup failed for {file_path}: {e}")

        # scoring contains round directories — clean entire dirs
        scoring_dir = BASE_DIR / "output" / "scoring"
        if scoring_dir.exists():
            for round_dir in scoring_dir.iterdir():
                try:
                    if round_dir.is_dir() and round_dir.stat().st_mtime < cutoff_time:
                        dir_size = sum(f.stat().st_size for f in round_dir.rglob('*') if f.is_file())
                        shutil.rmtree(round_dir)
                        total_cleaned += 1
                        total_bytes += dir_size
                except Exception as e:
                    bt.logging.debug(f"Cleanup failed for {round_dir}: {e}")

        if total_cleaned > 0:
            bt.logging.info(f"Cleaned {total_cleaned} items ({total_bytes / (1024**3):.2f} GB)")

    def _calculate_wait_until_scoring(self, next_scoring_window: str) -> int:
        """Calculate seconds to wait until the next scoring window.

        Args:
            next_scoring_window: ISO datetime string of next scoring window start

        Returns:
            Seconds to wait (minimum 60 seconds)
        """
        try:
            # Parse the ISO datetime string
            if next_scoring_window.endswith('Z'):
                next_scoring_window = next_scoring_window[:-1] + '+00:00'
            next_scoring_dt = datetime.fromisoformat(next_scoring_window)

            # Calculate seconds until that time
            now = datetime.now(next_scoring_dt.tzinfo)
            delta = (next_scoring_dt - now).total_seconds()

            # Return at least MIN_WAIT_SECONDS, at most MAX_WAIT_SECONDS
            return max(MIN_WAIT_SECONDS, min(int(delta), MAX_WAIT_SECONDS))
        except Exception as e:
            bt.logging.warning(f"Failed to parse next_scoring_window '{next_scoring_window}': {e}")
            # Fall back to default interval
            return GENOMICS_CONFIG["task_interval"]

    @staticmethod
    def get_config():
        """Get configuration from argparse and environment."""
        parser = argparse.ArgumentParser(
            description="Minos Validator",
            allow_abbrev=False,
        )

        parser.add_argument(
            "--netuid",
            type=int,
            default=int(os.getenv("NETUID", 107)),
            help="Subnet UID to validate on"
        )

        bt.subtensor.add_args(parser)
        bt.logging.add_args(parser)
        bt.wallet.add_args(parser)

        config = bt.config(parser)
        env_network = os.getenv("NETWORK")
        if env_network:
            config.subtensor.network = env_network

        env_netuid = os.getenv("NETUID")
        if env_netuid:
            try:
                config.netuid = int(env_netuid)
            except ValueError:
                bt.logging.warning(f"Invalid NETUID env var: {env_netuid}")

        env_wallet_name = os.getenv("WALLET_NAME")
        if env_wallet_name:
            config.wallet.name = env_wallet_name

        env_wallet_hotkey = os.getenv("WALLET_HOTKEY")
        if env_wallet_hotkey:
            config.wallet.hotkey = env_wallet_hotkey

        return config

    async def score_platform_rounds(self) -> dict:
        """Round-based scoring: fetch scoring rounds from platform, run miner configs, submit scores.

        Flow:
        1. Poll platform for rounds in "scoring" phase
        2. For each scoring round, get all miner submissions (configs + presigned URLs)
        3. Download BAM and truth VCF from presigned URLs
        4. For each miner submission: run their tool_config via templates, generate VCF
        5. Score VCF with HappyScorer against truth VCF
        6. Submit scores to platform via submit_score()

        Returns:
            dict with:
                - next_scoring_window_start: Optional[str] - ISO datetime for smart scheduling
        """
        if not self.platform_client:
            bt.logging.warning("Platform client not initialized - cannot run scoring")
            return {"next_scoring_window_start": None}

        try:
            # 1. Get rounds in scoring phase
            response = await self.platform_client.get_scoring_rounds()
            scoring_rounds = response.get("scoring_rounds", [])
            next_scoring_start = response.get("next_scoring_window_start")

            if not scoring_rounds:
                bt.logging.debug("No rounds in scoring phase")
                return {"next_scoring_window_start": next_scoring_start}

            print(f"\n{'='*60}", flush=True)
            print(f"   ROUND-BASED SCORING", flush=True)
            print(f"   Scoring rounds found: {len(scoring_rounds)}", flush=True)
            print(f"{'='*60}", flush=True)

            for round_info in scoring_rounds:
                round_id = round_info.get("round_id")
                if not round_id:
                    bt.logging.warning(f"Skipping scoring round with missing round_id: {round_info}")
                    continue
                submission_count = round_info.get("submission_count", 0)

                # Skip rounds we've already scored in this session
                if round_id in self.scored_rounds:
                    bt.logging.debug(f"Round {round_id}: already scored this session, skipping")
                    continue

                if submission_count == 0:
                    bt.logging.info(f"Round {round_id}: no submissions to score")
                    self.scored_rounds.add(round_id)  # Mark as done even if no submissions
                    continue

                print(f"\n   Scoring round: {round_id}", flush=True)
                print(f"   Region: {round_info.get('region', 'unknown')}", flush=True)
                print(f"   Submissions: {submission_count}", flush=True)

                await self._score_round_submissions(round_id)

                # Mark round as scored after processing all submissions
                self.scored_rounds.add(round_id)
                bt.logging.info(f"Round {round_id}: marked as scored")

            return {"next_scoring_window_start": next_scoring_start}

        except PlatformClientError as e:
            bt.logging.error(f"Scoring error: {e}")
            print(f"   Scoring error: {e}", flush=True)
            return {"next_scoring_window_start": None}
        except Exception as e:
            bt.logging.error(f"Unexpected error in scoring: {e}")
            bt.logging.debug(traceback.format_exc())
            return {"next_scoring_window_start": None}

    async def _score_round_submissions(self, round_id: str):
        """Score all submissions for a single round."""
        rid_check = validate_round_id(round_id)
        if not rid_check["valid"]:
            bt.logging.error(f"_score_round_submissions: invalid round_id '{round_id}': {rid_check['error']}")
            return

        try:
            # 2. Get all submissions for this round (includes presigned URLs)
            round_data = await self.platform_client.get_round_submissions(round_id)

            submissions = round_data.get("submissions", [])
            region = round_data.get("region", "")

            if not submissions:
                bt.logging.info(f"Round {round_id}: no submissions")
                return

            # 3. Download BAM and truth VCF, verify reference exists
            download_result = self._download_round_files(round_id, round_data)
            if download_result is None:
                return

            work_dir = download_result["work_dir"]
            bam_path = download_result["bam_path"]
            truth_vcf_path = download_result["truth_vcf_path"]
            ref_path = download_result["ref_path"]
            ref_sdf_path = download_result["ref_sdf_path"]
            truth_bed_path = download_result["truth_bed_path"]

            # 4. For each submission, run their tool config and score
            scored_hotkeys = []  # Track which miners were scored this round
            submission_times = {}  # hotkey -> submitted_at epoch for tiebreaking

            # Restart recovery: get miners already scored by this validator
            already_scored = round_data.get("scored_miners") or {}
            if already_scored:
                print(f"   Restart recovery: {len(already_scored)} miners already scored, skipping", flush=True)
                for hotkey, score_info in already_scored.items():
                    combined_final = score_info.get("combined_final", 0.0)
                    self.score_tracker.update(hotkey, combined_final if combined_final is not None else 0.0)
                    scored_hotkeys.append(hotkey)
                    bt.logging.info(f"Restored score for {hotkey[:16]}...: combined_final={combined_final}")

            for sub in submissions:
                await self._score_single_miner(
                    round_id, sub, already_scored, work_dir, bam_path,
                    ref_path, ref_sdf_path, truth_bed_path, truth_vcf_path,
                    region, scored_hotkeys, submission_times,
                )

            # 9-10. Record round, set weights, cleanup
            await self._finalize_round_scores(round_id, scored_hotkeys, submission_times)

        except Exception as e:
            bt.logging.error(f"Error scoring round {round_id}: {e}")
            bt.logging.debug(traceback.format_exc())

    def _download_round_files(self, round_id, round_data):
        """Download BAM + truth VCF and verify reference files; return paths dict or None on failure."""
        # Resolve primary/backup URL order based on operator preference
        _prefer_hippius = os.getenv("STORAGE_PRIMARY_BACKEND", "hippius").lower() != "aws_s3"

        def _ordered(s3_key: str, hip_key: str):
            s3 = round_data.get(s3_key)
            hip = round_data.get(hip_key)
            return (hip, s3) if _prefer_hippius else (s3, hip)

        bam_url, bam_url_backup = _ordered("bam_presigned_url", "bam_presigned_url_backup")
        bam_index_url, bam_index_url_backup = _ordered("bam_index_presigned_url", "bam_index_presigned_url_backup")
        truth_vcf_url, truth_vcf_url_backup = _ordered("truth_vcf_presigned_url", "truth_vcf_presigned_url_backup")
        truth_vcf_index_url, truth_vcf_index_url_backup = _ordered("truth_vcf_index_presigned_url", "truth_vcf_index_presigned_url_backup")

        if not (bam_url or bam_url_backup) or not (truth_vcf_url or truth_vcf_url_backup):
            bt.logging.error(f"Round {round_id}: missing presigned URLs")
            return None

        # 3. Download BAM and truth VCF to local temp directory
        work_dir = BASE_DIR / "output" / "scoring" / safe_round_dir_name(round_id)
        work_dir.mkdir(parents=True, exist_ok=True)

        bam_path = work_dir / "round.bam"
        bam_index_path = work_dir / "round.bam.bai"
        truth_vcf_path = work_dir / "truth.vcf.gz"

        # Use SHA256-verified downloads (skips download if valid cached file exists)
        bam_sha256 = round_data.get("bam_sha256")
        truth_vcf_sha256 = round_data.get("truth_vcf_sha256")

        print(f"   Downloading BAM...", flush=True)
        if not download_file_with_fallback(bam_url, bam_path, backup_url=bam_url_backup, expected_sha256=bam_sha256, show_progress=True):
            bt.logging.error(f"Round {round_id}: failed to download BAM (primary and backup)")
            return None

        # Always clear old index to prevent stale index with re-downloaded BAM
        if bam_index_path.exists():
            bam_index_path.unlink()
        if bam_index_url or bam_index_url_backup:
            print(f"   Downloading BAM index...", flush=True)
            if not download_file_with_fallback(bam_index_url, bam_index_path, backup_url=bam_index_url_backup, show_progress=False):
                bt.logging.warning(f"Round {round_id}: failed to download BAM index (variant calling may still work)")
                # Don't fail - some tools can work without index, and samtools can regenerate it

        print(f"   Downloading truth VCF...", flush=True)
        if not download_file_with_fallback(truth_vcf_url, truth_vcf_path, backup_url=truth_vcf_url_backup, expected_sha256=truth_vcf_sha256):
            bt.logging.error(f"Round {round_id}: failed to download truth VCF (primary and backup)")
            return None

        # Download or create truth VCF index (required for bcftools region slicing)
        truth_vcf_index = work_dir / "truth.vcf.gz.tbi"
        if truth_vcf_index_url or truth_vcf_index_url_backup:
            print(f"   Downloading truth VCF index...", flush=True)
            if not download_file_with_fallback(truth_vcf_index_url, truth_vcf_index, backup_url=truth_vcf_index_url_backup):
                bt.logging.warning(f"Round {round_id}: failed to download truth VCF index, will re-create locally")

        if not truth_vcf_index.exists():
            print(f"   Indexing truth VCF...", flush=True)
            try:
                index_cmd = [
                    "docker", "run", "--rm",
                    "-v", f"{work_dir}:/data",
                    BCFTOOLS_DOCKER_IMAGE,
                    "bcftools", "index", "-t", "/data/truth.vcf.gz",
                ]
                result = subprocess.run(index_cmd, capture_output=True, text=True, timeout=120)
                if result.returncode != 0:
                    bt.logging.warning(f"Truth VCF indexing failed: {result.stderr}")
                else:
                    print(f"   Truth VCF indexed successfully", flush=True)
            except Exception as e:
                bt.logging.warning(f"Failed to index truth VCF: {e}")

        print(f"   BAM: {bam_path.stat().st_size / (1024**3):.2f} GB", flush=True)
        print(f"   Processing {len(round_data.get('submissions', []))} submissions...", flush=True)

        # Reference path (local dataset)
        ref_path = BASE_DIR / "datasets" / "reference" / "chr20.fa"
        ref_sdf_path = BASE_DIR / "datasets" / "reference" / "chr20.sdf"
        truth_bed_path = BASE_DIR / "datasets" / "truth" / "sample_GRCh38_1_22_v4.2.1_benchmark_noinconsistent.chr20.bed"

        if not ref_path.exists():
            bt.logging.error(f"Reference not found: {ref_path}")
            return None

        # The platform uploads a merged truth VCF (GIAB + synthetic) to S3.
        # Use it directly — no need to re-parse or re-merge.
        # Verify it has variants before proceeding.
        truth_variant_count = 0
        try:
            opener = gzip.open if str(truth_vcf_path).endswith('.gz') else open
            with opener(truth_vcf_path, 'rt') as f:
                for line in f:
                    if not line.startswith('#'):
                        truth_variant_count += 1
        except Exception as e:
            bt.logging.error(f"Round {round_id}: cannot read truth VCF: {e}")
            return None

        if truth_variant_count == 0:
            bt.logging.error(f"Round {round_id}: truth VCF has 0 variants — cannot score")
            return None

        print(f"   Truth VCF: {truth_variant_count} variants", flush=True)

        return {
            "work_dir": work_dir,
            "bam_path": bam_path,
            "truth_vcf_path": truth_vcf_path,
            "ref_path": ref_path,
            "ref_sdf_path": ref_sdf_path,
            "truth_bed_path": truth_bed_path,
        }

    async def _score_single_miner(self, round_id, sub, already_scored, work_dir,
                                   bam_path, ref_path, ref_sdf_path, truth_bed_path,
                                   truth_vcf_path, region, scored_hotkeys, submission_times):
        """Run a single miner's tool, score the output, and submit results."""
        miner_hotkey = sub.get("miner_hotkey")
        tool_name = sub.get("tool_name")
        if not miner_hotkey or not tool_name:
            bt.logging.warning(f"Skipping submission with missing miner_hotkey or tool_name: {sub}")
            return
        tool_config = sub.get("tool_config", {})

        # Skip miners already scored (restart recovery)
        if miner_hotkey in already_scored:
            bt.logging.debug(f"Skipping {miner_hotkey[:16]}... (already scored)")
            # Capture submission time for tiebreaking even for restored miners
            if sub.get("submitted_at"):
                try:
                    submission_times[miner_hotkey] = datetime.fromisoformat(sub["submitted_at"]).timestamp()
                except (ValueError, TypeError):
                    pass
            return

        # Capture submission timestamp for tiebreaking
        if sub.get("submitted_at"):
            try:
                submitted_at = sub["submitted_at"]
                # Handle ISO format with timezone (e.g. "2026-02-15T12:00:00+00:00")
                submission_times[miner_hotkey] = datetime.fromisoformat(submitted_at).timestamp()
            except (ValueError, TypeError):
                pass

        print(f"\n   --- Miner {miner_hotkey[:16]}... ({tool_name}) ---", flush=True)

        try:
            # Run variant calling with miner's config
            # Use full hotkey to avoid collisions between similar hotkeys
            miner_vcf_path = work_dir / f"miner_{miner_hotkey}.vcf.gz"

            scoring_start = time.time()
            result = await self._run_miner_tool(
                tool_name=tool_name,
                tool_config=tool_config,
                bam_path=bam_path,
                ref_path=ref_path,
                output_vcf_path=miner_vcf_path,
                region=region
            )

            if not result.get("success"):
                bt.logging.warning(f"Miner {miner_hotkey[:16]}: tool failed - {result.get('error')}")
                print(f"   Tool failed: {result.get('error', 'Unknown error')[:100]}", flush=True)
                # Submit zero score for failed runs
                await self._submit_miner_score(round_id, miner_hotkey, None, 0)
                # Still counts as participation (they submitted, tool just failed)
                self.score_tracker.update(miner_hotkey, 0.0)
                scored_hotkeys.append(miner_hotkey)
                return

            variant_count = result.get("variant_count", 0)
            print(f"   Variants called: {variant_count}", flush=True)

            # 5. Score with hap.py
            metrics = self.happy_scorer.score_vcf(
                truth_vcf=str(truth_vcf_path),
                query_vcf=str(miner_vcf_path),
                reference_fasta=str(ref_path),
                confident_bed=str(truth_bed_path) if truth_bed_path.exists() else None,
                region=region,
                reference_sdf=str(ref_sdf_path) if ref_sdf_path.exists() else None
            )

            scoring_elapsed = time.time() - scoring_start

            # 6. Upload VCF artifacts to S3 (audit trail)
            output_vcf_s3_key = None
            output_vcf_sha256_val = None
            happy_output_s3_key = None
            original_query_stem = miner_vcf_path.stem
            happy_vcf_path = work_dir / f"happy_{original_query_stem}.vcf.gz"

            try:
                vcf_s3_prefix = f"scoring/{safe_round_dir_name(round_id)}"

                # Upload miner output VCF
                if miner_vcf_path.exists():
                    output_vcf_sha256_val = compute_sha256(miner_vcf_path)
                    output_vcf_s3_key = f"{vcf_s3_prefix}/{miner_hotkey}.vcf.gz"
                    if not await self.platform_client.upload_file_to_s3(
                        str(miner_vcf_path), output_vcf_s3_key
                    ):
                        bt.logging.warning(f"Failed to upload miner VCF for {miner_hotkey[:16]}")
                        output_vcf_s3_key = None

                # Upload hap.py annotated VCF (contains BD/BVT tags)
                if happy_vcf_path.exists():
                    happy_output_s3_key = f"{vcf_s3_prefix}/happy_{miner_hotkey}.vcf.gz"
                    if not await self.platform_client.upload_file_to_s3(
                        str(happy_vcf_path), happy_output_s3_key
                    ):
                        bt.logging.warning(f"Failed to upload hap.py VCF for {miner_hotkey[:16]}")
                        happy_output_s3_key = None

                if output_vcf_s3_key:
                    print(f"   VCFs uploaded to S3 for audit trail", flush=True)
            except Exception as e:
                bt.logging.warning(f"VCF upload failed for {miner_hotkey[:16]}: {e}")

            # 7. Submit score to platform (with VCF S3 keys)
            score_result = await self._submit_miner_score(
                round_id, miner_hotkey, metrics, scoring_elapsed,
                output_vcf_s3_key=output_vcf_s3_key,
                output_vcf_sha256=output_vcf_sha256_val,
                happy_output_s3_key=happy_output_s3_key,
            )

            # 8. Parse and submit variant-level results (non-blocking)
            try:
                if happy_vcf_path.exists() and score_result:
                    score_id = score_result.get("score_id")
                    if score_id:
                        variant_results = parse_happy_vcf(str(happy_vcf_path), truth_vcf_path=str(truth_vcf_path))
                        if variant_results:
                            await self.platform_client.submit_variant_results(
                                score_id=score_id,
                                results=variant_results
                            )
                            print(f"   Submitted {len(variant_results)} variant-level results", flush=True)
            except Exception as e:
                bt.logging.warning(f"Variant results submission failed for {miner_hotkey[:16]}: {e}")

            # Log results and update EMA
            print(f"   SNP F1={metrics.get('f1_snp', 0):.4f}  INDEL F1={metrics.get('f1_indel', 0):.4f}", flush=True)
            advanced_score = AdvancedScorer.compute_advanced_score(metrics)
            combined_final = advanced_score / 100.0
            ema = self.score_tracker.update(miner_hotkey, combined_final)
            scored_hotkeys.append(miner_hotkey)
            print(f"   Score: {advanced_score:.2f}/100  EMA: {ema:.4f}", flush=True)

        except Exception as e:
            bt.logging.error(f"Error scoring miner {miner_hotkey[:16]}: {e}")
            print(f"   Error: {str(e)[:100]}", flush=True)
            # Submit zero score on error
            await self._submit_miner_score(round_id, miner_hotkey, None, 0)
            self.score_tracker.update(miner_hotkey, 0.0)
            scored_hotkeys.append(miner_hotkey)

    async def _finalize_round_scores(self, round_id, scored_hotkeys, submission_times):
        """Record round participation, set weights, and log completion."""
        # 9. Record round participation for eligibility tracking
        if scored_hotkeys:
            self.score_tracker.record_round(round_id, scored_hotkeys)
            bt.logging.info(f"Round {round_id}: recorded participation for {len(scored_hotkeys)} miners")

        # 10. Compute winner-takes-all weights and set on chain
        await self._set_weights_after_round(round_id, submission_times)

        print(f"\n   Round {round_id} scoring complete", flush=True)

    async def _run_miner_tool(
        self,
        tool_name: str,
        tool_config: dict,
        bam_path: Path,
        ref_path: Path,
        output_vcf_path: Path,
        region: str
    ) -> dict:
        """Run a miner's variant calling tool via templates."""
        try:
            # Load the template for this tool
            template = load_template(tool_name)

            # Merge miner's config with validator's infrastructure settings
            # SECURITY: Whitelist only the keys templates actually use
            # Reject everything else — prevents unknown param injection
            ALLOWED_CONFIG_KEYS = {
                "tool", "version",
                "gatk_options", "deepvariant_options",
                "freebayes_options", "bcftools_options",
            }
            sanitized_config = {k: v for k, v in tool_config.items()
                                if k in ALLOWED_CONFIG_KEYS}

            # Scoring resource profile — configurable via env vars.
            # Validators can tune these to match their hardware.
            # Thread/memory counts affect speed, not variant-calling output.
            SCORING_THREADS = int(os.getenv("SCORING_THREADS", "4"))
            SCORING_MEMORY_GB = int(os.getenv("SCORING_MEMORY_GB", "8"))

            config = {
                **sanitized_config,  # Miner's quality params FIRST
                "timeout": GENOMICS_CONFIG.get("variant_calling_timeout", 1800),
                "threads": SCORING_THREADS,
                "memory_gb": SCORING_MEMORY_GB,
                "ref_build": "GRCh38",  # Standard reference build
            }

            # Run variant calling in thread pool to avoid blocking
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: template.variant_call(
                    bam_path=bam_path,
                    reference_path=ref_path,
                    output_vcf_path=output_vcf_path,
                    region=region,
                    config=config
                )
            )

            return result

        except ValueError as e:
            return {"success": False, "variant_count": 0, "error": f"Unknown tool: {tool_name}"}
        except Exception as e:
            return {"success": False, "variant_count": 0, "error": str(e)}

    async def _submit_miner_score(
        self,
        round_id: str,
        miner_hotkey: str,
        metrics: dict,
        validation_runtime: float,
        output_vcf_s3_key: str = None,
        output_vcf_sha256: str = None,
        happy_output_s3_key: str = None,
    ):
        """Submit scoring results to platform.

        Uses AdvancedScorer (whitepaper formula) so platform dashboard scores
        match on-chain weights.

        Returns:
            Dict with score_id on success, None on failure.
        """
        try:
            if metrics is None:
                # Failed run - submit zeros
                result = await self.platform_client.submit_score(
                    round_id=round_id,
                    miner_hotkey=miner_hotkey,
                    snp_f1=0.0,
                    indel_f1=0.0,
                    validation_runtime_seconds=validation_runtime
                )
            else:
                advanced_score = AdvancedScorer.compute_advanced_score(metrics)
                combined_final = advanced_score / 100.0
                snp_final = metrics.get("f1_snp", 0.0)
                indel_final = metrics.get("f1_indel", 0.0)

                bt.logging.info(f"Platform score (Advanced): combined_final={combined_final:.4f}, "
                               f"snp_final={snp_final:.4f}, indel_final={indel_final:.4f}")

                result = await self.platform_client.submit_score(
                    round_id=round_id,
                    miner_hotkey=miner_hotkey,
                    snp_f1=metrics.get("f1_snp"),
                    snp_precision=metrics.get("precision_snp"),
                    snp_recall=metrics.get("recall_snp"),
                    snp_tp=metrics.get("tp_snp"),
                    snp_fp=metrics.get("fp_snp"),
                    snp_fn=metrics.get("fn_snp"),
                    indel_f1=metrics.get("f1_indel"),
                    indel_precision=metrics.get("precision_indel"),
                    indel_recall=metrics.get("recall_indel"),
                    indel_tp=metrics.get("tp_indel"),
                    indel_fp=metrics.get("fp_indel"),
                    indel_fn=metrics.get("fn_indel"),
                    ti_tv_ratio=metrics.get("titv_query_snp"),
                    het_hom_ratio=metrics.get("hethom_query_snp"),
                    additional_metrics={
                        "scorer": "Advanced",
                        "weighted_f1": metrics.get("weighted_f1"),
                        "frac_na_snp": metrics.get("frac_na_snp"),
                        "frac_na_indel": metrics.get("frac_na_indel"),
                        "snp_final": snp_final,
                        "indel_final": indel_final,
                        "combined_final": combined_final,
                    },
                    validation_runtime_seconds=validation_runtime,
                    output_vcf_s3_key=output_vcf_s3_key,
                    output_vcf_sha256=output_vcf_sha256,
                    happy_output_s3_key=happy_output_s3_key,
                )
            bt.logging.info(f"Score submitted for {miner_hotkey[:16]}...")
            return result
        except Exception as e:
            bt.logging.warning(f"Failed to submit score for {miner_hotkey[:16]}: {e}")
            return None

    async def _set_weights_after_round(self, round_id: str, submission_times: dict = None):
        """Compute winner-takes-all weights, set on chain, and POST history to platform.

        Called after all miners in a round have been scored and EMA updated.

        Args:
            round_id: The round that was just scored.
            submission_times: Dict of {hotkey: epoch} for tiebreaking.
        """
        if not self.is_registered:
            bt.logging.info("Skipping weight setting — not registered (demo mode)")
            return

        try:
            # Build list of miner hotkeys from metagraph (exclude validators and self)
            miner_hotkeys = []
            hotkey_to_uid = {}
            for uid in range(len(self.metagraph.hotkeys)):
                if uid == self.my_subnet_uid:
                    continue
                if self.metagraph.validator_permit[uid]:
                    continue
                hk = self.metagraph.hotkeys[uid]
                miner_hotkeys.append(hk)
                hotkey_to_uid[hk] = uid

            if not miner_hotkeys:
                bt.logging.warning("No miners found for weight setting")
                return

            # Compute winner-takes-all weights
            weights = self.score_tracker.get_winner_takes_all_weights(
                miner_hotkeys, submission_times
            )

            # Log weight distribution
            stats = self.score_tracker.get_stats()
            winner = [hk for hk, w in weights.items() if w > 0]
            print(f"\n   Weight distribution (winner-takes-all):", flush=True)
            print(f"   Eligible: {stats['eligible_count']}/{len(miner_hotkeys)} miners "
                  f"(need {stats['min_rounds_required']} rounds)", flush=True)
            if winner:
                w_hk = winner[0]
                w_ema = self.score_tracker.ema_scores.get(w_hk, 0)
                w_uid = hotkey_to_uid.get(w_hk, "?")
                print(f"   Winner: UID {w_uid} ({w_hk[:16]}...) EMA={w_ema:.4f}", flush=True)
            else:
                print(f"   No winner — weights submission skipped (fail-closed)", flush=True)

            # Set weights on chain (blocking RPC — run in thread to avoid blocking event loop)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self.set_weights, weights, hotkey_to_uid)

            # POST weight history to platform (non-blocking)
            try:
                validator_hotkey = self.wallet.hotkey.ss58_address
                entries = self.score_tracker.build_weight_history(
                    round_id, validator_hotkey, miner_hotkeys, weights
                )
                await self.platform_client.submit_weight_history(
                    round_id=round_id,
                    validator_hotkey=validator_hotkey,
                    entries=entries,
                )
                bt.logging.info(f"Weight history submitted to platform for round {round_id}")
            except Exception as e:
                bt.logging.warning(f"Failed to POST weight history: {e}")

        except Exception as e:
            bt.logging.error(f"Error in _set_weights_after_round: {e}")
            bt.logging.error(traceback.format_exc())

    def set_weights(self, weights_by_hotkey: dict = None, hotkey_to_uid: dict = None, retry_count: int = 0):
        """Set weights on the blockchain using winner-takes-all distribution.

        Args:
            weights_by_hotkey: Dict of {hotkey: weight} from ScoreTracker.
                              If None, computes weights from current EMA scores.
            hotkey_to_uid: Dict of {hotkey: uid} mapping.
                          If None, builds from metagraph.
            retry_count: Number of retry attempts.
        """
        try:
            # Build mappings if not provided
            if hotkey_to_uid is None:
                hotkey_to_uid = {}
                miner_hotkeys = []
                for uid in range(len(self.metagraph.hotkeys)):
                    if uid == self.my_subnet_uid:
                        continue
                    if self.metagraph.validator_permit[uid]:
                        continue
                    hk = self.metagraph.hotkeys[uid]
                    hotkey_to_uid[hk] = uid
                    miner_hotkeys.append(hk)
            else:
                miner_hotkeys = list(hotkey_to_uid.keys())

            # Compute weights if not provided
            if weights_by_hotkey is None:
                weights_by_hotkey = self.score_tracker.get_winner_takes_all_weights(miner_hotkeys)

            if not miner_hotkeys:
                bt.logging.warning("No miners found to set weights on")
                return False

            # Convert to UID-indexed arrays for Bittensor SDK
            miner_uids = []
            miner_weights = []
            for hk in miner_hotkeys:
                uid = hotkey_to_uid[hk]
                miner_uids.append(uid)
                miner_weights.append(weights_by_hotkey.get(hk, 0.0))

            # Re-normalize to sum to 1 (safety check)
            total = sum(miner_weights)
            if total > 0:
                miner_weights = [w / total for w in miner_weights]
            else:
                # Fail closed — never silently distribute equal weights.
                # Zero-sum weights indicate a scoring failure or no eligible miners
                # with positive EMA. Submitting equal weights would leak emissions.
                bt.logging.error(
                    "WEIGHT SAFETY: all computed weights are zero — skipping "
                    "weight submission to prevent unintended equal distribution. "
                    "This may indicate a scoring failure or all miners having zero EMA."
                )
                return False

            # CRITICAL: Use numpy arrays, NOT torch tensors!
            uids_array = np.array(miner_uids, dtype=np.int64)
            weights_array = np.array(miner_weights, dtype=np.float32)

            # Log weight distribution
            bt.logging.info(f"Submitting weights to chain...")
            bt.logging.info(f"  Network: {self.config.subtensor.network}, Netuid: {self.config.netuid}")
            bt.logging.info(f"  Miners: {len(miner_uids)}, Non-zero: {np.sum(weights_array > 0)}")
            bt.logging.info(f"  Weight stats: min={weights_array.min():.4f}, max={weights_array.max():.4f}, mean={weights_array.mean():.4f}")

            for i, (uid, weight) in enumerate(zip(miner_uids, miner_weights)):
                if i < 10:
                    bt.logging.info(f"    UID {uid}: {weight:.4f}")
            if len(miner_uids) > 10:
                bt.logging.info(f"    ... and {len(miner_uids) - 10} more")

            # Check commit-reveal status (for logging only)
            try:
                commit_reveal_enabled = self.subtensor.commit_reveal_enabled(self.config.netuid)
                print(f"   Commit-reveal enabled: {commit_reveal_enabled}", flush=True)
                if commit_reveal_enabled:
                    print(f"   Note: SDK handles commit-reveal automatically via set_weights", flush=True)
            except Exception as e:
                bt.logging.debug(f"Could not check commit-reveal: {e}")

            return self._set_weights_direct(uids_array, weights_array, retry_count)

        except Exception as e:
            error_str = str(e).lower()
            if "already" in error_str and "import" in error_str:
                bt.logging.info("Transaction already in mempool - will be processed")
                print(f"   Transaction in mempool, will be processed", flush=True)
                return True
            bt.logging.error(f"Failed to set weights: {e}")
            print(f"   Weight submission error: {e}", flush=True)
            return False

    def _set_weights_direct(self, uids, weights, retry_count: int = 0, max_retries: int = 3):
        """Set weights directly with exponential-backoff retry.

        Args:
            uids: numpy array of miner UIDs.
            weights: numpy array of weight values.
            retry_count: Current attempt number (0 = first try).
            max_retries: Maximum number of retry attempts.
        """
        print(f"\n   [Direct Mode] Submitting weights (attempt {retry_count + 1}/{max_retries + 1})...", flush=True)

        # Get current block for version_key
        try:
            current_block = self.subtensor.get_current_block()
            print(f"   Current block: {current_block}", flush=True)
        except Exception as e:
            bt.logging.debug(f"Could not get block: {e}")
            current_block = int(time.time())

        # Check rate limit
        try:
            blocks_since_last = self.subtensor.blocks_since_last_update(
                netuid=self.config.netuid,
                uid=self.my_subnet_uid
            )
            weights_rate_limit = self.subtensor.weights_rate_limit(netuid=self.config.netuid)
            print(f"   Blocks since last: {blocks_since_last}, Rate limit: {weights_rate_limit}", flush=True)

            if blocks_since_last is not None and blocks_since_last < weights_rate_limit:
                wait_blocks = weights_rate_limit - blocks_since_last
                wait_seconds = wait_blocks * BITTENSOR_BLOCK_TIME_SECONDS
                print(f"   Rate limited: wait {wait_blocks} blocks (~{wait_seconds}s)", flush=True)
                # If we have retries left, sleep and retry after the rate limit window
                if retry_count < max_retries:
                    sleep_time = min(wait_seconds + 5, MAX_SLEEP_SECONDS)  # cap at 2 min
                    print(f"   Sleeping {sleep_time}s before retry...", flush=True)
                    time.sleep(sleep_time)
                    return self._set_weights_direct(uids, weights, retry_count + 1, max_retries)
                return False
        except Exception as e:
            print(f"   Rate limit check failed: {e}", flush=True)

        # Ensure numpy arrays
        if not isinstance(uids, np.ndarray):
            uids = np.array(uids, dtype=np.int64)
        if not isinstance(weights, np.ndarray):
            weights = np.array(weights, dtype=np.float32)

        print(f"   Calling set_weights for {len(uids)} miners...", flush=True)
        try:
            success, msg = self.subtensor.set_weights(
                wallet=self.wallet,
                netuid=self.config.netuid,
                uids=uids,
                weights=weights,
                wait_for_finalization=False,
                wait_for_inclusion=True,
            )
        except Exception as e:
            error_str = str(e)
            if "Already Imported" in error_str:
                print(f"   Transaction already in mempool (will be processed)", flush=True)
                return True
            bt.logging.error(f"set_weights exception: {e}")
            print(f"   Exception: {e}", flush=True)
            # Retry on transient errors
            if retry_count < max_retries:
                delay = min(2 ** retry_count * 5, 60)  # 5s, 10s, 20s (cap 60s)
                print(f"   Retrying in {delay}s (attempt {retry_count + 2}/{max_retries + 1})...", flush=True)
                time.sleep(delay)
                return self._set_weights_direct(uids, weights, retry_count + 1, max_retries)
            return False

        if success:
            print(f"   Weights submitted successfully", flush=True)
            if msg:
                print(f"   Message: {msg}", flush=True)
            return True
        else:
            error_msg = str(msg) if msg else "Unknown error"
            if "Already Imported" in error_msg:
                print(f"   Transaction already in mempool", flush=True)
                return True
            else:
                print(f"   Failed: {error_msg}", flush=True)
                # Retry on non-permanent failures
                if retry_count < max_retries:
                    delay = min(2 ** retry_count * 5, 60)
                    print(f"   Retrying in {delay}s (attempt {retry_count + 2}/{max_retries + 1})...", flush=True)
                    time.sleep(delay)
                    return self._set_weights_direct(uids, weights, retry_count + 1, max_retries)
                return False

    async def run(self):
        """Main validator loop."""
        task_interval_mins = GENOMICS_CONFIG["task_interval"] // 60

        bt.logging.info(f"Validator running - Network: {self.config.subtensor.network}, "
                       f"Netuid: {self.config.netuid}")
        bt.logging.info(f"Task interval: {task_interval_mins} min, Miner timeout: {GENOMICS_CONFIG['variant_calling_timeout']//60} min, "
                       f"Docker: {is_docker_available()}")
        bt.logging.info(f"Platform URL: {os.getenv('PLATFORM_URL', 'Not set')}, Platform mode: {self.use_platform}")

        # Platform mode: verify platform is reachable
        if self.use_platform and self.platform_client:
            print(f"\n   Checking platform connection...", flush=True)
            try:
                if await self.platform_client.health_check():
                    print(f"   Platform is healthy - round-based scoring enabled", flush=True)
                else:
                    print(f"   Platform health check failed - falling back to standalone mode", flush=True)
                    self.use_platform = False
            except Exception as e:
                print(f"   Platform connection error: {e} - falling back to standalone mode", flush=True)
                self.use_platform = False

        # Restart recovery: rebuild ScoreTracker from platform DB
        if self.use_platform and self.platform_client:
            try:
                print(f"   Recovering state from platform...", flush=True)
                state = await self.platform_client.get_validator_state()

                self.score_tracker.recover_from_platform_state(
                    ema_entries=state.get("ema_scores", []),
                    round_history=state.get("round_history", []),
                )
                self.scored_rounds = set(state.get("scored_round_ids", []))

                print(
                    f"   State recovered: {len(self.score_tracker.ema_scores)} miners, "
                    f"{len(self.scored_rounds)} scored rounds",
                    flush=True,
                )
            except Exception as e:
                bt.logging.warning(f"State recovery failed (starting fresh): {e}")
                print(f"   State recovery failed: {e} (starting fresh)", flush=True)

        step = 0
        next_scoring_window = None  # Smart scheduling - when next scoring window starts

        try:
            while True:
                try:
                    round_start = time.time()
                    bt.logging.info(f"Round {step} started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

                    if self.use_platform:
                        result = await self.score_platform_rounds()
                        next_scoring_window = result.get("next_scoring_window_start")

                    if step % VALIDATOR_CONFIG["scoring_interval"] == 0:
                        stats = self.score_tracker.get_stats()
                        bt.logging.info(f"Performance stats - Top EMA: {stats['top_ema']:.4f}, "
                                       f"Eligible: {stats['eligible_count']}/{stats['total_miners_tracked']}, "
                                       f"Rounds tracked: {stats['rounds_tracked']}")

                    # Sync metagraph every round to catch new/removed miners quickly
                    self.metagraph.sync(subtensor=self.subtensor)
                    bt.logging.debug(f"Metagraph synced - {len(self.metagraph.hotkeys)} neurons")

                    # Cleanup old files every 2 rounds (5h keeps files for full round lifecycle)
                    if step % 2 == 0:
                        self._cleanup_old_files()

                    round_elapsed = time.time() - round_start
                    step += 1

                    next_round = datetime.now().timestamp() + GENOMICS_CONFIG["task_interval"]
                    next_round_str = datetime.fromtimestamp(next_round).strftime('%H:%M:%S')

                    bt.logging.info(f"Round {step-1} complete in {round_elapsed:.1f}s. "
                                   f"Next round at {next_round_str} (in {task_interval_mins} min)")

                    # Smart scheduling: if platform provided next scoring window, wait until then
                    # Otherwise fall back to fixed interval
                    if self.use_platform and next_scoring_window:
                        bt.logging.info(f"Smart scheduling: next_scoring_window={next_scoring_window}")
                        total_wait = self._calculate_wait_until_scoring(next_scoring_window)
                        if total_wait <= 0:
                            # Scoring window is now or in the past, use minimum wait
                            total_wait = MIN_WAIT_SECONDS
                        wait_reason = "scoring window"
                        bt.logging.info(f"Waiting {total_wait}s until next scoring window")
                    else:
                        bt.logging.debug(f"No smart scheduling: use_platform={self.use_platform}, next_scoring_window={next_scoring_window}")
                        total_wait = GENOMICS_CONFIG["task_interval"]
                        wait_reason = "task interval"

                    countdown_interval = 300  # 5 minutes
                    elapsed_wait = 0

                    next_check_time = datetime.now() + timedelta(seconds=total_wait)
                    next_check_str = next_check_time.strftime('%H:%M:%S')
                    wait_mins = total_wait // 60

                    print(f"\n   Waiting for next {wait_reason}...", flush=True)
                    print(f"   Next check at: {next_check_str}", flush=True)
                    print(f"   Time remaining: {wait_mins} minutes\n", flush=True)

                    while elapsed_wait < total_wait:
                        sleep_chunk = min(countdown_interval, total_wait - elapsed_wait)
                        await asyncio.sleep(sleep_chunk)
                        elapsed_wait += sleep_chunk

                        remaining = total_wait - elapsed_wait
                        if remaining > 0:
                            remaining_mins = remaining // 60
                            print(f"   Waiting... {remaining_mins} minutes until next {wait_reason}", flush=True)

                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    bt.logging.error(f"Error in main loop: {e}. Retrying in 10 seconds...")
                    await asyncio.sleep(10)

        except KeyboardInterrupt:
            bt.logging.info(f"Validator shutting down - Completed {step} rounds")


def main():
    """Main entry point."""
    validator = Validator()
    asyncio.run(validator.run())


if __name__ == "__main__":
    main()