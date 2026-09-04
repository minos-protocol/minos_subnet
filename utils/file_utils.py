"""Download and cache genomics data files from S3 and HTTP sources."""

import os
import hashlib
import urllib.request
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError
    HAS_BOTO3 = True
except ImportError:
    HAS_BOTO3 = False
    logger.warning("boto3 not installed - S3 features disabled")

# The public reference-data origin (behind api.theminos.ai/reference/*) returns
# 403 Forbidden for the default `Python-urllib/*` User-Agent as part of its bot
# protection. Set an explicit UA on every request so those downloads (and the
# presigned URLs served for BAMs) land cleanly instead of being bot-blocked.
USER_AGENT = "minos-installer/0.1 (+https://github.com/minos-protocol/minos_subnet)"

# Per-socket-operation timeout for downloads. Bounds a stalled connection
# without capping total transfer time for a large BAM.
DOWNLOAD_SOCKET_TIMEOUT = float(os.getenv("MINOS_DOWNLOAD_SOCKET_TIMEOUT", "60"))

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False
    logger.warning("tqdm not installed - progress bars disabled")


def _partial_path(local_path: Path) -> Path:
    """Sibling scratch path a download is written to before it is committed."""
    return local_path.with_name(local_path.name + ".part")


def _discard_partial(tmp_path: Path) -> None:
    """Remove a partial download so no later run can mistake it for a cache hit."""
    try:
        tmp_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning(f"Could not remove partial download {tmp_path}: {e}")


def download_file(
    url: str,
    local_path: Path,
    use_cache: bool = True,
    show_progress: bool = True
) -> Optional[Path]:
    """
    Download file from URL (supports S3 URIs, S3 public URLs, HTTP/HTTPS).

    Args:
        url: URL to download from (s3://, https://, http://)
        local_path: Path to save the file
        use_cache: Whether to use cached version if exists
        show_progress: Whether to show progress bar

    Returns:
        Path to downloaded file or None if failed
    """
    local_path = Path(local_path)

    # Check cache
    if use_cache and local_path.exists():
        logger.info(f"Using cached file: {local_path}")
        return local_path

    # Create directory if needed
    local_path.parent.mkdir(parents=True, exist_ok=True)

    # Write to a sibling .part and os.replace() it into position only on
    # success, so the final path never holds the bytes of a failed transfer:
    # round paths are deterministic and the cache check is exists + non-empty,
    # which a truncated file would satisfy.
    tmp_path = _partial_path(local_path)
    _discard_partial(tmp_path)

    # Handle s3:// URIs
    if url.startswith("s3://"):
        result = _download_from_s3_uri(url, tmp_path, show_progress)
        if result is None:
            _discard_partial(tmp_path)
            return None
        os.replace(tmp_path, local_path)
        return local_path

    # Handle HTTP/HTTPS URLs (including S3 presigned URLs)
    # Use raw urllib with Accept-Encoding: identity to prevent auto-decompression
    # of .gz files (S3 may set Content-Encoding: gzip which causes urlretrieve
    # to silently decompress, corrupting bgzipped VCF/BAM files)
    try:
        file_size = _get_remote_file_size(url)

        request = urllib.request.Request(url)
        request.add_header('Accept-Encoding', 'identity')
        request.add_header('User-Agent', USER_AGENT)

        # An explicit timeout is required: urlopen blocks indefinitely on a
        # blackholed endpoint and this runs synchronously inside the round loop.
        # It bounds each socket operation, not the whole transfer, so a slow but
        # progressing multi-GB BAM is unaffected.
        with urllib.request.urlopen(request, timeout=DOWNLOAD_SOCKET_TIMEOUT) as response:
            declared = response.headers.get('Content-Length')
            try:
                expected_bytes = int(declared) if declared is not None else None
            except (TypeError, ValueError):
                expected_bytes = None
            total = (expected_bytes or 0) or file_size or 0
            written = 0

            if show_progress and HAS_TQDM and total > 0:
                logger.info(f"Downloading {url} ({_format_size(total)})")
                with tqdm(
                    total=total,
                    unit='B',
                    unit_scale=True,
                    unit_divisor=1024,
                    desc=local_path.name,
                    bar_format='{desc}: {percentage:3.0f}%|{bar:20}| {n_fmt}/{total_fmt} [{rate_fmt}]',
                    ncols=80
                ) as pbar:
                    with open(tmp_path, 'wb') as f:
                        while True:
                            chunk = response.read(8192)
                            if not chunk:
                                break
                            f.write(chunk)
                            written += len(chunk)
                            pbar.update(len(chunk))
            else:
                logger.info(f"Downloading {url} to {local_path}")
                with open(tmp_path, 'wb') as f:
                    while True:
                        chunk = response.read(8192)
                        if not chunk:
                            break
                        f.write(chunk)
                        written += len(chunk)

        # http.client does not raise on a premature close mid-body:
        # HTTPResponse.read(amt) closes the connection instead of raising
        # IncompleteRead, so the read loop simply sees b'' and exits. Compare
        # the byte count against Content-Length or a truncated body reads as a
        # successful download.
        if expected_bytes is not None and written != expected_bytes:
            raise IOError(
                f"Incomplete download: got {written} bytes, "
                f"Content-Length advertised {expected_bytes}"
            )

        os.replace(tmp_path, local_path)
        logger.info(f"Downloaded to {local_path}")
        return local_path

    except Exception as e:
        logger.error(f"Failed to download {url}: {e}")
        _discard_partial(tmp_path)
        return None


