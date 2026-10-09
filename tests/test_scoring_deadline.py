"""Enforcing the scoring deadline.

Three things decide whether a round's weights go out on time:

  the lead          stop starting new secondary work before the deadline
  the submit window stop starting anything once a result cannot be stored
  the reaper        stop work already RUNNING at that same point

The first two only govern what is started. Without the third, one job holds the
round open for as long as it runs and keeps its CPU away from the next round.
"""
import ast
import asyncio
import contextvars
import datetime as dt
import pathlib
import subprocess

import pytest

from templates import _common
from utils.subset_scoring import (
    DEFAULT_CUTOFF_LEAD_SECONDS,
    scoring_window_closed,
    should_stop_secondary_scoring,
)

GRACE = 60


def deadline_in(seconds: float) -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)


def _src(rel: str) -> str:
    return (pathlib.Path(__file__).resolve().parents[1] / rel).read_text()


@pytest.fixture(autouse=True)
def _fresh_registry():
    """Each test starts outside any round with an empty registry and leaves it
    that way, so a round begun in one test cannot affect another."""
    ctx = _common._job_round.set(None)
    _common._open_round = None
    _common._live_containers.clear()
    yield
    _common._job_round.reset(ctx)
    _common._open_round = None
    _common._live_containers.clear()


def _register(prefix: str) -> str:
    """A registered container, as run_container holds one while it runs."""
    name = _common.container_name(prefix)
    _common._live_containers.add(name)
    return name


def _docker_run(prefix: str = "gatk") -> list:
    return ["docker", "run", "--rm", "--name", _common.container_name(prefix), "img"]


