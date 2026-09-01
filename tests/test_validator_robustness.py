"""Regression tests for validator failure paths that silently cost rounds.

Each of these was a quiet loss, not a crash:

* a malformed-but-successful assignment payload escaped the no-assignment
  fallback, so the round was retried forever and never scored;
* a missing ``submission_count`` in the round listing was read as zero, which
  marked the round scored without ever scoring it;
* weight history was reported over the unfiltered tracked miners, so the
  dashboard credited hotkeys the chain was never paying;
* the inter-round wait lived inside the main loop's try, so a subtensor outage
  collapsed the ~hour interval into a 10s retry sleep.
"""

import asyncio
import importlib
import sys
import types

import pytest


ROUND_ID = "2026-01-21T12:00:00+00:00"


def _noop(*args, **kwargs):
    return None


def _import_validator_with_runtime_stubs(monkeypatch):
    """Import neurons.validator without requiring a live Bittensor runtime."""
    logging_stub = types.SimpleNamespace(
        debug=_noop,
        error=_noop,
        info=_noop,
        warning=_noop,
        set_debug=_noop,
        set_trace=_noop,
    )
    bittensor_stub = types.SimpleNamespace(
        Config=object,
        Subtensor=object,
        Wallet=object,
        config=lambda parser=None: types.SimpleNamespace(),
        logging=logging_stub,
        subtensor=object,
        wallet=object,
    )
    httpx_stub = types.SimpleNamespace(
        AsyncClient=object,
        TimeoutException=Exception,
        ConnectError=Exception,
        ReadError=Exception,
    )

    monkeypatch.setitem(sys.modules, "bittensor", bittensor_stub)
    monkeypatch.setitem(sys.modules, "bittensor_wallet", types.SimpleNamespace(Keypair=object))
    monkeypatch.setitem(sys.modules, "dotenv", types.SimpleNamespace(load_dotenv=_noop))
    monkeypatch.setitem(sys.modules, "httpx", httpx_stub)
    monkeypatch.setitem(sys.modules, "numpy", types.SimpleNamespace())
    sys.modules.pop("neurons.validator", None)
    return importlib.import_module("neurons.validator")


@pytest.fixture
def validator_module(monkeypatch):
    module = _import_validator_with_runtime_stubs(monkeypatch)
    yield module
    sys.modules.pop("neurons.validator", None)


async def _snapshot():
    return {"hotkeys": ["hk_burn", "hk_live", "hk_permit"],
            "permits": [False, False, True],
            "owners": {}, "synced": True}


class TestAssignmentFallback:
    """A bad assignment payload must degrade to scoring everyone.

    Before the fix the handler caught only PlatformClientError, so a null
    primary_miner_hotkeys (TypeError in set()) or a non-ISO scoring_deadline
    (ValueError in fromisoformat) fell through to the method's catch-all, which
    returns False — the round was never scored and was retried forever.
    """

    def _run(self, validator_module, assignment):
        reached_step_two = []

        async def get_assignment(round_id):
            return assignment

        async def get_round_submissions(round_id, scoring_version=None):
            reached_step_two.append(round_id)
            return {"submissions": [], "region": "chr20:1-1000"}

        async def _declared_scoring_version(round_id):
            # The real method is best-effort and may return None; these tests
            # exercise assignment fallback, not version declaration.
            return None

        validator = types.SimpleNamespace(
            platform_client=types.SimpleNamespace(
                get_assignment=get_assignment,
                get_round_submissions=get_round_submissions,
            ),
            _declared_scoring_version=_declared_scoring_version,
            _adopt_round_pin=lambda round_id, pinned: None,
        )
        result = asyncio.run(
            validator_module.Validator._score_round_submissions(validator, ROUND_ID)
        )
        return result, reached_step_two

    def test_null_primary_hotkeys_takes_the_fallback(self, validator_module):
        result, reached_step_two = self._run(
            validator_module,
            {
                "primary_miner_hotkeys": None,
                "secondary_miner_hotkeys": [],
                "scoring_deadline": None,
            },
        )
        assert reached_step_two == [ROUND_ID]
        assert result is False  # no submissions in this round, but it was attempted

    def test_non_iso_deadline_takes_the_fallback(self, validator_module):
        result, reached_step_two = self._run(
            validator_module,
            {
                "primary_miner_hotkeys": ["hk_a"],
                "secondary_miner_hotkeys": [],
                "scoring_deadline": "not-a-timestamp",
            },
        )
        assert reached_step_two == [ROUND_ID]
        assert result is False

    def test_platform_error_still_takes_the_fallback(self, validator_module):
        async def get_assignment(round_id):
            raise validator_module.PlatformClientError("assignment endpoint down")

        reached_step_two = []

        async def get_round_submissions(round_id, scoring_version=None):
            reached_step_two.append(round_id)
            return {"submissions": [], "region": "chr20:1-1000"}

        async def _declared_scoring_version(round_id):
            # The real method is best-effort and may return None; these tests
            # exercise assignment fallback, not version declaration.
            return None

        validator = types.SimpleNamespace(
            platform_client=types.SimpleNamespace(
                get_assignment=get_assignment,
                get_round_submissions=get_round_submissions,
            ),
            _declared_scoring_version=_declared_scoring_version,
            _adopt_round_pin=lambda round_id, pinned: None,
        )
        asyncio.run(
            validator_module.Validator._score_round_submissions(validator, ROUND_ID)
        )
        assert reached_step_two == [ROUND_ID]

    def test_valid_assignment_still_proceeds(self, validator_module):
        result, reached_step_two = self._run(
            validator_module,
            {
                "primary_miner_hotkeys": ["hk_a", "hk_b"],
                "secondary_miner_hotkeys": ["hk_c"],
                "scoring_deadline": "2026-01-21T13:00:00Z",
            },
        )
        assert reached_step_two == [ROUND_ID]
        assert result is False


