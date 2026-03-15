"""Platform API client for Minos miners and validators."""

import os
import time
import asyncio
import hashlib
import json
import logging
import uuid
import httpx
from typing import Optional, Dict, Any, List, Callable, TypeVar
from dataclasses import dataclass
from bittensor_wallet import Keypair

logger = logging.getLogger(__name__)

T = TypeVar('T')


async def retry_async(
    func: Callable[..., T],
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retryable_exceptions: tuple = (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError),
) -> T:
    """Retry an async function with exponential backoff.

    Args:
        func: Async callable to retry
        max_retries: Maximum number of retry attempts
        base_delay: Initial delay between retries in seconds
        max_delay: Maximum delay between retries
        retryable_exceptions: Tuple of exceptions that trigger a retry

    Returns:
        The result of the function call

    Raises:
        The last exception if all retries are exhausted
    """
    last_exception = None
    for attempt in range(max_retries + 1):
        try:
            return await func()
        except retryable_exceptions as e:
            last_exception = e
            if attempt < max_retries:
                delay = min(base_delay * (2 ** attempt), max_delay)
                logger.warning(f"Retry {attempt + 1}/{max_retries} after {delay:.1f}s due to: {e}")
                await asyncio.sleep(delay)
            else:
                logger.error(f"All {max_retries} retries exhausted: {e}")
    raise last_exception


@dataclass
class PlatformConfig:
    """Platform connection configuration."""
    base_url: str
    timeout: float = 60.0


class PlatformClientError(Exception):
    """Base exception for platform client errors."""
    pass


class AuthenticationError(PlatformClientError):
    """Authentication failed."""
    pass


class PlatformClient:
    """Base client for platform API communication."""

    _AUTH_HEADERS = {"X-Minos-Auth-Version": "2"}

    def __init__(self, config: PlatformConfig):
        # Require HTTPS for non-localhost URLs to prevent credential interception
        url = config.base_url.rstrip("/")
        if not url.startswith("https://") and not any(
            url.startswith(f"http://{host}") for host in ("localhost", "127.0.0.1", "[::1]")
        ):
            raise ValueError(
                f"PLATFORM_URL must use HTTPS (got {url}). "
                "HTTP is only allowed for localhost development."
            )
        self.config = config
        # Don't create client here - create fresh one for each request
        # to avoid event loop binding issues with bittensor's threading

    def _get_client(self) -> httpx.AsyncClient:
        """Get a fresh httpx client for the current event loop."""
        return httpx.AsyncClient(
            base_url=self.config.base_url,
            timeout=self.config.timeout
        )

    @staticmethod
    def sign_request(keypair: Keypair, method: str, path: str, body: dict, timestamp: int, nonce: str) -> str:
        """Sign a canonical request payload.

        Canonical string: METHOD|PATH|BODY_HASH|TIMESTAMP|NONCE
        where BODY_HASH = SHA256 of the JSON body (sorted keys, compact separators),
        excluding 'signature' and 'nonce' fields.
        """
        canonical_body = {k: v for k, v in sorted(body.items()) if k not in ("signature", "nonce")}
        body_hash = hashlib.sha256(
            json.dumps(canonical_body, sort_keys=True, separators=(',', ':')).encode()
        ).hexdigest()
        canonical = f"{method.upper()}|{path}|{body_hash}|{timestamp}|{nonce}"
        return keypair.sign(canonical.encode()).hex()

    def _auth_body(self, method: str, path: str, **fields) -> dict:
        """Build request body with canonical auth signature and nonce."""
        timestamp = int(time.time())
        nonce = uuid.uuid4().hex
        body = {**fields, "timestamp": timestamp}
        body["signature"] = self.sign_request(self.keypair, method, path, body, timestamp, nonce)
        body["nonce"] = nonce
        return body

    async def health_check(self) -> bool:
        """Check if platform is healthy."""
        try:
            async with self._get_client() as client:
                response = await client.get("/health")
                return response.status_code == 200
        except Exception:
            return False