def _download_from_s3_uri(
    s3_uri: str,
    local_path: Path,
    show_progress: bool = True
) -> Optional[Path]:
    """Download file from S3 URI, trying public HTTPS first, then authenticated."""
    # Parse s3://bucket/key
    uri_parts = s3_uri[5:].split("/", 1)
    bucket = uri_parts[0]
    key = uri_parts[1] if len(uri_parts) > 1 else ""

    local_path.parent.mkdir(parents=True, exist_ok=True)

    # Try anonymous HTTPS first (works for public buckets)
    region = os.environ.get("AWS_REGION", "us-east-1")
    if region == "us-east-1":
        https_url = f"https://{bucket}.s3.amazonaws.com/{key}"
    else:
        https_url = f"https://{bucket}.s3.{region}.amazonaws.com/{key}"

    logger.info(f"Attempting public download: {https_url}")

    try:
        file_size = _get_remote_file_size(https_url)

        if show_progress and HAS_TQDM and file_size and file_size > 0:
            logger.info(f"Downloading {s3_uri} ({_format_size(file_size)}) via HTTPS")
            with tqdm(
                total=file_size,
                unit='B',
                unit_scale=True,
                unit_divisor=1024,
                desc=local_path.name,
                bar_format='{desc}: {percentage:3.0f}%|{bar:20}| {n_fmt}/{total_fmt} [{rate_fmt}]',
                ncols=80
            ) as pbar:
                def _report_hook(block_num, block_size, total_size):
                    pbar.update(block_size)
                urllib.request.urlretrieve(https_url, local_path, reporthook=_report_hook)
        else:
            logger.info(f"Downloading {s3_uri} via HTTPS")
            urllib.request.urlretrieve(https_url, local_path)

        logger.info(f"Downloaded to {local_path} (public access)")
        return local_path

    except urllib.error.HTTPError as e:
        if e.code == 403:
            logger.info(f"Public access denied, trying authenticated access...")
        elif e.code == 404:
            logger.error(f"File not found: {s3_uri}")
            return None
        else:
            logger.warning(f"HTTPS download failed ({e.code}), trying authenticated access...")
    except Exception as e:
        logger.warning(f"Public download failed: {e}, trying authenticated access...")

    # Strategy 2: Try authenticated access with boto3
    if not HAS_BOTO3:
        logger.error("boto3 required for authenticated S3 downloads. Install with: pip install boto3")
        return None

    try:
        s3 = boto3.client('s3')

        # Get file size
        try:
            response = s3.head_object(Bucket=bucket, Key=key)
            file_size = response['ContentLength']
        except ClientError:
            file_size = 0

        if show_progress and HAS_TQDM and file_size > 0:
            logger.info(f"Downloading {s3_uri} ({_format_size(file_size)}) via boto3")
            with tqdm(
                total=file_size,
                unit='B',
                unit_scale=True,
                unit_divisor=1024,
                desc=local_path.name,
                bar_format='{desc}: {percentage:3.0f}%|{bar:20}| {n_fmt}/{total_fmt} [{rate_fmt}]',
                ncols=80
            ) as pbar:
                s3.download_file(bucket, key, str(local_path), Callback=pbar.update)
        else:
            logger.info(f"Downloading {s3_uri} to {local_path}")
            s3.download_file(bucket, key, str(local_path))

        logger.info(f"Downloaded to {local_path} (authenticated)")
        return local_path

    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code == '404':
            logger.error(f"File not found in S3: {s3_uri}")
        elif error_code == '403':
            logger.error(f"Permission denied for S3 file: {s3_uri}. Bucket may not be public.")
        else:
            logger.error(f"S3 download failed: {e}")
        return None
    except NoCredentialsError:
        logger.error(f"No AWS credentials found and bucket is not public: {s3_uri}")
        return None
    except Exception as e:
        logger.error(f"Failed to download from S3: {e}")
        return None


