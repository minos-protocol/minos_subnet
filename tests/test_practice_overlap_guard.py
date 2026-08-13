"""Regression tests for the validator practice/live overlap guard.

Practice samples are fully answered (truth includes the synthetic planted
variants) and their files are downloadable by any keypair, so a live round
that reuses a practice BAM or mutation set is decided before it starts: a
miner who cached the practice answer key submits perfect planted calls with
zero read evidence. The validator must hard-fail any round whose file
hashes appear in the practice manifest, refuse it BEFORE downloading its
files, and must not penalize rounds that are disjoint from the practice
pool or rounds scored while the manifest is unavailable.
"""

import asyncio
import importlib
import sys
import types

from utils.platform_client import PlatformClientError


ROUND_ID = "2026-08-13T12:00:00.000000+00:00"

# A practice sample's answer-key file hashes, as served by the manifest.
PRACTICE_BAM_SHA = "a" * 64
PRACTICE_TRUTH_SHA = "b" * 64
PRACTICE_MUTATIONS_SHA = "c" * 64

# Honest live round: no file shared with the practice pool.
HONEST_BAM_SHA = "1" * 64
HONEST_TRUTH_SHA = "2" * 64
HONEST_MUTATIONS_SHA = "3" * 64


def _noop(*args, **kwargs):
    return None


