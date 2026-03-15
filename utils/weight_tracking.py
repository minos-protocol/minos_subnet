"""
Winner-takes-all weight tracking with EMA, participation gating, and score decay.

Tracks miner performance over time with exponential moving averages.
The top eligible miner receives 100% of the weight; all others get 0.

Two phases:
- Warmup (before any miner reaches min_rounds): reward split 50/30/20 among
  top 3 scoring active miners (>= 1 round) by EMA score.
- Normal: winner-takes-all by EMA among eligible miners.

Eligibility requires scoring in at least `min_rounds` of the recent window.
Tiebreaker: earliest submission timestamp in the most recent round.

Inactive miners' EMA scores decay each round they miss, preventing stale
high scores from persisting indefinitely.
"""

from typing import Dict, List, Any, Optional
from collections import defaultdict
import functools
import logging
import os

logger = logging.getLogger(__name__)

# Participation window: track last N rounds for eligibility checks
PARTICIPATION_WINDOW = 20
MIN_PARTICIPATION_ROUNDS = 10

# EMA decay: multiply absent miners' EMA by this factor each round they miss.
# 0.95 means a miner who misses 10 consecutive rounds sees their EMA drop to
# ~60% of its original value  (0.95^10 ≈ 0.60).
DEFAULT_DECAY_FACTOR = 0.95

# Warmup phase: positional reward split among top N scoring active miners.
# Index 0 = 1st place, 1 = 2nd, 2 = 3rd. Must sum to 1.0.
WARMUP_TOP_N = 3
WARMUP_WEIGHTS = [0.50, 0.30, 0.20]

# Scores within this epsilon are treated as tied; tiebreak by submission time.
SCORE_EPSILON = 0.005