class TestUnknownSubmissionCount:
    """An absent submission_count is UNKNOWN, never zero.

    The zero branch is the only path that adds a round to scored_rounds without
    _score_round_submissions confirming finalization, so a renamed listing field
    or a lagging read replica permanently zeroed this validator's contribution to
    the round with no error anywhere.
    """

    def _run(self, validator_module, round_info, score_result=True):
        scored_calls = []

        async def get_scoring_rounds():
            return {
                "scoring_rounds": [round_info],
                "next_scoring_window_start": "2026-01-21T13:00:00+00:00",
            }

        async def _score_round_submissions(round_id):
            scored_calls.append(round_id)
            return score_result

        validator = types.SimpleNamespace(
            platform_client=types.SimpleNamespace(get_scoring_rounds=get_scoring_rounds),
            scored_rounds=set(),
            _score_round_submissions=_score_round_submissions,
        )
        result = asyncio.run(validator_module.Validator.score_platform_rounds(validator))
        return validator, scored_calls, result

    def test_missing_field_is_scored_not_skipped(self, validator_module):
        validator, scored_calls, _ = self._run(
            validator_module, {"round_id": ROUND_ID, "region": "chr20:1-1000"}
        )
        assert scored_calls == [ROUND_ID]
        assert validator.scored_rounds == {ROUND_ID}

    def test_explicit_null_is_scored_not_skipped(self, validator_module):
        validator, scored_calls, _ = self._run(
            validator_module, {"round_id": ROUND_ID, "submission_count": None}
        )
        assert scored_calls == [ROUND_ID]

    def test_unknown_count_that_fails_to_finalize_is_retried(self, validator_module):
        validator, scored_calls, _ = self._run(
            validator_module, {"round_id": ROUND_ID}, score_result=False
        )
        assert scored_calls == [ROUND_ID]
        assert validator.scored_rounds == set()

    def test_real_zero_is_still_marked_done(self, validator_module):
        validator, scored_calls, _ = self._run(
            validator_module, {"round_id": ROUND_ID, "submission_count": 0}
        )
        assert scored_calls == []
        assert validator.scored_rounds == {ROUND_ID}

    def test_positive_count_is_scored(self, validator_module):
        validator, scored_calls, _ = self._run(
            validator_module, {"round_id": ROUND_ID, "submission_count": 7}
        )
        assert scored_calls == [ROUND_ID]
        assert validator.scored_rounds == {ROUND_ID}