def _import_validator_with_runtime_stubs(monkeypatch):
    """Import neurons.validator without requiring a live Bittensor runtime.

    Returns (module, error_log): the stubbed logging sink records every
    bt.logging.error message so tests can distinguish "the guard fired"
    from "an exception was swallowed by the scoring try/except".
    """
    error_log = []

    def _record_error(*args, **kwargs):
        error_log.append(" ".join(str(a) for a in args))

    logging_stub = types.SimpleNamespace(
        debug=_noop,
        error=_record_error,
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
    return importlib.import_module("neurons.validator"), error_log


def _make_round_data(bam_sha, truth_sha, mutations_sha):
    """Round payload shaped like /v2/get-submissions' response."""
    return {
        "round_id": ROUND_ID,
        "region": "chr18:1-1000000",
        "num_mutations": 42,
        "bam_sha256": bam_sha,
        "truth_vcf_sha256": truth_sha,
        "mutations_vcf_sha256": mutations_sha,
        "submissions": [{"miner_hotkey": "hk_miner_1"}],
    }


def _make_validator(validator_module, round_data, manifest):
    """Validator stand-in whose platform client serves the given round +
    practice manifest. ``_download_round_files`` records every call so tests
    can witness whether the round was refused before its files were touched.
    """
    async def get_assignment(round_id):
        # Force the documented fallback path (score all miners).
        raise PlatformClientError("stub: no assignment")

    async def get_round_submissions(round_id):
        return round_data

    async def get_practice_manifest():
        return manifest

    platform_client = types.SimpleNamespace(
        get_assignment=get_assignment,
        get_round_submissions=get_round_submissions,
        get_practice_manifest=get_practice_manifest,
    )

    download_attempts = []

    def _download_round_files(round_id, round_data):
        download_attempts.append(round_id)
        return None

    validator = types.SimpleNamespace(
        platform_client=platform_client,
        _download_round_files=_download_round_files,
    )
    # SimpleNamespace has no class attribute lookup, so bind the real gate
    # method (the unit under test) explicitly.
    validator._round_reuses_practice_material = (
        lambda rid, rd: validator_module.Validator._round_reuses_practice_material(
            validator, rid, rd
        )
    )
    return validator, download_attempts


def _practice_manifest():
    return {PRACTICE_BAM_SHA, PRACTICE_TRUTH_SHA, PRACTICE_MUTATIONS_SHA}


def test_round_reusing_practice_bam_hard_fails_before_download(monkeypatch):
    validator_module, error_log = _import_validator_with_runtime_stubs(monkeypatch)
    round_data = _make_round_data(
        bam_sha=PRACTICE_BAM_SHA,  # collides with the practice pool
        truth_sha=HONEST_TRUTH_SHA,
        mutations_sha=HONEST_MUTATIONS_SHA,
    )
    validator, download_attempts = _make_validator(validator_module, round_data, _practice_manifest())

    result = asyncio.run(
        validator_module.Validator._score_round_submissions(validator, ROUND_ID)
    )

    assert result is False
    # Refused wholesale: no round file may be downloaded or scored.
    assert download_attempts == []
    # The guard fired for the right reason (not a swallowed exception).
    assert any("HARD FAIL" in msg for msg in error_log), error_log

    sys.modules.pop("neurons.validator", None)


def test_round_reusing_practice_mutations_vcf_hard_fails(monkeypatch):
    """The planted-answer file alone is enough to decide the round."""
    validator_module, error_log = _import_validator_with_runtime_stubs(monkeypatch)
    round_data = _make_round_data(
        bam_sha=HONEST_BAM_SHA,
        truth_sha=HONEST_TRUTH_SHA,
        mutations_sha=PRACTICE_MUTATIONS_SHA,  # collides with the practice pool
    )
    validator, download_attempts = _make_validator(validator_module, round_data, _practice_manifest())

    result = asyncio.run(
        validator_module.Validator._score_round_submissions(validator, ROUND_ID)
    )

    assert result is False
    assert download_attempts == []
    assert any("HARD FAIL" in msg for msg in error_log), error_log

    sys.modules.pop("neurons.validator", None)


def test_disjoint_round_is_not_refused(monkeypatch):
    validator_module, error_log = _import_validator_with_runtime_stubs(monkeypatch)
    round_data = _make_round_data(
        bam_sha=HONEST_BAM_SHA,
        truth_sha=HONEST_TRUTH_SHA,
        mutations_sha=HONEST_MUTATIONS_SHA,
    )
    validator, download_attempts = _make_validator(validator_module, round_data, _practice_manifest())

    asyncio.run(
        validator_module.Validator._score_round_submissions(validator, ROUND_ID)
    )

    # Guard did not fire: the round proceeded to file download, cleanly.
    assert download_attempts == [ROUND_ID]
    assert error_log == [], error_log

    sys.modules.pop("neurons.validator", None)


def test_manifest_unavailable_degrades_to_scoring(monkeypatch):
    """Practice mode disabled / endpoint not deployed: overlap is not
    provable, so the round must still be scored (a hard fail fires only on
    an actual hash collision)."""
    validator_module, error_log = _import_validator_with_runtime_stubs(monkeypatch)
    round_data = _make_round_data(
        bam_sha=PRACTICE_BAM_SHA,
        truth_sha=HONEST_TRUTH_SHA,
        mutations_sha=HONEST_MUTATIONS_SHA,
    )
    # manifest fetch failed → None
    validator, download_attempts = _make_validator(validator_module, round_data, None)

    asyncio.run(
        validator_module.Validator._score_round_submissions(validator, ROUND_ID)
    )

    assert download_attempts == [ROUND_ID]
    assert error_log == [], error_log

    sys.modules.pop("neurons.validator", None)


def test_hash_matching_is_case_insensitive(monkeypatch):
    validator_module, _ = _import_validator_with_runtime_stubs(monkeypatch)
    round_data = _make_round_data(
        bam_sha=PRACTICE_BAM_SHA,  # lowercase round hash
        truth_sha=HONEST_TRUTH_SHA,
        mutations_sha=HONEST_MUTATIONS_SHA,
    )
    # Manifest serves the same hash uppercased — must still collide.
    validator, download_attempts = _make_validator(
        validator_module, round_data, {PRACTICE_BAM_SHA.upper()}
    )

    result = asyncio.run(
        validator_module.Validator._round_reuses_practice_material(
            validator, ROUND_ID, round_data
        )
    )

    assert result is True
    assert download_attempts == []

    sys.modules.pop("neurons.validator", None)


def test_round_without_file_hashes_is_not_refused(monkeypatch):
    """A round missing hash fields skips those comparisons instead of
    crashing the scoring path."""
    validator_module, _ = _import_validator_with_runtime_stubs(monkeypatch)
    round_data = {
        "round_id": ROUND_ID,
        "region": "chr18:1-1000000",
        "submissions": [{"miner_hotkey": "hk_miner_1"}],
        # no bam_sha256 / truth_vcf_sha256 / mutations_vcf_sha256 keys
    }
    validator, download_attempts = _make_validator(validator_module, round_data, _practice_manifest())

    result = asyncio.run(
        validator_module.Validator._round_reuses_practice_material(
            validator, ROUND_ID, round_data
        )
    )

    assert result is False
    assert download_attempts == []

    sys.modules.pop("neurons.validator", None)
