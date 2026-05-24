"""Platform API client for Minos miners and validators."""

import gzip
import io
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

from utils.path_utils import safe_round_dir_name

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

    async def get_network_config(self) -> Optional[Dict[str, Any]]:
        """Fetch authoritative network reward params.

        Returns None on any error; validators must treat that as fail-closed.
        """
        try:
            async with self._get_client() as client:
                response = await client.get("/scoring/network-config")
                if response.status_code == 200:
                    return response.json()
                return None
        except Exception:
            return None

    async def get_canonical_ranking(
        self,
        round_id: Optional[str] = None,
        top_n: int = 10,
    ) -> Optional[Dict[str, Any]]:
        """Fetch stake-weighted canonical ranking for a round.

        Used by validator weight tracking as a tiebreak signal when local EMA
        leaders are close. Returns the full response including the ranked list of
        ``{miner_hotkey, consensus_ema, validator_count, stake_share}``.

        Platform serves this only after the late-score grace period. Returns
        ``None`` on any error; the validator treats that as canonical
        unavailable for the round.

        Args:
            round_id: optional explicit round; defaults to platform's latest
                completed round.
            top_n: number of top miners to retrieve.
        """
        try:
            params: Dict[str, Any] = {"top_n": top_n}
            if round_id:
                params["round_id"] = round_id
            async with self._get_client() as client:
                # Keep the canonical lookup bounded on the weight-setting
                # path. The endpoint is expected to be cached after the
                # final-score gate opens.
                for attempt in range(3):
                    response = await client.get(
                        "/scoring/canonical-ranking",
                        params=params,
                        timeout=10.0,
                    )
                    if response.status_code == 200:
                        return response.json()
                    if response.status_code == 425 and attempt < 2:
                        await asyncio.sleep(5.0)
                        continue
                    break
                return None
        except Exception:
            return None