class ScoreTracker:
    """Track scores with EMA and winner-takes-all weight distribution.

    Miners are identified by hotkey (ss58 address) for stability across
    metagraph resyncs. UID mapping happens at weight-setting time.
    """

    def __init__(
        self,
        alpha: float = None,
        min_rounds: int = MIN_PARTICIPATION_ROUNDS,
        decay_factor: float = None,
    ):
        """
        Initialize score tracker.

        Args:
            alpha: EMA smoothing factor (0 < alpha <= 1).
                   Higher values weight recent scores more heavily.
                   Defaults to EMA_ALPHA env var or 0.1.
            min_rounds: Minimum rounds scored to be eligible for weights.
            decay_factor: Multiplier applied to absent miners' EMA each round.
                          Defaults to EMA_DECAY_FACTOR env var or 0.95.
        """
        self.alpha = alpha if alpha is not None else float(os.getenv("EMA_ALPHA", "0.1"))
        self.min_rounds = min_rounds
        self.decay_factor = decay_factor if decay_factor is not None else float(
            os.getenv("EMA_DECAY_FACTOR", str(DEFAULT_DECAY_FACTOR))
        )

        # hotkey -> current EMA score
        self.ema_scores: Dict[str, float] = {}

        # hotkey -> most recent raw score (for reporting)
        self.last_raw_scores: Dict[str, float] = {}

        # Round participation history (sliding window)
        # Each entry: {"round_id": str, "scored_hotkeys": set[str]}
        self.round_history: List[dict] = []

        # hotkey -> participation count (cached, updated on record_round)
        self._participation_counts: Dict[str, int] = defaultdict(int)

    def recover_from_platform_state(
        self,
        ema_entries: List[Dict[str, Any]],
        round_history: List[Dict[str, Any]],
    ):
        """Rebuild tracker state from platform data after restart.

        Called once on startup to restore EMA scores and participation
        history from the platform DB, avoiding the need to re-score.

        Args:
            ema_entries: List of {miner_hotkey, ema_score, participation_count, eligible}
            round_history: List of {round_id, scored_hotkeys}
        """
        # Restore EMA scores
        for entry in ema_entries:
            hotkey = entry.get("miner_hotkey")
            ema = entry.get("ema_score")
            if hotkey and ema is not None:
                self.ema_scores[hotkey] = ema

        # Restore round history
        self.round_history = [
            {
                "round_id": r["round_id"],
                "scored_hotkeys": set(r.get("scored_hotkeys", [])),
            }
            for r in round_history
        ]

        # Trim to window
        if len(self.round_history) > PARTICIPATION_WINDOW:
            self.round_history = self.round_history[-PARTICIPATION_WINDOW:]

        # Recalculate participation counts from history
        self._recalculate_participation()

        logger.info(
            f"State recovered: {len(self.ema_scores)} miners, "
            f"{len(self.round_history)} rounds in history"
        )

    def update(self, hotkey: str, raw_score: float) -> float:
        """
        Update EMA score for a miner.

        Args:
            hotkey: Miner's ss58 hotkey
            raw_score: New raw score (e.g., combined_final from AdvancedScorer)

        Returns:
            Updated EMA score
        """
        # EMA starts at 0; first round yields α × S₀ (10% of first score)
        old_ema = self.ema_scores.get(hotkey, 0.0)
        new_ema = (1 - self.alpha) * old_ema + self.alpha * raw_score
        self.ema_scores[hotkey] = new_ema
        self.last_raw_scores[hotkey] = raw_score
        return new_ema

    def record_round(self, round_id: str, scored_hotkeys: List[str]):
        """
        Record which miners scored in a round for participation tracking.

        Call this once per round after all miners in that round have been scored.
        Also applies EMA decay to miners who were absent this round.

        Args:
            round_id: Unique round identifier
            scored_hotkeys: List of miner hotkeys that were scored this round
        """
        # Avoid recording the same round twice
        for entry in self.round_history:
            if entry["round_id"] == round_id:
                logger.debug(f"Round {round_id} already recorded, skipping")
                return

        scored_set = set(scored_hotkeys)

        # Decay EMA for miners who did NOT participate in this round
        if self.decay_factor < 1.0:
            decayed = 0
            for hotkey in list(self.ema_scores):
                if hotkey not in scored_set:
                    old = self.ema_scores[hotkey]
                    self.ema_scores[hotkey] = old * self.decay_factor
                    # Remove miners whose EMA has decayed to near-zero
                    if self.ema_scores[hotkey] < 1e-6:
                        del self.ema_scores[hotkey]
                        self.last_raw_scores.pop(hotkey, None)
                    else:
                        decayed += 1
            if decayed:
                logger.debug(f"Decayed EMA for {decayed} absent miners (factor={self.decay_factor})")

        self.round_history.append({
            "round_id": round_id,
            "scored_hotkeys": scored_set,
        })

        # Trim to window size
        if len(self.round_history) > PARTICIPATION_WINDOW:
            self.round_history = self.round_history[-PARTICIPATION_WINDOW:]

        # Recalculate participation counts
        self._recalculate_participation()

    def _recalculate_participation(self):
        """Recalculate participation counts from round history."""
        counts: Dict[str, int] = defaultdict(int)
        for entry in self.round_history:
            for hotkey in entry["scored_hotkeys"]:
                counts[hotkey] += 1
        self._participation_counts = counts

    def get_participation_count(self, hotkey: str) -> int:
        """Get the number of rounds a miner has scored in the recent window."""
        return self._participation_counts.get(hotkey, 0)

    def is_eligible(self, hotkey: str) -> bool:
        """Check if a miner meets the minimum participation requirement."""
        return self.get_participation_count(hotkey) >= self.min_rounds

    def get_winner_takes_all_weights(
        self,
        miner_hotkeys: List[str],
        submission_times: Optional[Dict[str, float]] = None,
    ) -> Dict[str, float]:
        """
        Compute winner-takes-all weights.

        Two modes:
        - **Warmup** (no miner has min_rounds yet): winner-takes-all by
          EMA score, among active miners only.
          Inactive miners (0 rounds) get zero weight.
        - **Normal**: winner-takes-all by EMA among eligible miners.

        Tiebreak in both modes: earliest submission timestamp.

        Args:
            miner_hotkeys: List of all miner hotkeys to consider.
            submission_times: Optional dict of {hotkey: submitted_at_epoch}
                              for tiebreaking. Typically from the most recent
                              round's submission timestamps.

        Returns:
            Dict of {hotkey: weight} for all miners in miner_hotkeys.
        """
        weights = {hk: 0.0 for hk in miner_hotkeys}

        # Find eligible miners
        eligible = [hk for hk in miner_hotkeys if self.is_eligible(hk)]

        if not eligible:
            # Warmup phase: no miner has hit the eligibility threshold yet.
            # Split reward equally among top WARMUP_TOP_N scoring active miners
            # (those who have participated in >= 1 round).
            active = [hk for hk in miner_hotkeys
                      if self.get_participation_count(hk) > 0]

            if not active:
                logger.info("No active miners yet — all weights zero")
                return weights

            # Sort by EMA score (desc); scores within SCORE_EPSILON are treated
            # as tied and ranked by earliest submission time instead.
            def _cmp(hk_a, hk_b):
                sa = self.ema_scores.get(hk_a, 0.0)
                sb = self.ema_scores.get(hk_b, 0.0)
                ta = submission_times.get(hk_a, float("inf")) if submission_times else float("inf")
                tb = submission_times.get(hk_b, float("inf")) if submission_times else float("inf")
                if abs(sa - sb) <= SCORE_EPSILON:
                    # Within epsilon — earlier submission ranks higher
                    return -1 if ta < tb else (1 if ta > tb else 0)
                # Clear score difference — higher EMA ranks higher
                return -1 if sa > sb else 1

            sorted_active = sorted(active, key=functools.cmp_to_key(_cmp))

            # Positionally split among top N active miners with EMA > 0
            top_n = [
                hk for hk in sorted_active[:WARMUP_TOP_N]
                if self.ema_scores.get(hk, 0.0) > 0
            ]

            if top_n:
                # Positional split: renormalize WARMUP_WEIGHTS to however many
                # miners actually scored > 0 (e.g. if only 2, split 50/30 → ~0.625/0.375)
                raw_w = WARMUP_WEIGHTS[:len(top_n)]
                total_w = sum(raw_w)
                positional_w = [w / total_w for w in raw_w]
                for hk, w in zip(top_n, positional_w):
                    weights[hk] = w
                top_scores = [
                    (hk[:16], self.ema_scores.get(hk, 0.0), w)
                    for hk, w in zip(top_n, positional_w)
                ]
                logger.info(
                    f"Warmup: top-{len(top_n)} positional split "
                    f"(active: {len(active)}/{len(miner_hotkeys)}, "
                    f"need {self.min_rounds} rounds for EMA eligibility): "
                    + ", ".join(f"{hk}...=ema:{s:.4f} w:{w:.2f}" for hk, s, w in top_scores)
                )
            else:
                # All active miners have zero EMA — split among active only
                equal_w = 1.0 / len(active)
                for hk in active:
                    weights[hk] = equal_w
                logger.info(
                    f"Warmup: all active miners scored zero, "
                    f"equal weights to {len(active)} active miners"
                )
            return weights

        # Find winner: highest EMA, tiebreak by earliest submission
        # Use tolerance for float comparison to avoid precision issues
        EMA_TOLERANCE = 1e-9
        best_hotkey = None
        best_ema = -1.0
        best_time = float("inf")

        for hk in eligible:
            ema = self.ema_scores.get(hk, 0.0)
            sub_time = (
                submission_times.get(hk, float("inf"))
                if submission_times
                else float("inf")
            )

            if ema > best_ema + EMA_TOLERANCE:
                # Strictly better EMA — new winner
                best_ema = ema
                best_hotkey = hk
                best_time = sub_time
            elif abs(ema - best_ema) <= EMA_TOLERANCE and sub_time < best_time:
                # EMA effectively tied — tiebreak by earliest submission
                best_ema = ema
                best_hotkey = hk
                best_time = sub_time

        if best_hotkey is not None and best_ema > 0:
            weights[best_hotkey] = 1.0
            logger.info(
                f"Winner: {best_hotkey[:16]}... EMA={best_ema:.4f} "
                f"(eligible: {len(eligible)}/{len(miner_hotkeys)})"
            )
        else:
            # All eligible miners have zero EMA — return all-zero weights (fail closed).
            # The caller (set_weights) will detect the zero total and skip submission
            # rather than silently distributing equal emissions.
            logger.warning(
                f"All {len(eligible)} eligible miners have zero EMA — "
                f"returning zero weights (no-op). Validator will skip weight submission."
            )

        return weights

    def get_rankings(self, miner_hotkeys: List[str]) -> Dict[str, Optional[int]]:
        """
        Get EMA-based rankings for all miners.
        Only eligible miners are ranked (1 = best). Ineligible miners get None.

        Args:
            miner_hotkeys: List of miner hotkeys to rank.

        Returns:
            Dict of {hotkey: rank_or_None}
        """
        eligible_scores = []
        for hk in miner_hotkeys:
            if self.is_eligible(hk):
                eligible_scores.append((hk, self.ema_scores.get(hk, 0.0)))

        # Sort descending by EMA
        eligible_scores.sort(key=lambda x: -x[1])

        rankings: Dict[str, Optional[int]] = {hk: None for hk in miner_hotkeys}
        for rank, (hk, _) in enumerate(eligible_scores, start=1):
            rankings[hk] = rank

        return rankings

    def build_weight_history(
        self,
        round_id: str,
        validator_hotkey: str,
        miner_hotkeys: List[str],
        weights: Dict[str, float],
    ) -> List[Dict[str, Any]]:
        """
        Build the payload for submitting weight history to the platform.

        Args:
            round_id: The round these weights correspond to.
            validator_hotkey: The validator's hotkey.
            miner_hotkeys: All miner hotkeys.
            weights: The weight dict from get_winner_takes_all_weights().

        Returns:
            List of entry dicts ready for the API.
        """
        rankings = self.get_rankings(miner_hotkeys)

        entries = []
        for hk in miner_hotkeys:
            entries.append({
                "miner_hotkey": hk,
                "raw_score": self.last_raw_scores.get(hk),
                "ema_score": self.ema_scores.get(hk),
                "rank": rankings.get(hk),
                "weight": weights.get(hk, 0.0),
                "eligible": self.is_eligible(hk),
                "participation_count": self.get_participation_count(hk),
            })

        return entries

    def get_stats(self) -> Dict[str, Any]:
        """Get current statistics for logging."""
        all_hotkeys = list(self.ema_scores.keys())
        eligible_count = sum(1 for hk in all_hotkeys if self.is_eligible(hk))
        ema_values = list(self.ema_scores.values())

        return {
            "total_miners_tracked": len(all_hotkeys),
            "eligible_count": eligible_count,
            "rounds_tracked": len(self.round_history),
            "min_rounds_required": self.min_rounds,
            "ema_alpha": self.alpha,
            "decay_factor": self.decay_factor,
            "top_ema": max(ema_values) if ema_values else 0.0,
            "mean_ema": sum(ema_values) / len(ema_values) if ema_values else 0.0,
        }