class FakeDocker:
    """Stands in for the docker CLI and tracks which containers exist, which is
    what decides whether one escaped. A hook runs inside its call before docker
    would act on it, to place a reap at an exact point in a launch."""

    def __init__(self, before_create=None, before_start=None):
        self.created, self.started, self.calls = set(), [], []
        self.before_create, self.before_start = before_create, before_start

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[:3] == ["docker", "rm", "-f"]:
            gone = [n for n in cmd[3:] if n in self.created]
            self.created.difference_update(gone)
            return subprocess.CompletedProcess(cmd, 0, "\n".join(gone), "")
        if cmd[:2] == ["docker", "create"]:
            name = cmd[cmd.index("--name") + 1]
            if self.before_create:
                self.before_create(name)
            self.created.add(name)
            return subprocess.CompletedProcess(cmd, 0, "id\n", "")
        if cmd[:3] == ["docker", "start", "-a"]:
            name = cmd[3]
            if self.before_start:
                self.before_start(name)
            if name not in self.created:
                return subprocess.CompletedProcess(cmd, 1, "", f"No such container: {name}")
            self.started.append(name)
            self.created.discard(name)          # --rm: removed once it exits
            return subprocess.CompletedProcess(cmd, 0, "out", "")
        if cmd[:2] == ["docker", "run"]:
            self.started.append(cmd[cmd.index("--name") + 1])
            return subprocess.CompletedProcess(cmd, 0, "out", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


# --- the lead ---------------------------------------------------------------

@pytest.mark.parametrize("remaining,stop", [
    (3600, False), (600, False), (421, False), (419, True), (0, True), (-120, True),
])
def test_lead_stops_new_secondary_work(remaining, stop):
    assert should_stop_secondary_scoring(deadline_in(remaining)) is stop


def test_lead_default_is_seven_minutes():
    assert DEFAULT_CUTOFF_LEAD_SECONDS == 420


def test_lead_is_not_read_from_the_environment():
    """subset_scoring is imported before load_dotenv(), so an env read there
    would miss .env on a manual launch."""
    assert "getenv" not in _src("utils/subset_scoring.py")
    val = _src("neurons/validator.py")
    assert val.index("load_dotenv()") < val.index('os.getenv("SCORE_CUTOFF_LEAD_SECONDS"')


# --- the submit window ------------------------------------------------------

@pytest.mark.parametrize("remaining,closed", [
    (420, False), (0, False), (-59, False), (-61, True), (-600, True),
])
def test_window_closes_only_after_grace(remaining, closed):
    assert scoring_window_closed(deadline_in(remaining), GRACE) is closed


@pytest.mark.parametrize("fn,extra", [
    (should_stop_secondary_scoring, ()), (scoring_window_closed, (GRACE,)),
])
def test_both_checks_fail_open_without_a_deadline(fn, extra):
    assert fn(None, *extra) is False


# --- the reaper -------------------------------------------------------------

def test_a_running_container_is_registered_and_released_after(monkeypatch):
    seen = []
    docker = FakeDocker(before_start=lambda name: seen.append(
        (name in _common._live_containers, name in docker.created)))
    monkeypatch.setattr(subprocess, "run", docker)
    _common.begin_round()
    _common.run_container(_docker_run())
    assert seen == [(True, True)], "registered while it runs, and it already exists"
    assert _common._live_containers == set(), "released once it returns"


def test_reaping_removes_every_registered_container(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        # docker echoes each name it removed; that is what the count reads.
        return subprocess.CompletedProcess(cmd, 0, "\n".join(cmd[3:]), "")

    monkeypatch.setattr(_common.subprocess, "run", fake_run)
    a, b = _register("gatk"), _register("bcftools")

    assert _common.reap_live_containers() == 2
    assert len(calls) == 1, "one call for the whole set"
    assert calls[0][:3] == ["docker", "rm", "-f"]
    assert sorted(calls[0][3:]) == sorted([a, b])
    # Drained, so a second pass is a no-op rather than a double reap.
    assert _common.reap_live_containers() == 0


def test_reaping_never_raises(monkeypatch):
    """Cleanup runs on the deadline path; an exception there would lose the
    round it is trying to protect."""
    def boom(cmd, **kw):
        raise OSError("docker is gone")

    monkeypatch.setattr(_common.subprocess, "run", boom)
    _register("gatk")
    assert _common.reap_live_containers() == 0


def test_the_registry_is_process_local():
    """A validator and a miner on one box both import this module. Neither may
    reap the other's containers, so the registry must not be shared state."""
    src = _src("templates/_common.py")
    assert "_live_containers" in src
    assert "threading.Lock()" in src, "jobs run in a thread pool"
    for shared in ("docker ps", "--filter", "redis", "open(", "Path("):
        assert shared not in src.split("def count_variants")[0], (
            f"registry must not reach outside the process ({shared})"
        )


# --- wiring -----------------------------------------------------------------

def test_the_reaper_is_started_and_cancelled():
    src = _src("neurons/validator.py")
    assert "reaper = asyncio.create_task(_reap_past_the_window())" in src
    assert "reaper.cancel()" in src
    assert src.index("reaper = asyncio.create_task") < src.index("reaper.cancel()")


def test_the_reaper_waits_for_the_window_not_the_lead():
    """Reaping at the lead would kill work that could still finish and be
    stored. The only safe instant is when the result stops being storable."""
    tree = ast.parse(_src("neurons/validator.py"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_reap_past_the_window")
    calls = [c.func.id for c in ast.walk(fn)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)]
    assert "scoring_window_closed" in calls
    assert "should_stop_secondary_scoring" not in calls
    assert "reap_live_containers" in [
        c.func.id for c in ast.walk(fn)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
    ] or "reap_live_containers" in ast.dump(fn)


def test_the_window_check_stops_the_job():
    """A source match would not notice a deleted `return`."""
    guards = [n for n in ast.walk(ast.parse(_src("neurons/validator.py")))
              if isinstance(n, ast.If) and isinstance(n.test, ast.Call)
              and getattr(n.test.func, "id", None) == "scoring_window_closed"]
    assert len(guards) == 1
    assert isinstance(guards[0].body[-1], ast.Return)


def test_the_validator_ceiling_is_its_own_constant():
    """GENOMICS_CONFIG["variant_calling_timeout"] is also read by
    neurons/miner.py; lowering it there would change miner behaviour."""
    val = _src("neurons/validator.py")
    assert 'os.getenv("VALIDATOR_VARIANT_CALLING_TIMEOUT_SECONDS"' in val
    assert '"timeout": VALIDATOR_VARIANT_CALLING_TIMEOUT_SECONDS,' in val
    assert 'GENOMICS_CONFIG.get("variant_calling_timeout"' in _src("neurons/miner.py")


def test_the_ceiling_exceeds_the_lead():
    """Otherwise the lead, not the ceiling, is what bounds a job -- and the
    ceiling would be killing work the lead had already made room for."""
    val = _src("neurons/validator.py")
    ceiling = int(val.split('VALIDATOR_VARIANT_CALLING_TIMEOUT_SECONDS", "')[1].split('"')[0])
    assert ceiling > DEFAULT_CUTOFF_LEAD_SECONDS


# --- the round always reaches finalization ---------------------------------

def test_scoring_is_bounded_by_the_window_not_by_the_jobs():
    """The invariant: stop, submit what landed, backfill, set weights -- on
    every round. A job that will not end must not be able to prevent it, so the
    scoring phase carries its own deadline rather than trusting each job."""
    src = _src("neurons/validator.py")
    assert "await asyncio.wait_for(_run_scoring(), timeout=max(0.0, budget))" in src
    assert "budget = seconds_until_deadline(scoring_deadline) + grace_seconds" in src


def test_a_timed_out_scoring_phase_still_finalizes():
    """TimeoutError must be handled, not propagate: the whole point is that the
    round goes on to publish."""
    tree = ast.parse(_src("neurons/validator.py"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_score_round_submissions")
    handlers = [h for h in ast.walk(fn) if isinstance(h, ast.ExceptHandler)
                and h.type is not None
                and "TimeoutError" in ast.dump(h.type)]
    assert handlers, "a scoring-phase timeout is not caught"
    for h in handlers:
        assert not any(isinstance(n, ast.Raise) for n in h.body), \
            "a scoring timeout must not abort the round"
        assert not any(isinstance(n, ast.Return) for n in h.body), \
            "a scoring timeout must fall through to finalization"


def test_finalization_follows_the_scoring_phase_unconditionally():
    """No early return between the end of scoring and the finalize call."""
    src = _src("neurons/validator.py")
    end = src.index("reaper.cancel()")
    fin = src.index("--- Steps 5 & 6: Backfill + finalize ---")
    assert end < fin
    assert "return" not in src[end:fin], "an early exit skips finalization"


def test_the_window_budget_is_never_negative():
    """A round picked up after its window has closed gets timeout=0, not a
    negative one, which asyncio rejects."""
    assert "max(0.0, budget)" in _src("neurons/validator.py")


def test_a_platform_outage_still_blocks_weights():
    """Deliberately unchanged. Without backfill the miner set is incomplete and
    the reward policy unknown, so publishing would mean divergent weights --
    worse than publishing nothing."""
    src = _src("neurons/validator.py")
    assert "Skipping weight submission to avoid validator divergence." in src
    assert "to avoid stale reward policy" in src


# --- the reaper must cover the whole scoring stage --------------------------

def test_every_scoring_stage_container_is_named_and_registered():
    """Variant calling is not the only thing holding CPU at the deadline.

    hap.py and its slice/index prep run in their own containers. Unnamed, they
    cannot be removed at all -- a subprocess timeout kills the docker CLI, not
    the container -- so the round could finalize while they kept running.
    """
    src = _src("utils/scoring.py")
    runs = src.count('"docker", "run", "--rm"')
    named = src.count('"docker", "run", "--rm", "--name"')
    assert runs == named, f"{runs - named} scoring-stage container(s) still unnamed"
    assert "from templates._common import container_name, run_container" in src


def test_every_container_launch_goes_through_run_container():
    """A launch that bypasses run_container is invisible to the reap: nothing
    registers it and nothing checks its round."""
    for rel in ("templates/gatk.py", "templates/bcftools.py", "templates/freebayes.py",
                "templates/deepvariant.py", "utils/scoring.py"):
        src = _src(rel)
        assert "subprocess.run(" not in src, f"{rel} launches a container directly"
        assert src.count("run_container(") == src.count("container_name("), rel


def test_no_scoring_stage_name_is_used_before_it_is_assigned():
    """A name declared inside one branch of an if/else is unbound in the other."""
    import ast

    tree = ast.parse(_src("utils/scoring.py"))
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        stored = {n.id for n in ast.walk(fn)
                  if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        loaded = {n.id for n in ast.walk(fn)
                  if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        for v in ("_slice_name", "_index_name", "_happy_name"):
            assert not (v in loaded and v not in stored), f"{fn.name} uses unbound {v}"


# --- cleanup must not itself delay finalization -----------------------------

def test_a_finished_job_leaves_the_live_set(monkeypatch):
    """Otherwise the set is every container the round ever started, and the
    reap spends its budget on names that exited minutes ago."""
    monkeypatch.setattr(subprocess, "run", FakeDocker())
    _common.begin_round()
    _common.run_container(_docker_run())
    assert _common._live_containers == set()


def test_reaping_a_finished_job_costs_nothing(monkeypatch):
    docker = FakeDocker()
    monkeypatch.setattr(subprocess, "run", docker)
    _common.begin_round()
    for _ in range(50):
        _common.run_container(_docker_run())
    assert _common.reap_live_containers() == 0
    assert not [c for c in docker.calls if c[:2] == ["docker", "rm"]], (
        "released names must not reach docker"
    )


def test_the_reap_is_one_bounded_call_not_one_per_name(monkeypatch):
    """Finalization waits for this. N removals at 30s each is unbounded in N;
    docker takes many names at once, so the budget covers the operation."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw.get("timeout")))
        return subprocess.CompletedProcess(cmd, 0, "\n".join(cmd[3:]), "")

    monkeypatch.setattr(_common.subprocess, "run", fake_run)
    names = [_register("gatk") for _ in range(40)]

    assert _common.reap_live_containers() == 40
    assert len(calls) == 1, "one docker call for the whole set"
    cmd, timeout = calls[0]
    assert cmd[:3] == ["docker", "rm", "-f"]
    assert sorted(cmd[3:]) == sorted(names)
    assert timeout == _common.REAP_BUDGET_SECONDS


def test_the_reap_budget_is_short_enough_to_wait_on():
    """It sits between the deadline and weight submission."""
    assert 0 < _common.REAP_BUDGET_SECONDS <= 30


def test_a_slow_docker_cannot_hold_the_round(monkeypatch):
    def hang(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 0))

    monkeypatch.setattr(_common.subprocess, "run", hang)
    _register("gatk")
    assert _common.reap_live_containers() == 0   # returns, does not raise
    assert _common._live_containers == set(), "the set is drained even on timeout"


# --- a container starts only while its round is open ------------------------

def test_a_reap_during_create_leaves_nothing_running(monkeypatch):
    """The reap can land while `docker create` is still in flight. The name is
    registered only once the container exists, and the round is checked again
    before it starts, so the late container is removed instead of run."""
    docker = FakeDocker(before_create=lambda name: _common.reap_live_containers())
    monkeypatch.setattr(subprocess, "run", docker)
    _common.begin_round()
    with pytest.raises(_common.RegistryClosed):
        _common.run_container(_docker_run())
    assert docker.started == []
    assert docker.created == set(), "the late container is removed, not left behind"


def test_a_reap_between_create_and_start_stops_it(monkeypatch):
    docker = FakeDocker(before_start=lambda name: _common.reap_live_containers())
    monkeypatch.setattr(subprocess, "run", docker)
    _common.begin_round()
    result = _common.run_container(_docker_run())
    assert result.returncode != 0
    assert docker.started == [] and docker.created == set()


def test_a_worker_from_an_earlier_round_cannot_start_a_container(monkeypatch):
    """A job's thread outlives the cancellation of its round. It keeps that
    round, and is refused once a later one has begun -- reaped or not."""
    docker = FakeDocker()
    monkeypatch.setattr(subprocess, "run", docker)
    _common.begin_round()
    earlier = contextvars.copy_context()        # a worker started in that round
    _common.reap_live_containers()
    _common.begin_round()
    with pytest.raises(_common.RegistryClosed):
        earlier.run(_common.run_container, _docker_run("happy"))
    _common.begin_round()                        # and with no reap in between
    with pytest.raises(_common.RegistryClosed):
        earlier.run(_common.run_container, _docker_run("happy"))
    assert docker.started == [] and docker.created == set()
    assert not [c for c in docker.calls if c[:2] == ["docker", "create"]], (
        "refused before docker is asked to create anything"
    )

    _common.run_container(_docker_run("happy"))  # the current round is unaffected
    assert len(docker.started) == 1


def test_outside_a_round_the_command_runs_unchanged(monkeypatch):
    """Miners share these templates and never begin a round."""
    docker = FakeDocker()
    monkeypatch.setattr(subprocess, "run", docker)
    cmd = _docker_run()
    _common.run_container(cmd, capture_output=True, text=True)
    assert docker.calls == [cmd]
    assert _common._live_containers == set()


def test_a_failed_create_returns_like_docker_run(monkeypatch):
    def fake_run(cmd, **kw):
        assert cmd[:2] == ["docker", "create"], f"nothing runs after a failed create: {cmd[:3]}"
        return subprocess.CompletedProcess(cmd, 125, "", "Unable to find image")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _common.begin_round()
    cmd = _docker_run()
    result = _common.run_container(cmd, capture_output=True, text=True)
    assert (result.returncode, result.stderr, result.args) == (125, "Unable to find image", cmd)
    assert _common._live_containers == set()


def test_a_timed_out_container_is_removed(monkeypatch):
    """The timeout stops the attached CLI, not the container."""
    def hang(name):
        raise subprocess.TimeoutExpired(["docker", "start", "-a", name], 1)

    docker = FakeDocker(before_start=hang)
    monkeypatch.setattr(subprocess, "run", docker)
    _common.begin_round()
    with pytest.raises(subprocess.TimeoutExpired):
        _common.run_container(_docker_run(), timeout=1)
    assert docker.created == set()
    assert _common._live_containers == set()


def test_the_registry_closes_inside_the_reap_lock():
    """Ended outside that lock, a container could register after the snapshot
    and before the round ends -- missed by the reap and never refused."""
    fn = next(n for n in ast.walk(ast.parse(_src("templates/_common.py")))
              if isinstance(n, ast.FunctionDef) and n.name == "reap_live_containers")
    locked = next(n for n in ast.walk(fn) if isinstance(n, ast.With)
                  and ast.unparse(n.items[0].context_expr) == "_live_lock")
    body = [ast.unparse(s) for s in locked.body]
    snapshot = next(i for i, s in enumerate(body) if "sorted(_live_containers)" in s)
    assert "_open_round = None" in body[:snapshot]


def test_a_job_reaped_mid_index_does_not_go_on_to_start_hap_py(tmp_path, monkeypatch):
    """Docker stubbed. The round is reaped while the truth file is indexed: the
    index container is killed, a failed index is not fatal, and the thread
    moves on to hap.py -- which must not be created or started after the reap."""
    from utils import scoring

    for name in ("truth.vcf.gz", "ref.fa", "muts.vcf.gz"):
        (tmp_path / name).write_text("")
    (tmp_path / "query.vcf").write_text("chr20\t100\t.\tA\tG\t50\tPASS\t.\n")
    (tmp_path / "ref.sdf").mkdir()
    reaped, after_reap = [], []

    def fake_run(cmd, **kw):
        if cmd[:3] == ["docker", "rm", "-f"]:
            reaped.append(cmd[3:])
            return subprocess.CompletedProcess(cmd, 0, "\n".join(cmd[3:]), "")
        if reaped and cmd[:2] in (["docker", "create"], ["docker", "start"]):
            after_reap.append(cmd)
        if cmd[:2] == ["docker", "create"]:
            if "view" in cmd:                   # slice: write what it promises
                (tmp_path / pathlib.Path(cmd[cmd.index("-o") + 1]).name).write_text("")
            return subprocess.CompletedProcess(cmd, 0, "id\n", "")
        if cmd[:3] == ["docker", "start", "-a"]:
            if cmd[3].startswith("minos-happy-index-"):
                _common.reap_live_containers()  # the round is reaped right now
                return subprocess.CompletedProcess(cmd, 137, "", "killed")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 1, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _common.begin_round()
    result = scoring.HappyScorer().score_vcf(
        truth_vcf=str(tmp_path / "truth.vcf.gz"), query_vcf=str(tmp_path / "query.vcf"),
        reference_fasta=str(tmp_path / "ref.fa"), region="chr20:1-1000",
        reference_sdf=str(tmp_path / "ref.sdf"), mutations_vcf=str(tmp_path / "muts.vcf.gz"),
    )
    assert reaped, "the scenario must reap mid-index"
    assert after_reap == [], "a container was created or started after the reap"
    assert result is None, "no score, so the miner is left for backfill"


def test_the_round_reaches_the_worker_thread():
    """Jobs run through asyncio.to_thread, which carries the round into the
    worker thread; run_in_executor would leave it behind."""
    async def scenario():
        round_id = _common.begin_round()
        return round_id, await asyncio.to_thread(_common._job_round.get)

    round_id, seen = asyncio.run(scenario())
    assert seen == round_id


def test_job_launches_carry_their_round():
    tree = ast.parse(_src("neurons/validator.py"))
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
    for fn_name, target in (("_run_miner_tool", "variant_call"),
                            ("_score_single_miner", "score_vcf")):
        fn = fns[fn_name]
        assert "run_in_executor" not in ast.unparse(fn), f"{fn_name} would drop the round"
        assert any(
            isinstance(c, ast.Call) and ast.unparse(c.func) == "asyncio.to_thread"
            and c.args and ast.unparse(c.args[0]).endswith(target)
            for c in ast.walk(fn)
        ), f"{fn_name} must launch {target} through asyncio.to_thread"


def test_each_round_begins_before_any_job_starts():
    """Placed beside the reaper so it runs in both modes and before _run_scoring
    is ever awaited; otherwise jobs would carry the previous round's id."""
    fn = next(n for n in ast.walk(ast.parse(_src("neurons/validator.py")))
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_score_round_submissions")
    block = next(
        [ast.unparse(s) for s in n.body] for n in ast.walk(fn)
        if isinstance(getattr(n, "body", None), list)
        and any(ast.unparse(s).startswith("reaper = asyncio.create_task") for s in n.body)
    )
    assert "begin_round()" in block, "the round must begin beside the reaper, unconditionally"
    reaper = next(i for i, s in enumerate(block) if s.startswith("reaper = asyncio.create_task"))
    assert block.index("begin_round()") < reaper
