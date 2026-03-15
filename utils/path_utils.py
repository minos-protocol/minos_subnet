"""Safe filesystem path utilities for round-based operations.

All local directory names derived from round IDs use SHA-256 hashing
to prevent collisions and path-traversal attacks.
"""

import hashlib


def safe_round_dir_name(round_id: str) -> str:
    """Generate a collision-safe directory name from a round ID.

    Uses a truncated SHA-256 hash (16 hex chars = 64 bits) instead of raw
    round_id characters, preventing both path-traversal and truncation
    collisions (e.g., two timestamps sharing a prefix).

    Args:
        round_id: ISO-8601 round identifier.

    Returns:
        Directory name like ``round_a1b2c3d4e5f67890``.
    """
    digest = hashlib.sha256(round_id.encode()).hexdigest()[:16]
    return f"round_{digest}"
