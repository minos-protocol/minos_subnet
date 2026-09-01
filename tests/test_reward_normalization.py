"""One reward-eligible representative per coldkey.

Ownership is read from the metagraph -- public chain state every validator
already syncs -- so two honest validators agree without anything being attested.
"""
import types

import pytest

from utils.reward_normalization import apply, one_per_owner, owners_from_metagraph


def _mg(pairs):
    """A metagraph carrying (hotkey, coldkey) pairs in UID order."""
    return types.SimpleNamespace(
        hotkeys=[h for h, _ in pairs],
        coldkeys=[c for _, c in pairs],
    )


class TestOwnersFromMetagraph:
    def test_reads_the_pairs_in_uid_order(self):
        mg = _mg([("hk1", "ckA"), ("hk2", "ckB"), ("hk3", "ckA")])
        assert owners_from_metagraph(mg) == {"hk1": "ckA", "hk2": "ckB", "hk3": "ckA"}

    @pytest.mark.parametrize("mg", [
        types.SimpleNamespace(hotkeys=[], coldkeys=[]),
        types.SimpleNamespace(hotkeys=["hk1"], coldkeys=[]),
        types.SimpleNamespace(hotkeys=[], coldkeys=["ckA"]),
        types.SimpleNamespace(),
    ])
    def test_an_unusable_metagraph_yields_nothing(self, mg):
        assert owners_from_metagraph(mg) == {}

    def test_a_short_coldkey_list_does_not_raise(self):
        """Never index past the end -- a partial sync must not take the round."""
        mg = types.SimpleNamespace(hotkeys=["a", "b", "c"], coldkeys=["ckA"])
        assert owners_from_metagraph(mg) == {"a": "ckA"}


class TestOnePerColdkey:
    def test_the_best_ranked_hotkey_survives(self):
        ranked = ["hk1", "hk2", "hk3"]              # best first
        owners = {"hk1": "ckA", "hk2": "ckA", "hk3": "ckB"}
        kept, rep = one_per_owner(ranked, owners)
        assert kept == ["hk1", "hk3"]
        assert rep == {"hk2": "hk1"}, "the dropped hotkey must name its representative"

    def test_order_is_preserved(self):
        """This must not reorder anything -- ties were already resolved upstream
        by score then submission time."""
        ranked = ["a", "b", "c", "d", "e"]
        owners = {"a": "1", "b": "2", "c": "1", "d": "3", "e": "2"}
        kept, _ = one_per_owner(ranked, owners)
        assert kept == ["a", "b", "d"]

    def test_a_coldkey_with_many_hotkeys_gets_exactly_one(self):
        ranked = [f"hk{i}" for i in range(20)]
        owners = {hk: "ckA" for hk in ranked}
        kept, rep = one_per_owner(ranked, owners)
        assert kept == ["hk0"]
        assert len(rep) == 19
        assert set(rep.values()) == {"hk0"}

    def test_unrelated_hotkeys_are_untouched(self):
        ranked = ["a", "b", "c"]
        owners = {"a": "1", "b": "2", "c": "3"}
        kept, rep = one_per_owner(ranked, owners)
        assert kept == ranked and rep == {}

    @pytest.mark.parametrize("missing", [{}, {"hk2": "ckB"}])
    def test_an_unknown_owner_is_kept_not_dropped(self, missing):
        """A stale or partial metagraph must not silently remove real miners.
        Paying a sybil twice is recoverable; unpaying a legitimate miner is not."""
        ranked = ["hk1", "hk2"]
        kept, _ = one_per_owner(ranked, missing)
        assert "hk1" in kept, "a hotkey with no resolvable owner was dropped"

    def test_an_empty_ranking_is_empty(self):
        assert one_per_owner([], {"a": "1"}) == ([], {})


class TestApply:
    RANKED = ["hk1", "hk2", "hk3"]
    MG = _mg([("hk1", "ckA"), ("hk2", "ckA"), ("hk3", "ckB")])

    def test_disabled_is_a_no_op(self):
        kept, rep = apply(self.RANKED, self.MG, enabled=False)
        assert kept == self.RANKED and rep == {}

    def test_enabled_deduplicates(self):
        kept, rep = apply(self.RANKED, self.MG, enabled=True)
        assert kept == ["hk1", "hk3"] and rep == {"hk2": "hk1"}

    def test_a_metagraph_without_coldkeys_ranks_everyone(self):
        """Enabled but unreadable must not drop anyone."""
        blank = types.SimpleNamespace(hotkeys=["hk1", "hk2", "hk3"], coldkeys=[])
        kept, rep = apply(self.RANKED, blank, enabled=True)
        assert kept == self.RANKED and rep == {}

    def test_it_returns_a_copy_not_the_caller_s_list(self):
        original = list(self.RANKED)
        kept, _ = apply(original, self.MG, enabled=False)
        kept.append("mutated")
        assert original == self.RANKED, "mutating the result changed the input"