def _get_remote_file_size(url: str) -> Optional[int]:
    """Get file size from remote URL via HEAD request."""
    try:
        request = urllib.request.Request(url, method='HEAD')
        request.add_header('User-Agent', USER_AGENT)
        with urllib.request.urlopen(request, timeout=10) as response:
            content_length = response.headers.get('Content-Length')
            return int(content_length) if content_length else None
    except Exception:
        return None


def _format_size(size_bytes: int) -> str:
    """Format bytes to human-readable size."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def compute_sha256(file_path: Path) -> str:
    """Compute SHA256 hash of a file."""
    sha256 = hashlib.sha256()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            sha256.update(chunk)
    return sha256.hexdigest()


def download_file_with_fallback(
    primary_url: str,
    local_path: Path,
    backup_url: Optional[str] = None,
    expected_sha256: Optional[str] = None,
    show_progress: bool = True,
) -> Optional[Path]:
    """Download file trying primary_url first, then backup_url on failure.

    The calling code controls which URL is primary and which is backup —
    swap them to prefer a different backend (e.g. Hippius vs S3).

    Args:
        primary_url: First URL to try
        local_path: Path to save the file
        backup_url: Fallback URL if primary fails (e.g. Hippius backup)
        expected_sha256: SHA256 for cache verification
        show_progress: Whether to show progress bar

    Returns:
        Path to file or None if both downloads failed
    """
    local_path = Path(local_path)

    # With a backup available, a digest mismatch on the primary defers rather
    # than returning immediately, so the backup gets a chance to supply
    # matching bytes. With no backup configured there is nothing to defer to.
    result = download_file_verified(
        primary_url, local_path, expected_sha256=expected_sha256,
        show_progress=show_progress,
        on_mismatch="defer" if backup_url else "accept",
    )
    if result:
        return result

    if backup_url:
        logger.warning(f"Primary download failed, trying backup URL for {local_path.name}")
        # Hold the primary's bytes aside rather than deleting them, so they can
        # be restored if the backup does not produce a usable file. What counts
        # as usable on a digest mismatch follows enforce_download_sha256().
        quarantined = None
        if local_path.exists():
            quarantined = local_path.with_suffix(local_path.suffix + ".primary")
            try:
                local_path.replace(quarantined)
            except OSError:
                local_path.unlink(missing_ok=True)
                quarantined = None

        backup_result = download_file_verified(
            backup_url, local_path, expected_sha256=expected_sha256,
            show_progress=show_progress,
        )
        if backup_result:
            if quarantined:
                quarantined.unlink(missing_ok=True)
            return backup_result

        if quarantined:
            logger.warning(
                f"Backup also failed for {local_path.name}; falling back to the "
                f"primary copy that failed its digest check."
            )
            try:
                quarantined.replace(local_path)
                return local_path
            except OSError:
                quarantined.unlink(missing_ok=True)
        return None

    return None


def _normalised_digest(value) -> Optional[str]:
    """A comparable hex digest, or None when no usable digest was supplied.

    A blank or placeholder value is treated as absent rather than as a digest
    that can never match.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def enforce_download_sha256() -> bool:
    """Whether a digest mismatch on a fresh download fails the download.

    Cached files are always checked against the published digest, and a
    mismatching cache is discarded and re-fetched. This setting covers the
    freshly downloaded bytes: off (the default), a mismatch that survives the
    backup URL is logged and the bytes are used; on, they are discarded and the
    download fails. Files served without a published digest (indexes, for
    example) are unaffected either way.

    Set MINOS_ENFORCE_DOWNLOAD_SHA256 to 1/true/yes/on to enable.
    """
    return os.getenv("MINOS_ENFORCE_DOWNLOAD_SHA256", "").strip().lower() in ("1", "true", "yes", "on")


