"""Tests for the miner-side config commitment.

The properties that matter are adversarial: the commitment must be reproducible
by the miner who made it, and useless to anyone trying to work out what config
it describes.
"""

import json
import os
import stat

import pytest

from utils import config_commit as cc


BASE = dict(
    netuid=107,
    round_id="2026-08-26T03:44:00+00:00",
    hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
    tool_name="gatk",
)

CONFIG = {
    "tool": "gatk",
    "gatk_options": {
        "min_base_quality_score": 18,
        "contamination_fraction_to_filter": 0.02,
        "standard_min_confidence_threshold_for_calling": 30,
    },
}


class TestDeterminism:
    def test_same_inputs_give_the_same_commitment(self):
        n = cc.new_nonce()
        a = cc.compute_commitment(tool_config=CONFIG, nonce=n, **BASE)
        b = cc.compute_commitment(tool_config=CONFIG, nonce=n, **BASE)
        assert a == b and len(a) == 64

    def test_key_order_does_not_matter(self):
        n = cc.new_nonce()
        reordered = {
            "gatk_options": dict(reversed(list(CONFIG["gatk_options"].items()))),
            "tool": "gatk",
        }
        assert cc.compute_commitment(tool_config=CONFIG, nonce=n, **BASE) == \
               cc.compute_commitment(tool_config=reordered, nonce=n, **BASE)

    def test_equivalent_numeric_spellings_agree(self):
        """30 and 30.0 are the same config to the caller. The field submits both,
        so they must not produce different commitments — otherwise a miner's own
        hash fails to reproduce after a restart that reparsed its config file."""
        n = cc.new_nonce()
        as_int = {"tool": "gatk", "gatk_options": {"standard_min_confidence_threshold_for_calling": 30}}
        as_float = {"tool": "gatk", "gatk_options": {"standard_min_confidence_threshold_for_calling": 30.0}}
        assert cc.compute_commitment(tool_config=as_int, nonce=n, **BASE) == \
               cc.compute_commitment(tool_config=as_float, nonce=n, **BASE)

    def test_booleans_are_not_collapsed_into_integers(self):
        n = cc.new_nonce()
        as_bool = {"gatk_options": {"x": True}}
        as_int = {"gatk_options": {"x": 1}}
        assert cc.compute_commitment(tool_config=as_bool, nonce=n, **BASE) != \
               cc.compute_commitment(tool_config=as_int, nonce=n, **BASE)


class TestHiding:
    """The config space is enumerable, so a bare hash would be an oracle."""

    def test_a_new_nonce_changes_the_commitment(self):
        a = cc.compute_commitment(tool_config=CONFIG, nonce=cc.new_nonce(), **BASE)
        b = cc.compute_commitment(tool_config=CONFIG, nonce=cc.new_nonce(), **BASE)
        assert a != b

    def test_attacker_with_the_exact_config_cannot_match_without_the_nonce(self):
        target = cc.compute_commitment(tool_config=CONFIG, nonce=cc.new_nonce(), **BASE)
        guesses = [
            cc.compute_commitment(tool_config=CONFIG, nonce=cc.new_nonce(), **BASE)
            for _ in range(2000)
        ]
        assert target not in guesses

    def test_nonce_is_256_bit(self):
        n = cc.new_nonce()
        assert len(n) == 64 and int(n, 16) >= 0
        assert cc.new_nonce() != cc.new_nonce()

    def test_refuses_to_commit_without_a_nonce(self):
        with pytest.raises(ValueError, match="without a nonce"):
            cc.compute_commitment(tool_config=CONFIG, nonce="", **BASE)


class TestBinding:
    """Each bound field must actually change the digest, or it is decoration."""

    @pytest.mark.parametrize(
        "field,value",
        [("netuid", 1), ("round_id", "other-round"), ("hotkey", "5XXX"), ("tool_name", "bcftools")],
    )
    def test_changing_a_bound_field_changes_the_commitment(self, field, value):
        n = cc.new_nonce()
        base = cc.compute_commitment(tool_config=CONFIG, nonce=n, **BASE)
        altered = dict(BASE, **{field: value})
        assert cc.compute_commitment(tool_config=CONFIG, nonce=n, **altered) != base

    def test_a_single_config_value_change_is_detected(self):
        n = cc.new_nonce()
        tampered = json.loads(json.dumps(CONFIG))
        tampered["gatk_options"]["min_base_quality_score"] = 19
        assert cc.compute_commitment(tool_config=tampered, nonce=n, **BASE) != \
               cc.compute_commitment(tool_config=CONFIG, nonce=n, **BASE)

    def test_field_boundaries_cannot_be_shifted(self):
        """Concatenating without a separator would let one field bleed into the
        next, so two different tuples could share a digest."""
        n = cc.new_nonce()
        a = cc.compute_commitment(tool_config=CONFIG, nonce=n, **dict(BASE, hotkey="AB", tool_name="gatk"))
        b = cc.compute_commitment(tool_config=CONFIG, nonce=n, **dict(BASE, hotkey="A", tool_name="Bgatk"))
        assert a != b