class TestDeterminismAcrossValidators:
    def test_two_validators_on_the_same_block_agree(self):
        """The point of reading the chain: no attestation, no trust, no
        disagreement. Same metagraph, same ranking, same answer."""
        pairs = [(f"hk{i}", f"ck{i % 7}") for i in range(40)]
        ranked = [h for h, _ in pairs]
        a = apply(ranked, _mg(pairs), enabled=True)
        b = apply(ranked, _mg(list(pairs)), enabled=True)
        assert a == b
        assert len(a[0]) == 7, "one survivor per distinct coldkey"


class TestTheValidatorGate:
    """Enabled, this REMOVES hotkeys from the reward set. An unreadable policy
    must leave the ranking alone rather than guess at dropping people."""

    @pytest.mark.parametrize("cfg", [
        None, {}, "not-a-dict", 42,
        {"reward_normalization": False},
        {"reward_normalization": "true"},   # a string is not True
        {"reward_normalization": 1},        # nor is a truthy int
        {"reward_normalization": None},
    ])
    def test_anything_but_a_literal_true_is_off(self, cfg):
        from neurons.validator import _reward_normalization_enabled
        assert _reward_normalization_enabled(cfg) is False

    def test_a_literal_true_enables_it(self):
        from neurons.validator import _reward_normalization_enabled
        assert _reward_normalization_enabled({"reward_normalization": True}) is True


class TestTheChainSnapshot:
    """UIDs, permits and ownership must describe ONE view of the chain. A
    refresh partway through leaves the weight vector addressing UIDs from one
    block and ownership from another."""

    def _validator(self, metagraph, owner_map=None):
        import types
        from neurons.validator import Validator

        calls = []

        async def get_owner_map():
            calls.append("owner-map")
            if owner_map is None:
                raise RuntimeError("route not available")
            return {"owners": owner_map, "block": 1}

        v = object.__new__(Validator)
        v.metagraph = metagraph
        v.platform_client = types.SimpleNamespace(get_owner_map=get_owner_map)
        return v, calls

    @pytest.mark.parametrize("n", [0, 1, 5, 100])
    def test_a_numpy_validator_permit_does_not_raise(self, n):
        """Bittensor stores validator_permit as a numpy array. `x or []` calls
        bool() on it, which raises for every length but one -- and the caller
        turns that into "no weights this round", every round."""
        import asyncio
        np = pytest.importorskip("numpy")
        mg = types.SimpleNamespace(
            hotkeys=[f"hk{i}" for i in range(n)],
            coldkeys=[f"ck{i}" for i in range(n)],
            validator_permit=np.zeros(n, dtype=bool),
        )
        v, _ = self._validator(mg)
        snap = asyncio.run(v._chain_snapshot())
        assert len(snap["permits"]) == n
        assert len(snap["hotkeys"]) == n

    def test_ownership_falls_back_to_the_platform(self):
        """A hotkey belongs to the coldkey that registered it and stays there,
        so a cached map is still correct. Skipping normalization instead would
        pay a whole fleet for the round."""
        import asyncio
        mg = _mg([("hk1", ""), ("hk2", "")])   # coldkeys unreadable
        mg.coldkeys = []
        v, calls = self._validator(mg, owner_map={"hk1": "ckA", "hk2": "ckA"})
        snap = asyncio.run(v._chain_snapshot(need_owners=True))
        assert snap["owners"] == {"hk1": "ckA", "hk2": "ckA"}
        assert calls == ["owner-map"]

    def test_the_platform_is_not_asked_when_ownership_is_unused(self):
        """The fallback is a round-trip to a route that does not exist on every
        deployment. With normalization off there is nothing to fetch."""
        import asyncio
        mg = _mg([("hk1", "")])
        mg.coldkeys = []
        v, calls = self._validator(mg, owner_map={"hk1": "ckA"})
        asyncio.run(v._chain_snapshot(need_owners=False))
        assert calls == []

    def test_an_unreachable_platform_leaves_the_ranking_alone(self):
        """Neither source answering must rank everyone, never silently drop."""
        import asyncio
        mg = _mg([("hk1", "")])
        mg.coldkeys = []
        v, _ = self._validator(mg, owner_map=None)   # raises
        snap = asyncio.run(v._chain_snapshot(need_owners=True))
        assert snap["owners"] == {}
