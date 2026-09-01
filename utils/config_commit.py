"""Miner-side commitment over a submitted tool config.

The miner hashes the config it is about to submit, publishes that hash on chain,
and sends the same hash to the platform. Revealing the nonce later proves what
was submitted, at a time the chain attests to. The platform commits to the
config it STORED; this is the other half, over the config as SENT, so the
difference between the two is the submit-time clamping.

The nonce is required, not optional: the parameter ranges are bounded and the
config space small enough to enumerate, so the commitment has to be salted.

The digest binds more than the config:
    domain    — this hash cannot be replayed as any other minos signature
    version   — the scheme can change without reinterpreting old commits
    netuid    — a commitment on one subnet is not valid on another
    round_id  — a commitment cannot be reused for a later round
    hotkey    — another miner cannot claim your published commitment as theirs
    tool_name — the tool is part of what was submitted

Persist the nonce before publishing the commitment: a commitment whose nonce is
lost can never be opened. See ``CommitmentLedger``.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

DOMAIN = "minos-config-commit"
VERSION = 1
NONCE_BYTES = 32

# Local execution settings, not quality parameters. Stripped before the config
# leaves the miner, so the commitment MUST be taken over the stripped form or it
# would not describe what the platform received. Single source of truth —
# platform_client.submit_config consumes this via submission_config().
INFRA_PARAMS = frozenset(
    {"threads", "memory_gb", "timeout", "ref_build", "num_threads"}
)

# Chain commitment storage is small. Exceeding it raises rather than truncating
# a digest into something that proves nothing.
MAX_CHAIN_PAYLOAD_BYTES = 128


def new_nonce() -> str:
    """Fresh 256-bit nonce, hex encoded.

    Not published on chain — the chain gets the digest. It IS sent to the
    platform with the config, which is what lets the digest be recomputed and
    checked; a nonce opens exactly one commitment, over a config travelling in
    the same request.
    """
    return secrets.token_hex(NONCE_BYTES)


def submission_config(tool_config: Dict[str, Any]) -> Dict[str, Any]:
    """The config as it will be sent to the platform (infrastructure stripped)."""
    return {k: v for k, v in (tool_config or {}).items() if k not in INFRA_PARAMS}


def _normalise(value: Any) -> Any:
    """Collapse spellings that mean the same thing to the variant caller.

    30 and 30.0 are the same value to the tool, so they must not produce
    different commitments — the hash has to reproduce across a reparse of the
    config file.
    """
    if isinstance(value, bool):        # bool is a subclass of int — check first
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _normalise(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalise(v) for v in value]
    return value


def canonical_config(tool_config: Dict[str, Any]) -> str:
    """Deterministic serialisation of the submitted config."""
    return json.dumps(
        _normalise(submission_config(tool_config)),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def compute_commitment(
    *,
    netuid: int,
    round_id: str,
    hotkey: str,
    tool_name: str,
    tool_config: Dict[str, Any],
    nonce: str,
) -> str:
    """SHA-256 over the bound tuple. 64 hex chars."""
    if not nonce:
        raise ValueError("refusing to build a commitment without a nonce")
    parts = [
        DOMAIN,
        str(VERSION),
        str(netuid),
        str(round_id),
        str(hotkey),
        str(tool_name).lower(),
        nonce,
        canonical_config(tool_config),
    ]
    # \x1f separated so no field can be shifted into its neighbour.
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def verify_commitment(expected: str, **kwargs) -> bool:
    """Recompute and compare in constant time. Used when opening a commitment."""
    if not expected:
        return False
    return secrets.compare_digest(compute_commitment(**kwargs), expected)


def chain_payload(round_id: str, commitment: str) -> str:
    """Compact on-chain form: ``m1:<round8>:<commitment>``.

    ``round8`` is a truncated digest of the round id, enough to tell which round
    a commitment belongs to. The commitment itself is never truncated.
    """
    round8 = hashlib.sha256(str(round_id).encode()).hexdigest()[:8]
    payload = f"m{VERSION}:{round8}:{commitment}"
    if len(payload.encode("utf-8")) > MAX_CHAIN_PAYLOAD_BYTES:
        raise ValueError(
            f"chain payload {len(payload)}B exceeds {MAX_CHAIN_PAYLOAD_BYTES}B budget"
        )
    return payload


class CommitmentLedger:
    """Append-only local record of every commitment made.

    Written BEFORE the commitment is published anywhere: a commitment whose
    nonce was never persisted cannot be opened.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(
            path
            or os.getenv("MINOS_COMMITMENT_LEDGER")
            or (Path.home() / ".minos" / "commitments.jsonl")
        )

    def record(self, entry: Dict[str, Any]) -> None:
        # 0o700 so a permissive umask cannot leave the directory holding
        # secrets world-traversable; exist_ok leaves an existing mode alone.
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        line = json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str)
        # fsync before returning: a published commitment whose nonce did not
        # reach disk is unopenable.
        # os.open with 0600 rather than open()+chmod, which would create the
        # file under the umask first and expose the nonce until the chmod lands.
        # O_NOFOLLOW refuses a symlink planted at this path.
        fd = os.open(
            self.path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        try:
            self.path.chmod(0o600)  # tighten a pre-existing file
        except OSError:
            pass

    def find(self, round_id: str, hotkey: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Everything known about a round's commitment, for opening it later.

        The ledger is append-only and a commitment is written in two parts: the
        nonce and config when it is built, then the block once it is published.
        Returning only the LAST matching line therefore returned the publication
        record -- which carries no nonce and no config, and so cannot open the
        commitment it describes.

        Matching entries are merged in file order instead, so later fields
        (block, published) overlay earlier ones (nonce, tool_config) and the
        result is the complete record. A later None does not erase a known
        value: the publication entry writes ``block`` and the build entry writes
        ``block: None``, and the ordering must not depend on which came last.
        """
        if not self.path.exists():
            return None
        merged: Optional[Dict[str, Any]] = None
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("round_id") != round_id:
                    continue
                if hotkey and entry.get("hotkey") != hotkey:
                    continue
                if merged is None:
                    merged = dict(entry)
                    continue
                for k, v in entry.items():
                    if v is not None or k not in merged:
                        merged[k] = v
        return merged