class MinerPlatformClient(PlatformClient):
    """Platform client for miners."""

    def __init__(self, keypair: Keypair, config: PlatformConfig):
        super().__init__(config)
        self.keypair = keypair
        self.miner_id: Optional[str] = None

    # =========================================================================
    # Round-Based API Methods
    # =========================================================================

    async def get_round_status(self) -> Dict[str, Any]:
        """Get current round status from the platform (authenticated).

        Requires valid hotkey signature to access round data and BAM URLs.

        Returns:
            Dict containing:
                - has_active_round: bool
                - round_id: Optional[str]
                - status: Optional[str] ("pending", "open", "scoring", "completed")
                - start_time: Optional[datetime]
                - submission_end_time: Optional[datetime]
                - scoring_end_time: Optional[datetime]
                - region: Optional[str]
                - bam_presigned_url: Optional[str]
                - bam_index_presigned_url: Optional[str]
                - num_mutations: Optional[int]
                - downsampled_coverage: Optional[int]
                - time_remaining_seconds: Optional[int]
        """
        path = "/v2/round-status"

        async def _do_request():
            body = self._auth_body("POST", path, hotkey=self.keypair.ss58_address)

            async with self._get_client() as client:
                response = await client.post(path, json=body, headers=self._AUTH_HEADERS)

                if response.status_code == 401:
                    raise AuthenticationError("Invalid signature or miner not registered on subnet")
                if response.status_code != 200:
                    raise PlatformClientError(f"Failed to get round status: {response.text}")

                return response.json()

        return await retry_async(_do_request, max_retries=2)

    async def submit_config(
        self,
        round_id: str,
        tool_name: str,
        tool_config: Dict[str, Any],
        variant_count: Optional[int] = None,
        runtime_seconds: Optional[float] = None
    ) -> Dict[str, Any]:
        """Submit tool configuration for a round.

        Miners submit their tool configuration instead of VCF files.
        Validators run the variant calling themselves to verify results.

        Args:
            round_id: The round ID to submit to
            tool_name: One of "gatk", "deepvariant", "freebayes", "bcftools"
            tool_config: Full tool configuration as JSON
            variant_count: Optional number of variants called
            runtime_seconds: Optional runtime in seconds

        Returns:
            Dict containing:
                - success: bool
                - submission_id: UUID
                - message: str
        """
        # SECURITY: Strip infrastructure params before sending to platform
        # These are local system settings, not quality parameters
        _INFRA_PARAMS = {"threads", "memory_gb", "timeout", "ref_build", "num_threads"}
        safe_config = {k: v for k, v in tool_config.items() if k not in _INFRA_PARAMS}

        path = "/v2/submit-config"

        async def _do_request():
            body = self._auth_body(
                "POST", path,
                hotkey=self.keypair.ss58_address,
                round_id=round_id,
                tool_name=tool_name,
                tool_config=safe_config,
                variant_count=variant_count,
                runtime_seconds=runtime_seconds,
            )

            async with self._get_client() as client:
                response = await client.post(path, json=body, headers=self._AUTH_HEADERS)

                if response.status_code == 401:
                    raise AuthenticationError("Invalid signature or miner not registered")
                if response.status_code == 400:
                    raise PlatformClientError(f"Invalid request: {response.text}")
                if response.status_code == 404:
                    raise PlatformClientError("Round not found")
                if response.status_code == 409:
                    raise PlatformClientError(f"Round not open for submissions: {response.text}")
                if response.status_code != 200:
                    raise PlatformClientError(f"Failed to submit config: {response.text}")

                return response.json()

        return await retry_async(_do_request, max_retries=2)


