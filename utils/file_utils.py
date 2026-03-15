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

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False
    logger.warning("tqdm not installed - progress bars disabled")


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

    # Handle s3:// URIs
    if url.startswith("s3://"):
        return _download_from_s3_uri(url, local_path, show_progress)

    # Handle HTTP/HTTPS URLs (including S3 presigned URLs)
    # Use raw urllib with Accept-Encoding: identity to prevent auto-decompression
    # of .gz files (S3 may set Content-Encoding: gzip which causes urlretrieve
    # to silently decompress, corrupting bgzipped VCF/BAM files)
    try:
        file_size = _get_remote_file_size(url)

        request = urllib.request.Request(url)
        request.add_header('Accept-Encoding', 'identity')

        with urllib.request.urlopen(request) as response:
            total = int(response.headers.get('Content-Length', 0)) or file_size or 0

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
                    with open(local_path, 'wb') as f:
                        while True:
                            chunk = response.read(8192)
                            if not chunk:
                                break
                            f.write(chunk)
                            pbar.update(len(chunk))
            else:
                logger.info(f"Downloading {url} to {local_path}")
                with open(local_path, 'wb') as f:
                    while True:
                        chunk = response.read(8192)
                        if not chunk:
                            break
                        f.write(chunk)

        logger.info(f"Downloaded to {local_path}")
        return local_path

    except Exception as e:
        logger.error(f"Failed to download {url}: {e}")
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

    result = download_file_verified(primary_url, local_path, expected_sha256=expected_sha256, show_progress=show_progress)
    if result:
        return result

    if backup_url:
        logger.warning(f"Primary download failed, trying backup URL for {local_path.name}")
        # Remove any partial file before retrying
        if local_path.exists():
            local_path.unlink()
        return download_file_verified(backup_url, local_path, expected_sha256=expected_sha256, show_progress=show_progress)

    return None


def download_file_verified(
    url: str,
    local_path: Path,
    expected_sha256: Optional[str] = None,
    show_progress: bool = True,
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
        if actual_hash == expected_sha256:
            logger.info(f"Cache hit (SHA256 verified): {local_path.name}")
            return local_path
        else:
            logger.warning(
                f"Cache invalid for {local_path.name} "
                f"(expected={expected_sha256[:16]}..., actual={actual_hash[:16]}...). Re-downloading."
            )

    return download_file(url, local_path, use_cache=False, show_progress=show_progress)
