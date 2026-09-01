"""Which scoring formula this validator should apply, decided by the platform.

v1 and v2 are different SCALES. A round scored partly by each produces a
meaningless ranking, and ranking is what pays — so the choice cannot be left to
each operator's environment, where any rollout guarantees a mixed fleet for as
long as the slowest operator takes to notice. The platform advertises it in
network-config and every validator follows.

WHAT HAPPENS WHEN THE PLATFORM CANNOT BE REACHED is the whole reason this module
exists. Defaulting to v1 on a failed fetch would be worse than useless: during a
v2 rollout, every validator that briefly lost the platform would drop back to v1
and diverge from the fleet precisely when the network is least able to notice.
So a resolved version is PERSISTED and reused. An unreachable platform changes
nothing; it just keeps doing what it was doing.

Only a validator that has never successfully read the platform falls back to v1,
which is the live formula and the safe assumption for a node that has never been
told otherwise.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

V1 = "v1"
V2 = "v2"
SUPPORTED = (V1, V2)

DEFAULT_STATE_PATH = Path.home() / ".minos" / "scoring_version.json"


def scorer_name(version: Any) -> str:
    """The label written into a score payload for a given scoring version.

    Defined here, beside V1/V2, because it is a fact about scoring versions
    rather than about the validator that happens to stamp it. One mapping, used
    both where a score is written and where an incoming score is checked -- two
    copies would drift, and a drifted copy means scores get compared against a
    label nothing produces.

    Anything not recognisably V2 maps to the v1 label. Defaulting the other way
    would label a v1 number AdvancedV2, which is exactly the confusion the label
    exists to prevent.

    The v1 label is "Advanced", matching what every deployed validator already
    writes, because v1 IS that formula -- identical arithmetic, verified against
    minos_subnet main. A new spelling for the same numbers would make the
    platform's consensus marker read "mixed" on any fleet that is not upgrading
    in lockstep, reporting a blend of two scales where there is only one.
    """
    return "AdvancedV2" if version == V2 else "Advanced"


def state_path() -> Path:
    override = os.getenv("MINOS_SCORING_VERSION_STATE")
    return Path(override) if override else DEFAULT_STATE_PATH


def normalise(value: Any) -> Optional[str]:
    """A recognised version, or None.

    None means "the platform said nothing usable", which is different from "the
    platform said v1" — the caller keeps its previous version rather than
    treating an unparseable answer as an instruction to change.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    return text if text in SUPPORTED else None


def read_last_used(path: Optional[Path] = None) -> Optional[str]:
    """The last version this validator actually scored with, or None."""
    target = path or state_path()
    try:
        with open(target, "r", encoding="utf-8") as fh:
            return normalise(json.load(fh).get("scoring_version"))
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def record_used(version: str, path: Optional[Path] = None) -> None:
    """Persist the version being used, atomically.

    Written via a temp file and os.replace so a crash mid-write cannot leave a
    truncated file — which read_last_used would treat as "never resolved" and
    silently drop the validator back to v1.
    """
    if normalise(version) is None:
        return
    target = path or state_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"scoring_version": version}, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        # Not fatal: the validator scores this round with the version it just
        # resolved. It only means a restart cannot remember it.
        pass


def resolve(network_config: Any, *, path: Optional[Path] = None, logger=None) -> str:
    """The version to score with, given whatever the platform returned.

    ``network_config`` is the parsed response, or None when the fetch failed.
    Persists whatever it settles on, so the next unreachable-platform round
    reuses it.
    """
    advertised = None
    if isinstance(network_config, dict):
        advertised = normalise(network_config.get("scoring_version"))

    if advertised is not None:
        previous = read_last_used(path)
        if previous != advertised:
            if previous is not None and logger:
                logger.warning(
                    f"Scoring version changed by the platform: {previous} -> {advertised}. "
                    f"Scores from this round are on a different scale to the last."
                )
            # Only on a change. Rewriting the same value every round is an
            # fsync per round for a file that has not moved.
            record_used(advertised, path)
        return advertised

    remembered = read_last_used(path)
    if remembered is not None:
        if logger:
            logger.warning(
                f"Platform advertised no usable scoring version; continuing with "
                f"{remembered}, the last one used. Falling back to {V1} here would "
                f"diverge from a fleet that already moved on."
            )
        return remembered

    if logger:
        logger.warning(
            f"No scoring version from the platform and none remembered; using {V1}."
        )
    return V1