class ValidatorPlatformClient(PlatformClient):
    """Platform client for validators."""

    def __init__(self, keypair: Keypair, config: PlatformConfig):
        super().__init__(config)
        self.keypair = keypair

    # =========================================================================
    # Round-Based API Methods
    # =========================================================================

    async def get_validator_state(self) -> Dict[str, Any]:
        """Get validator state for restart recovery (EMA scores, round history, scored rounds).

        Returns:
            Dict containing:
                - ema_scores: List of {miner_hotkey, ema_score, participation_count, eligible}
                - round_history: List of {round_id, scored_hotkeys}
                - scored_round_ids: List of round_id strings
        """
        path = "/v2/get-validator-state"

        async def _do_request():
            body = self._auth_body("POST", path, validator_hotkey=self.keypair.ss58_address)

            async with self._get_client() as client:
                response = await client.post(path, json=body, headers=self._AUTH_HEADERS)

                if response.status_code == 401:
                    raise AuthenticationError("Invalid signature or validator not authorized")
                if response.status_code != 200:
                    raise PlatformClientError(f"Failed to get validator state: {response.text}")

                return response.json()

        return await retry_async(_do_request, max_retries=2)

    async def get_scoring_rounds(self) -> Dict[str, Any]:
        """Get all rounds currently in scoring phase (validator only).

        Validators call this to discover which rounds need scoring.
        Includes retry logic for transient network errors.

        Returns:
            Dict containing:
                - scoring_rounds: List of round info dicts
                - next_scoring_window_start: Optional[str] - ISO datetime of next scoring window
        """
        path = "/v2/get-scoring-rounds"

        async def _do_request():
            body = self._auth_body("POST", path, validator_hotkey=self.keypair.ss58_address)

            async with self._get_client() as client:
                response = await client.post(path, json=body, headers=self._AUTH_HEADERS)

                if response.status_code == 401:
                    raise AuthenticationError("Invalid signature or validator not authorized")
                if response.status_code != 200:
                    raise PlatformClientError(f"Failed to get scoring rounds: {response.text}")

                return response.json()

        return await retry_async(_do_request, max_retries=3)

    async def get_round_submissions(self, round_id: str) -> Dict[str, Any]:
        """Get all submissions for a round (validator only).

        Validators call this during the scoring window to get all miner submissions.
        Includes retry logic for transient network errors.

        Args:
            round_id: The round ID to get submissions for

        Returns:
            Dict containing:
                - round_id: str
                - region: str
                - num_mutations: int
                - submissions: List[SubmissionDetail]
                - bam_presigned_url: str
                - bam_index_presigned_url: str
                - truth_vcf_presigned_url: str
        """
        path = "/v2/get-submissions"

        async def _do_request():
            body = self._auth_body(
                "POST", path,
                round_id=round_id,
                validator_hotkey=self.keypair.ss58_address,
            )

            async with self._get_client() as client:
                response = await client.post(path, json=body, headers=self._AUTH_HEADERS)

                if response.status_code == 401:
                    raise AuthenticationError("Invalid signature or validator not authorized")
                if response.status_code == 404:
                    raise PlatformClientError("Round not found")
                if response.status_code == 409:
                    raise PlatformClientError(f"Round not in scoring phase: {response.text}")
                if response.status_code != 200:
                    raise PlatformClientError(f"Failed to get submissions: {response.text}")

                return response.json()

        return await retry_async(_do_request, max_retries=3)

    async def submit_score(
        self,
        round_id: str,
        miner_hotkey: str,
        snp_f1: Optional[float] = None,
        snp_precision: Optional[float] = None,
        snp_recall: Optional[float] = None,
        snp_tp: Optional[int] = None,
        snp_fp: Optional[int] = None,
        snp_fn: Optional[int] = None,
        indel_f1: Optional[float] = None,
        indel_precision: Optional[float] = None,
        indel_recall: Optional[float] = None,
        indel_tp: Optional[int] = None,
        indel_fp: Optional[int] = None,
        indel_fn: Optional[int] = None,
        ti_tv_ratio: Optional[float] = None,
        het_hom_ratio: Optional[float] = None,
        additional_metrics: Optional[Dict[str, Any]] = None,
        validation_runtime_seconds: Optional[float] = None,
        output_vcf_s3_key: Optional[str] = None,
        output_vcf_sha256: Optional[str] = None,
        happy_output_s3_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Submit scoring results for a miner (validator only).

        Validators call this after running hap.py on a miner's VCF output.
        Includes retry logic for transient network errors.

        Args:
            round_id: The round ID
            miner_hotkey: The miner's hotkey being scored
            snp_f1/precision/recall/tp/fp/fn: SNP metrics from hap.py
            indel_f1/precision/recall/tp/fp/fn: INDEL metrics from hap.py
            ti_tv_ratio: Transition/transversion ratio
            het_hom_ratio: Het/hom ratio
            additional_metrics: Any additional metrics
            validation_runtime_seconds: How long validation took
            output_vcf_s3_key: S3 key for miner output VCF (audit trail)
            output_vcf_sha256: SHA256 of miner output VCF
            happy_output_s3_key: S3 key for hap.py annotated VCF (audit trail)

        Returns:
            Dict containing:
                - success: bool
                - score_id: UUID
        """
        path = "/v2/submit-score"

        async def _do_request():
            body = self._auth_body(
                "POST", path,
                round_id=round_id,
                validator_hotkey=self.keypair.ss58_address,
                miner_hotkey=miner_hotkey,
                snp_f1=snp_f1,
                snp_precision=snp_precision,
                snp_recall=snp_recall,
                snp_tp=snp_tp,
                snp_fp=snp_fp,
                snp_fn=snp_fn,
                indel_f1=indel_f1,
                indel_precision=indel_precision,
                indel_recall=indel_recall,
                indel_tp=indel_tp,
                indel_fp=indel_fp,
                indel_fn=indel_fn,
                ti_tv_ratio=ti_tv_ratio,
                het_hom_ratio=het_hom_ratio,
                additional_metrics=additional_metrics,
                validation_runtime_seconds=validation_runtime_seconds,
                output_vcf_s3_key=output_vcf_s3_key,
                output_vcf_sha256=output_vcf_sha256,
                happy_output_s3_key=happy_output_s3_key,
            )

            async with self._get_client() as client:
                response = await client.post(path, json=body, headers=self._AUTH_HEADERS)

                if response.status_code == 401:
                    raise AuthenticationError("Invalid signature or validator not authorized")
                if response.status_code == 404:
                    raise PlatformClientError("Round or miner submission not found")
                if response.status_code == 409:
                    raise PlatformClientError(f"Round not in scoring phase: {response.text}")
                if response.status_code != 200:
                    raise PlatformClientError(f"Failed to submit score: {response.text}")

                return response.json()

        return await retry_async(_do_request, max_retries=3)

    async def get_upload_url(self, s3_key: str) -> str:
        """Get a presigned PUT URL to upload a file to S3.

        Args:
            s3_key: The S3 key to upload to (must start with 'scoring/')

        Returns:
            Presigned PUT URL string
        """
        path = "/v2/get-upload-url"

        async def _do_request():
            body = self._auth_body(
                "POST", path,
                s3_key=s3_key,
                validator_hotkey=self.keypair.ss58_address,
            )

            async with self._get_client() as client:
                response = await client.post(path, json=body, headers=self._AUTH_HEADERS)

                if response.status_code != 200:
                    raise PlatformClientError(f"Failed to get upload URL: {response.text}")

                data = response.json()
                url = data.get("presigned_url")
                if not url:
                    raise PlatformClientError(f"No presigned_url in response: {data}")
                return url

        return await retry_async(_do_request, max_retries=2)

    async def upload_file_to_s3(self, local_path: str, s3_key: str) -> bool:
        """Upload a file to S3 via presigned PUT URL.

        Args:
            local_path: Path to local file
            s3_key: S3 key to upload to

        Returns:
            True on success, False on failure
        """
        try:
            presigned_url = await self.get_upload_url(s3_key)

            with open(local_path, 'rb') as f:
                file_data = f.read()

            async with self._get_client() as client:
                response = await client.put(
                    presigned_url,
                    content=file_data,
                    headers={"Content-Type": "application/octet-stream"},
                    timeout=300.0
                )

            if response.status_code in (200, 201, 204):
                logger.info(f"Uploaded {os.path.basename(local_path)} to S3: {s3_key}")
                return True
            else:
                logger.error(f"S3 upload failed ({response.status_code}): {response.text[:200]}")
                return False

        except Exception as e:
            logger.error(f"Failed to upload {local_path} to S3: {e}")
            return False

    async def submit_variant_results(
        self,
        score_id: str,
        results: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Submit per-variant classification results from hap.py.

        Args:
            score_id: UUID of the validator_score this belongs to
            results: List of variant result dicts with keys:
                     chrom, pos, ref, alt, variant_type, classification,
                     quality, filter_status

        Returns:
            Dict with success and variants_stored count
        """
        path = "/v2/submit-variant-results"

        async def _do_request():
            body = self._auth_body(
                "POST", path,
                score_id=score_id,
                validator_hotkey=self.keypair.ss58_address,
                results=results,
            )

            async with self._get_client() as client:
                response = await client.post(
                    path, json=body, headers=self._AUTH_HEADERS, timeout=60.0
                )

                if response.status_code == 401:
                    raise AuthenticationError("Invalid signature or validator not authorized")
                if response.status_code == 404:
                    raise PlatformClientError("Score not found")
                if response.status_code != 200:
                    raise PlatformClientError(f"Failed to submit variant results: {response.text}")

                return response.json()

        return await retry_async(_do_request, max_retries=2)

    async def submit_weight_history(
        self,
        round_id: str,
        validator_hotkey: str,
        entries: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Submit weight history (EMA scores, ranks, weights) for a round.

        Called after computing winner-takes-all weights so the platform
        can display historical scoring data on the dashboard.

        Args:
            round_id: The round these weights correspond to.
            validator_hotkey: The validator's hotkey.
            entries: List of per-miner dicts with keys:
                     miner_hotkey, raw_score, ema_score, rank, weight,
                     eligible, participation_count.

        Returns:
            Dict with success status and stored count.
        """
        path = "/v2/submit-weight-history"

        async def _do_request():
            body = self._auth_body(
                "POST", path,
                round_id=round_id,
                validator_hotkey=validator_hotkey,
                entries=entries,
            )

            async with self._get_client() as client:
                response = await client.post(
                    path, json=body, headers=self._AUTH_HEADERS, timeout=30.0
                )

                if response.status_code == 401:
                    raise AuthenticationError("Invalid signature or validator not authorized")
                if response.status_code == 404:
                    raise PlatformClientError("Round not found")
                if response.status_code != 200:
                    raise PlatformClientError(f"Failed to submit weight history: {response.text}")

                return response.json()

        return await retry_async(_do_request, max_retries=2)
