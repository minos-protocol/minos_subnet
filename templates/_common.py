"""Shared helpers for variant-calling templates."""
import contextvars
import gzip
import logging
import subprocess
import threading
import time
import uuid
import zlib
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


# Containers this process is running for a scoring round, by name.
#
# Process-local on purpose: a validator and a miner on one box both import this
# module, in separate processes, and neither may reap the other's work.
_live_containers: "set[str]" = set()
_live_lock = threading.Lock()

# The scoring round a job belongs to, carried by the job into its worker thread
# (see begin_round). None outside a round, which is every miner launch.
_job_round: "contextvars.ContextVar[Optional[int]]" = contextvars.ContextVar(
    "minos_job_round", default=None
)
_rounds = 0
# The round whose jobs may start containers. Cleared by a reap and replaced when
# the next round begins, so a job from any earlier round is refused.
_open_round: Optional[int] = None


class RegistryClosed(RuntimeError):
    """Raised in place of starting a container for a round that has ended."""


def begin_round() -> int:
    """Open a scoring round and tie the jobs started from here on to it.

    Sets the round in the calling context. Tasks created after this call, and
    the asyncio.to_thread calls they make, carry it into their worker threads.
    A worker left over from an earlier round keeps that earlier round, and is
    refused once it is no longer the open one -- whether or not that round was
    reaped.
    """
    global _rounds, _open_round
    with _live_lock:
        _rounds += 1
        _open_round = round_id = _rounds
    _job_round.set(round_id)
    return round_id


def container_name(prefix: str) -> str:
    """Build a unique container name for one `docker run`.

    subprocess.run's timeout kills the docker CLI, not the container it
    started; without a name there is no handle to remove a container that is
    still holding its `--cpus`/`--memory` reservation. Launch the command with
    run_container, which registers the name once the container exists.
    """
    return f"minos-{prefix}-{uuid.uuid4().hex[:12]}"


def _remaining(deadline: Optional[float]) -> Optional[float]:
    return None if deadline is None else max(0.0, deadline - time.monotonic())


def run_container(cmd: List[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a `docker run --name ...` command the way subprocess.run would.

    Inside a scoring round the container is created first, registered only if
    its round is still open, and then started attached. Registration and the
    reap take the same lock, so every registered name refers to a container
    that already exists -- a reap cannot miss one -- and a container is never
    started once its round has ended. Exit status, output and the overall
    timeout match `docker run`.

    Outside a round the command runs unchanged.
    """
    round_id = _job_round.get()
    if round_id is None:
        return subprocess.run(cmd, **kwargs)

    if cmd[:2] != ["docker", "run"] or "--name" not in cmd:
        raise ValueError("run_container expects a `docker run --name ...` command")
    name = cmd[cmd.index("--name") + 1]

    with _live_lock:
        if _open_round != round_id:
            raise RegistryClosed(f"round {round_id} has ended; not starting {name}")

    timeout = kwargs.get("timeout")
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        created = subprocess.run(
            ["docker", "create", *cmd[2:]],
            capture_output=True, text=True, timeout=_remaining(deadline),
        )
    except subprocess.TimeoutExpired:
        # Whether or not the daemon finished creating it, it was never started.
        reap_container(name)
        raise
    if created.returncode != 0:
        as_text = any(kwargs.get(k) for k in ("text", "universal_newlines", "encoding"))
        err = created.stderr if as_text else created.stderr.encode()
        return subprocess.CompletedProcess(cmd, created.returncode, "" if as_text else b"", err)

    with _live_lock:
        still_open = _open_round == round_id
        if still_open:
            _live_containers.add(name)
    if not still_open:
        # The round ended while the container was being created.
        reap_container(name)
        raise RegistryClosed(f"round {round_id} has ended; not starting {name}")

    try:
        result = subprocess.run(
            ["docker", "start", "-a", name],
            **{**kwargs, "timeout": _remaining(deadline)},
        )
    except BaseException:
        # A timeout stops the attached CLI, not the container.
        reap_container(name)
        raise
    finally:
        release_container(name)
    result.args = cmd
    return result


def release_container(name: str) -> None:
    """Drop a finished container from the live set.

    Called when a job ends, however it ends. Without it the set is every
    container the round ever started rather than the few still running, and
    reap_live_containers then spends its budget on names that exited minutes
    ago -- while finalization waits for it.
    """
    if not name:
        return
    with _live_lock:
        _live_containers.discard(name)


# Whole-operation budget for a reap. This runs on the path to finalization, so
# it is bounded as a unit: docker being slow must cost the round seconds, not
# however long N removals take one after another.
REAP_BUDGET_SECONDS = 20


def reap_live_containers(budget_seconds: int = REAP_BUDGET_SECONDS) -> int:
    """Force-remove every container this process still has running.

    For a caller past the point where its results can still be used: the work
    is already unusable, and until the container exits it holds CPU and memory
    the next round needs.

    One `docker rm -f` for the whole set, not one per name. Removal is the
    slow part and docker takes many names at once, so the budget covers the
    operation rather than each name -- the difference between a bounded pause
    and N times the per-call timeout.

    Also ends the open round, so nothing is started for it afterwards.
    """
    global _open_round
    with _live_lock:
        # Ended under the same lock as the snapshot: a container is either
        # registered here, and removed below, or refused by run_container.
        _open_round = None
        names = sorted(_live_containers)
        _live_containers.clear()
    if not names:
        return 0
    try:
        result = subprocess.run(
            ["docker", "rm", "-f", *names],
            capture_output=True, text=True, timeout=budget_seconds,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "Reaping %d container(s) exceeded %ss; leaving the rest to docker",
            len(names), budget_seconds,
        )
        return 0
    except Exception as e:  # noqa: BLE001 - cleanup must never raise
        logger.warning("Could not reap %d container(s): %s", len(names), e)
        return 0
    # Names already gone are reported on stderr and do not count; docker still
    # removes the ones that were live, so a partial failure is not an error.
    removed = len([ln for ln in result.stdout.splitlines() if ln.strip()])
    if removed:
        logger.warning("Reaped %d running container(s)", removed)
    return removed


def reap_container(name: str) -> None:
    """Force-remove a container by name. Safe if it is already gone."""
    if not name:
        return
    release_container(name)
    try:
        result = subprocess.run(
            ["docker", "rm", "-f", name],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            logger.warning("Reaped orphaned container %s", name)
    except Exception as e:  # noqa: BLE001 - cleanup must never mask the real error
        logger.warning("Could not reap container %s: %s", name, e)


def count_variants(vcf_path: Path) -> int:
    """Count non-header lines in a VCF file."""
    count = 0
    try:
        opener = gzip.open if str(vcf_path).endswith(".gz") else open
        with opener(vcf_path, "rt") as f:
            for line in f:
                if not line.startswith("#"):
                    count += 1
    except (OSError, EOFError, zlib.error, UnicodeDecodeError):
        # A truncated .vcf.gz raises EOFError or zlib.error mid-stream rather
        # than BadGzipFile; an unreadable callset counts as zero here instead
        # of raising out of the template.
        logger.warning("Failed to count variants in %s", vcf_path)
    return count