class TestWeightHistoryPopulation:
    """Weight history must describe the population the weights were computed over.

    Reporting the unfiltered tracked miners gave a deregistered top scorer
    rank=1 weight=0.0, so the public audit trail contradicted the chain payment.
    """

    def test_deregistered_top_scorer_is_not_reported(self, validator_module):
        tracker = validator_module.ScoreTracker()
        tracker.recover_from_platform_state(
            legacy_score_entries=[],
            round_history=[
                {"round_id": f"r{i}", "scored_hotkeys": ["hk_dereg", "hk_live"]}
                for i in range(5)
            ],
        )
        tracker.update("hk_dereg", 0.9)  # top score, but no longer on chain
        tracker.update("hk_live", 0.5)

        posted = {}

        async def get_validator_state():
            return {"round_history": []}

        async def get_network_config():
            return {
                "burn_rate": 0.5,
                "burn_uid": 0,
                "winner_weight": 0.3,
                "dust_top_n": 3,
                "dust_decay": 0.5,
            }

        async def submit_weight_history(round_id, validator_hotkey, entries):
            posted["entries"] = entries

        validator = types.SimpleNamespace(
            score_tracker=tracker,
            platform_client=types.SimpleNamespace(
                get_validator_state=get_validator_state,
                get_network_config=get_network_config,
                submit_weight_history=submit_weight_history,
            ),
            metagraph=types.SimpleNamespace(
                hotkeys=["hk_burn", "hk_live", "hk_permit"],
                validator_permit=[False, False, True],
                coldkeys=["ck_burn", "ck_live", "ck_permit"],
            ),
            # One snapshot for UIDs, permits and ownership; these tests exercise
            # weight-history reporting, not the chain read.
            _chain_snapshot=lambda **_: _snapshot(),
            my_subnet_uid=99,
            wallet=types.SimpleNamespace(
                hotkey=types.SimpleNamespace(ss58_address="hk_validator")
            ),
            is_registered=False,
        )

        result = asyncio.run(
            validator_module.Validator._set_weights_after_round(validator, ROUND_ID)
        )

        assert result is True
        reported = {e["miner_hotkey"]: e for e in posted["entries"]}
        assert "hk_dereg" not in reported
        assert reported["hk_live"]["rank"] == 1
        assert reported["hk_live"]["weight"] > 0


class TestMainLoopWaitSurvivesErrors:
    """A throw after scoring must not collapse the inter-round wait.

    With the wait inside the per-iteration try, a subtensor outage in
    sync_metagraph dropped the loop into the handler's 10s sleep: ~360
    authenticated platform polls an hour for as long as the outage lasted.
    """

    def _drive_loop(self, validator_module, monkeypatch, sync_raises, iterations=2):
        task_interval = validator_module.GENOMICS_CONFIG["task_interval"]
        slept = []

        class _StopLoop(KeyboardInterrupt):
            pass

        async def fake_sleep(seconds):
            slept.append(seconds)
            if sum(slept) >= iterations * task_interval:
                raise _StopLoop()

        def sync_metagraph(metagraph, subtensor):
            if sync_raises:
                raise RuntimeError("subtensor websocket closed")

        cleanups = []

        monkeypatch.setattr(
            validator_module, "asyncio", types.SimpleNamespace(sleep=fake_sleep)
        )
        monkeypatch.setattr(
            validator_module, "bt_compat",
            types.SimpleNamespace(sync_metagraph=sync_metagraph),
        )
        monkeypatch.setattr(validator_module, "is_docker_available", lambda: True)

        validator = types.SimpleNamespace(
            config=types.SimpleNamespace(
                subtensor=types.SimpleNamespace(network="test"), netuid=107
            ),
            use_platform=False,
            platform_client=None,
            score_tracker=types.SimpleNamespace(
                get_stats=lambda: {
                    "top_round_score": 0.0,
                    "eligible_count": 0,
                    "total_miners_tracked": 0,
                    "rounds_tracked": 0,
                }
            ),
            metagraph=types.SimpleNamespace(hotkeys=[]),
            subtensor=object(),
            _cleanup_old_files=lambda: cleanups.append(True),
        )

        asyncio.run(validator_module.Validator.run(validator))
        return slept, cleanups, task_interval

    def test_sync_failure_waits_the_full_interval(self, validator_module, monkeypatch):
        slept, _, task_interval = self._drive_loop(
            validator_module, monkeypatch, sync_raises=True
        )
        assert 10 not in slept, "fell back to the catch-all's 10s retry sleep"
        assert sum(slept) == 2 * task_interval

    def test_healthy_iterations_wait_the_same_interval(self, validator_module, monkeypatch):
        slept, cleanups, task_interval = self._drive_loop(
            validator_module, monkeypatch, sync_raises=False
        )
        assert sum(slept) == 2 * task_interval
        assert cleanups, "_cleanup_old_files never ran on a healthy iteration"