class TestSubmissionConfig:
    """The commitment must describe the bytes the platform actually receives."""

    def test_infra_params_are_stripped(self):
        cfg = dict(CONFIG, threads=8, memory_gb=32, timeout=900,
                   ref_build="GRCh38", num_threads=4)
        out = cc.submission_config(cfg)
        for k in cc.INFRA_PARAMS:
            assert k not in out
        assert out["gatk_options"] == CONFIG["gatk_options"]

    def test_infra_params_do_not_affect_the_commitment(self):
        """Two miners on different hardware running the same config must produce
        the same digest."""
        n = cc.new_nonce()
        a = cc.compute_commitment(tool_config=CONFIG, nonce=n, **BASE)
        b = cc.compute_commitment(
            tool_config=dict(CONFIG, threads=64, memory_gb=256), nonce=n, **BASE
        )
        assert a == b

    def test_platform_client_uses_this_same_strip(self):
        """If the two lists drifted, the published hash would describe different
        bytes than the platform received and no commitment could ever open."""
        src = open("utils/platform_client.py").read()
        assert "safe_config = submission_config(tool_config)" in src
        assert "_INFRA_PARAMS = {" not in src, "inline strip list reintroduced"


class TestChainPayload:
    def test_shape_and_round_binding(self):
        c = "a" * 64
        p = cc.chain_payload(BASE["round_id"], c)
        assert p.startswith(f"m{cc.VERSION}:") and p.endswith(c)
        assert p != cc.chain_payload("a-different-round", c)

    def test_commitment_is_never_truncated(self):
        c = "b" * 64
        assert c in cc.chain_payload(BASE["round_id"], c)

    def test_fits_the_on_chain_budget(self):
        p = cc.chain_payload(BASE["round_id"], "c" * 64)
        assert len(p.encode()) <= cc.MAX_CHAIN_PAYLOAD_BYTES

    def test_raises_rather_than_silently_truncating(self):
        with pytest.raises(ValueError, match="exceeds"):
            cc.chain_payload(BASE["round_id"], "d" * 400)


class TestVerify:
    def test_opens_a_correct_commitment(self):
        n = cc.new_nonce()
        c = cc.compute_commitment(tool_config=CONFIG, nonce=n, **BASE)
        assert cc.verify_commitment(c, tool_config=CONFIG, nonce=n, **BASE) is True

    def test_rejects_a_tampered_config(self):
        n = cc.new_nonce()
        c = cc.compute_commitment(tool_config=CONFIG, nonce=n, **BASE)
        tampered = json.loads(json.dumps(CONFIG))
        tampered["gatk_options"]["min_base_quality_score"] = 99
        assert cc.verify_commitment(c, tool_config=tampered, nonce=n, **BASE) is False

    def test_rejects_the_wrong_nonce_and_an_empty_expectation(self):
        n = cc.new_nonce()
        c = cc.compute_commitment(tool_config=CONFIG, nonce=n, **BASE)
        assert cc.verify_commitment(c, tool_config=CONFIG, nonce=cc.new_nonce(), **BASE) is False
        assert cc.verify_commitment("", tool_config=CONFIG, nonce=n, **BASE) is False


class TestCommitmentLedger:
    """A commitment whose nonce was lost proves nothing."""

    def test_round_trips_an_entry(self, tmp_path):
        led = cc.CommitmentLedger(tmp_path / "c.jsonl")
        n = cc.new_nonce()
        led.record({"round_id": BASE["round_id"], "hotkey": BASE["hotkey"], "nonce": n})
        found = led.find(BASE["round_id"])
        assert found["nonce"] == n

    def test_returns_the_most_recent_entry_for_a_round(self, tmp_path):
        led = cc.CommitmentLedger(tmp_path / "c.jsonl")
        for nonce in ("first", "second", "third"):
            led.record({"round_id": "r", "hotkey": "hk", "nonce": nonce})
        assert led.find("r")["nonce"] == "third"

    def test_filters_by_hotkey(self, tmp_path):
        led = cc.CommitmentLedger(tmp_path / "c.jsonl")
        led.record({"round_id": "r", "hotkey": "a", "nonce": "na"})
        led.record({"round_id": "r", "hotkey": "b", "nonce": "nb"})
        assert led.find("r", hotkey="a")["nonce"] == "na"

    def test_missing_file_and_unknown_round_return_none(self, tmp_path):
        assert cc.CommitmentLedger(tmp_path / "absent.jsonl").find("r") is None
        led = cc.CommitmentLedger(tmp_path / "c.jsonl")
        led.record({"round_id": "r", "hotkey": "hk", "nonce": "n"})
        assert led.find("other") is None

    def test_survives_a_corrupt_line(self, tmp_path):
        path = tmp_path / "c.jsonl"
        led = cc.CommitmentLedger(path)
        led.record({"round_id": "r", "hotkey": "hk", "nonce": "good"})
        with open(path, "a") as fh:
            fh.write("{not json\n")
        assert led.find("r")["nonce"] == "good"

    def test_ledger_is_not_world_readable(self, tmp_path):
        """It holds unrevealed nonces."""
        path = tmp_path / "c.jsonl"
        cc.CommitmentLedger(path).record({"round_id": "r", "hotkey": "hk", "nonce": "n"})
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode & 0o077 == 0, f"ledger is group/world accessible: {oct(mode)}"

    def test_honours_the_env_override(self, tmp_path, monkeypatch):
        target = tmp_path / "custom.jsonl"
        monkeypatch.setenv("MINOS_COMMITMENT_LEDGER", str(target))
        cc.CommitmentLedger().record({"round_id": "r", "hotkey": "hk", "nonce": "n"})
        assert target.exists()