class MinerPlatformClient(PlatformClient):
    """Platform client for miners."""

    def __init__(self, keypair: Keypair, config: PlatformConfig, demo: bool = False):
        super().__init__(config)
        self.keypair = keypair
        self.miner_id: Optional[str] = None
        # Demo mode routes round-status + submit to the /v2/demo/* namespace
        # on the platform — sandboxed onboarding path that accepts ephemeral
        # keypairs and never writes to the live submissions DB.
        self.demo = demo

    @property
    def _round_status_path(self) -> str:
        return "/v2/demo/round-status" if self.demo else "/v2/round-status"

    @property
    def _submit_path(self) -> str:
        # /v2/demo/submit-result returns {success, submission_id, message,
        # is_demo: true} — same shape as /v2/submit-config's response so
        # the caller's success handling is unchanged. The is_demo flag is
        # the signal for the miner to print its DEMO COMPLETE banner.
        return "/v2/demo/submit-result" if self.demo else "/v2/submit-config"

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
        path = self._round_status_path

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
            tool_name: One of "gatk", "deepvariant", "bcftools".
                       (freebayes deprecated 2026-05-09 16:00 UTC; platform
                       returns HTTP 400 for new freebayes submissions.)
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

        path = self._submit_path

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

    # Wire-format version for the gzipped NDJSON variant-results file.
    # Bumped if the on-disk format changes; the platform validates against
    # its known set so a stale validator can't poison the column.
    _VARIANT_RESULTS_FORMAT_VERSION = "ndjson-gz-v1"

    @staticmethod
    def _serialize_variant_results_ndjson_gz(
        records: List[Dict[str, Any]],
    ) -> bytes:
        """Encode variant records as gzipped NDJSON.

        One JSON object per line, UTF-8, trailing newline. Compact JSON
        (no spaces) keeps wire size minimal; gzip on top compresses the
        repeated field names roughly 8-10x.
        """
        buf = io.BytesIO()
        # mtime=0 keeps the gzip header byte-for-byte deterministic so the
        # same input produces the same SHA-256 across runs (useful for
        # idempotency / dedup logic on the platform side).
        with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6, mtime=0) as gz:
            for record in records:
                line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
                gz.write(line.encode("utf-8"))
                gz.write(b"\n")
        return buf.getvalue()

    @staticmethod
    def _sha256_hex(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    async def _put_bytes_to_presigned(
        self,
        presigned_url: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> bool:
        """PUT raw bytes to a presigned URL. Mirrors upload_file_to_s3 but
        operates on an in-memory buffer (no local file)."""
        try:
            async with self._get_client() as client:
                response = await client.put(
                    presigned_url,
                    content=data,
                    headers={"Content-Type": content_type},
                    timeout=300.0,
                )
            if response.status_code in (200, 201, 204):
                return True
            logger.error(
                f"Presigned PUT failed ({response.status_code}): "
                f"{response.text[:200]}"
            )
            return False
        except Exception as e:
            logger.error(f"Presigned PUT raised: {e}")
            return False

    async def submit_variant_results(
        self,
        score_id: str,
        round_id: str,
        results: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Upload per-variant TP/FP/FN breakdown to object storage and
        record the pointer on the platform.

        Flow:
          1. Serialize ``results`` as gzipped NDJSON in memory
          2. Compute SHA-256 of the gzipped bytes
          3. Build s3_key under ``scoring/{hotkey}/{round_slug}/`` so the
             /v2/get-upload-url prefix check accepts it
          4. Ask the platform for a presigned PUT URL (cascade R2 ->
             Hippius -> AWS is decided server-side)
          5. PUT the gzipped bytes to that URL
          6. POST {score_id, s3_key, sha256, record_count, format_version}
             to /v2/submit-variant-results

        Args:
            score_id: UUID of the validator_score this belongs to.
            round_id: Round identifier (used to slug the S3 key prefix).
            results: List of variant-result dicts (chrom, pos, ref, alt,
                variant_type, classification, plus optional call-level
                fields). Empty list is accepted but the upload is still
                recorded as record_count=0.

        Returns:
            The platform's JSON response (success, s3_key, record_count).
        """
        # 1+2. Serialize and hash. Both are deterministic for a given input.
        payload = self._serialize_variant_results_ndjson_gz(results)
        sha256 = self._sha256_hex(payload)

        # 3. S3 key under the validator's prefix. safe_round_dir_name() is
        # the same slug used elsewhere for round artifacts so directory
        # listings stay tidy.
        round_slug = safe_round_dir_name(round_id)
        s3_key = (
            f"scoring/{self.keypair.ss58_address}/{round_slug}"
            f"/variant_results_{score_id}.ndjson.gz"
        )

        # 4. Get a presigned PUT URL from the platform (it picks the backend).
        presigned_url = await self.get_upload_url(s3_key)

        # 5. PUT the bytes. Failure here means we never recorded anything;
        # the validator's outer try/except will log and continue.
        ok = await self._put_bytes_to_presigned(
            presigned_url, payload, content_type="application/x-ndjson"
        )
        if not ok:
            raise PlatformClientError(
                f"Failed to upload variant-results file to {s3_key}"
            )

        # 6. Record the pointer.
        path = "/v2/submit-variant-results"

        async def _do_request():
            body = self._auth_body(
                "POST", path,
                score_id=score_id,
                validator_hotkey=self.keypair.ss58_address,
                s3_key=s3_key,
                sha256=sha256,
                record_count=len(results),
                format_version=self._VARIANT_RESULTS_FORMAT_VERSION,
            )

            async with self._get_client() as client:
                response = await client.post(
                    path, json=body, headers=self._AUTH_HEADERS, timeout=30.0
                )

                if response.status_code == 401:
                    raise AuthenticationError(
                        "Invalid signature or validator not authorized"
                    )
                if response.status_code == 404:
                    raise PlatformClientError("Score not found")
                if response.status_code != 200:
                    raise PlatformClientError(
                        f"Failed to record variant-results pointer: "
                        f"{response.text}"
                    )
                return response.json()

        return await retry_async(_do_request, max_retries=2)

    async def get_assignment(
        self,
        round_id: str,
    ) -> Dict[str, Any]:
        """Get this validator's scoring assignment for a round (validator only).

        The platform computes assignments from its own metagraph snapshot —
        no metagraph data needs to be supplied by the validator.

        Returns:
            Dict containing:
                - round_id: str
                - validator_hotkey: str
                - stake_rank: int
                - total_validators: int
                - primary_miner_hotkeys: List[str]   # score these first
                - overlap_miner_hotkeys: List[str]   # shared with adjacent validator
                - secondary_miner_hotkeys: List[str] # score if time allows
                - scoring_deadline: str              # ISO datetime
        """
        path = "/v2/get-assignment"

        async def _do_request():
            body = self._auth_body(
                "POST", path,
                validator_hotkey=self.keypair.ss58_address,
                round_id=round_id,
            )

            async with self._get_client() as client:
                response = await client.post(path, json=body, headers=self._AUTH_HEADERS)

                if response.status_code == 401:
                    raise AuthenticationError("Invalid signature or validator not authorized")
                if response.status_code == 404:
                    raise PlatformClientError(f"Round or assignment not found: {response.text}")
                if response.status_code == 409:
                    raise PlatformClientError(f"Round not in scoring phase: {response.text}")
                if response.status_code == 503:
                    raise PlatformClientError(f"Assignment unavailable: {response.text}")
                if response.status_code != 200:
                    raise PlatformClientError(f"Failed to get assignment: {response.text}")

                return response.json()

        return await retry_async(_do_request, max_retries=3)

    async def get_backfill_scores(
        self,
        round_id: str,
        scored_miner_hotkeys: List[str],
    ) -> Dict[str, Any]:
        """Fetch scores for miners not personally covered this round (validator only).

        Must be called after the scoring window and late-score grace have closed.
        The platform enforces this gate (returns 425 Too Early until final).

        Args:
            round_id: The round to fetch backfill scores for.
            scored_miner_hotkeys: Hotkeys this validator personally scored.

        Returns:
            Dict containing:
                - backfill_scores: List of {miner_hotkey, combined_final,
                                            primary_validator_hotkey, submitted_at}
                - overlap_deltas: List of {miner_hotkey, your_score, peer_score,
                                           delta, peer_validator_hotkey}
                - unscored_miner_hotkeys: List[str]   # no score from any validator
                - gap_count: int
        """
        path = "/v2/get-backfill-scores"

        async def _do_request():
            body = self._auth_body(
                "POST", path,
                validator_hotkey=self.keypair.ss58_address,
                round_id=round_id,
                scored_miner_hotkeys=scored_miner_hotkeys,
            )

            async with self._get_client() as client:
                response = await client.post(
                    path, json=body, headers=self._AUTH_HEADERS, timeout=60.0
                )

                if response.status_code == 401:
                    raise AuthenticationError("Invalid signature or validator not authorized")
                if response.status_code == 404:
                    raise PlatformClientError("Round not found")
                if response.status_code == 425:
                    raise PlatformClientError(
                        f"Scoring window still open (Too Early): {response.text}"
                    )
                if response.status_code == 503:
                    raise PlatformClientError(
                        f"Backfill unavailable: {response.text}"
                    )
                if response.status_code != 200:
                    raise PlatformClientError(f"Failed to get backfill scores: {response.text}")

                return response.json()

        return await retry_async(_do_request, max_retries=2)

    async def submit_weight_history(
        self,
        round_id: str,
        validator_hotkey: str,
        entries: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Submit weight history (EMA scores, ranks, weights) for a round.

        Called after computing validator weights so the platform
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