def download_file_verified(
    url: str,
    local_path: Path,
    expected_sha256: Optional[str] = None,
    show_progress: bool = True,
    on_mismatch: str = "accept",
) -> Optional[Path]:
    """Download file with SHA256 verification for caching.

    If file exists on disk and its SHA256 matches expected_sha256, skip download.
    If file exists but hash doesn't match (partial/corrupt), re-download.
    If expected_sha256 is None, use simple existence check (like use_cache=True).

    Args:
        url: URL to download from
        local_path: Path to save the file
        expected_sha256: Expected SHA256 hex digest for verification
        show_progress: Whether to show progress bar

    Returns:
        Path to file or None if failed
    """
    local_path = Path(local_path)

    if local_path.exists() and local_path.stat().st_size > 0:
        if expected_sha256 is None:
            logger.info(f"Cache hit (no hash check): {local_path.name}")
            return local_path

        actual_hash = compute_sha256(local_path)
        expected_norm = _normalised_digest(expected_sha256)
        if actual_hash == expected_norm:
            logger.info(f"Cache hit (SHA256 verified): {local_path.name}")
            return local_path
        else:
            # Slice the NORMALISED digest, never the raw value: the platform
            # supplies it and a non-string there (an int, a dict) makes this
            # log line raise, turning a cache miss into a crash.
            shown = (expected_norm or "<none>")[:16]
            logger.warning(
                f"Cache invalid for {local_path.name} "
                f"(expected={shown}..., actual={actual_hash[:16]}...). Re-downloading."
            )

    result = download_file(url, local_path, use_cache=False, show_progress=show_progress)
    if result is None:
        return None

    # Verify the freshly downloaded bytes too, not just the cache branch.
    expected = _normalised_digest(expected_sha256)
    if expected:
        actual_hash = compute_sha256(result)
        if actual_hash != expected:
            if enforce_download_sha256():
                logger.error(
                    f"SHA256 mismatch after downloading {local_path.name} "
                    f"(expected={expected[:16]}..., actual={actual_hash[:16]}...)"
                )
                _discard_partial(result)
                return None
            if on_mismatch == "defer":
                # Signal the mismatch without discarding: a caller holding a
                # backup URL can fetch it, and restore these bytes if the
                # backup does not produce a usable file.
                logger.warning(
                    f"SHA256 mismatch after downloading {local_path.name} "
                    f"(expected={expected[:16]}..., actual={actual_hash[:16]}...). "
                    f"Trying the backup source before accepting it."
                )
                return None
            logger.warning(
                f"SHA256 mismatch after downloading {local_path.name} "
                f"(expected={expected[:16]}..., actual={actual_hash[:16]}...). "
                f"Accepting the file; set MINOS_ENFORCE_DOWNLOAD_SHA256 to reject."
            )

    return result
