"""
Round-only winner weighting.

Each Minos round is a fresh genomics challenge, so validator weights should be
computed from that round's finalized scores only. Miners must still have valid
scores in at least 5 of the last 20 finalized rounds before they can receive
winner/dust weight; the current round counts. The platform weight-history
schema still has a legacy ``ema_score`` field, which is intentionally left empty
for round-only scoring.

Weight distribution is winner-heavy: the top eligible current-round miner
receives the configured winner weight, ranks #2..N receive pruning dust, and the
caller sends the unallocated remainder to burn.
"""

from typing import Dict, List, Any, Optional
from collections import defaultdict
import functools
import logging
import math
import os

logger = logging.getLogger(__name__)

# Eligibility gate: score in at least 5 of the last 20 finalized rounds. The
# current round is appended before weights are computed, so it counts.
PARTICIPATION_WINDOW = int(os.getenv("PARTICIPATION_WINDOW", "20"))
MIN_PARTICIPATION_ROUNDS = int(os.getenv("MIN_PARTICIPATION_ROUNDS", "5"))

# Equal current-round scores are tied by earliest submission timestamp.
ROUND_SCORE_TOLERANCE = 1e-9

# Canonical-ranking tiebreak. When a canonical candidate is within this absolute
# current-round score gap of local rank 1, the canonical candidate is used as the
# winner. This keeps validators aligned on very close rounds without overriding
# clear local score differences.
CANONICAL_TIEBREAK_TOLERANCE = 0.001

# Minimum canonical coverage. Platform contributors below the per-validator
# stake floor are excluded, and the validator requires enough distinct
# validators before using the canonical ranking.
CANONICAL_MIN_VALIDATOR_COUNT = int(os.getenv("CANONICAL_MIN_VALIDATOR_COUNT", "4"))
CANONICAL_MIN_VALIDATOR_STAKE = float(os.getenv("CANONICAL_MIN_VALIDATOR_STAKE", "5000"))

# Reward defaults. These are absolute validator-vector weights before
# Bittensor's u16 encoding: burn gets 0.87, rank #1 gets 0.10, and ranks
# #2-#10 split the remaining 0.03 by geometric decay.
DEFAULT_BURN_RATE = 0.87
DEFAULT_WINNER_WEIGHT = 0.10
DEFAULT_DUST_TOP_N = 10
DEFAULT_DUST_DECAY = 0.80


class ScoreTracker:
    """Track current-round scores plus recent-window participation counts.

    Miners are identified by hotkey (ss58 address) for stability across
    metagraph resyncs. UID mapping happens at weight-setting time.
    """

    def __init__(
        self,
        min_rounds: int = MIN_PARTICIPATION_ROUNDS,
    ):
        self.min_rounds = min_rounds

        # hotkey -> current round score
        self.round_scores: Dict[str, float] = {}

        # hotkey -> current round raw score (same value, explicit for reporting)
        self.last_raw_scores: Dict[str, float] = {}

        # Recent finalized rounds for the 5-of-20 eligibility gate.
        self.round_history: List[dict] = []
        self._participation_counts: Dict[str, int] = defaultdict(int)
        self._recorded_round_ids = set()

    def recover_from_platform_state(
        self,
        legacy_score_entries: List[Dict[str, Any]],
        round_history: List[Dict[str, Any]],
    ):
        """Start fresh on scores while recovering recent participation.

        Historical platform scores are not loaded because old scores must not
        influence the next round's ranking. Recent participation history is
        loaded so the 5-of-20 eligibility gate survives validator restarts.
        Restart recovery for a currently scoring round is handled separately by
        /v2/get-submissions, which returns already-submitted scores for that
        round.
        """
        self.round_scores.clear()
        self.last_raw_scores.clear()
        self.round_history = []
        self._recorded_round_ids = set()
        self._participation_counts = defaultdict(int)
        for entry in round_history or []:
            if not isinstance(entry, dict):
                continue
            round_id = entry.get("round_id")
            if not round_id:
                continue
            scored_hotkeys = {
                hk for hk in entry.get("scored_hotkeys", []) if isinstance(hk, str) and hk
            }
            self.round_history.append({
                "round_id": round_id,
                "scored_hotkeys": scored_hotkeys,
            })
        self.round_history = self.round_history[-PARTICIPATION_WINDOW:]
        self._recalculate_participation()
        logger.info(
            "Round-only score tracker initialized fresh; ignored "
            f"{len(legacy_score_entries or [])} historical score entries and recovered "
            f"{len(self.round_history)} recent participation rounds"
        )

    def update(self, hotkey: str, raw_score: float) -> float:
        """Record a miner's current-round score and return it."""
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid round score for {hotkey[:16]}...: {raw_score!r}")
        if not math.isfinite(score) or score <= 0.0 or score > 1.0:
            raise ValueError(f"Round score out of range for {hotkey[:16]}...: {score!r}")
        self.round_scores[hotkey] = score
        self.last_raw_scores[hotkey] = score
        return score

    def record_round(self, round_id: str, scored_hotkeys: List[str]):
        """Finalize the current round's participation set.

        Scores for miners outside ``scored_hotkeys`` are dropped so stale state
        can never leak into the next weight update.
        """
        if round_id in self._recorded_round_ids:
            logger.debug(f"Round {round_id} already recorded, skipping")
            return

        scored_set = set(scored_hotkeys)
        self.round_scores = {
            hk: score for hk, score in self.round_scores.items() if hk in scored_set
        }
        self.last_raw_scores = {
            hk: score for hk, score in self.last_raw_scores.items() if hk in scored_set
        }

        counted_hotkeys = {
            hk for hk in scored_set if self.round_scores.get(hk, 0.0) > 0.0
        }

        self.round_history.append({
            "round_id": round_id,
            "scored_hotkeys": counted_hotkeys,
        })
        self.round_history = self.round_history[-PARTICIPATION_WINDOW:]
        self._recalculate_participation()

    def _recalculate_participation(self):
        """Recalculate recent-window participation counts.

        Counts are rebuilt from the last ``PARTICIPATION_WINDOW`` entries so a
        miner that disappears eventually loses eligibility.
        """
        counts: Dict[str, int] = defaultdict(int)
        for entry in self.round_history:
            for hotkey in entry["scored_hotkeys"]:
                counts[hotkey] += 1
        self._participation_counts = counts
        self._recorded_round_ids = {entry["round_id"] for entry in self.round_history}

    def get_participation_count(self, hotkey: str) -> int:
        """Return the miner's valid scored-round count in the recent window."""
        return self._participation_counts.get(hotkey, 0)

    def is_eligible(self, hotkey: str) -> bool:
        """Return whether a miner has met the recent-window round threshold."""
        return self.get_participation_count(hotkey) >= self.min_rounds

    def _sort_by_round_score(
        self,
        hotkeys: List[str],
        submission_times: Optional[Dict[str, float]] = None,
        tolerance: float = ROUND_SCORE_TOLERANCE,
    ) -> List[str]:
        """Sort by current-round score descending, then earliest submission."""
        def _cmp(hk_a, hk_b):
            sa = self.round_scores.get(hk_a, 0.0)
            sb = self.round_scores.get(hk_b, 0.0)
            ta = submission_times.get(hk_a, float("inf")) if submission_times else float("inf")
            tb = submission_times.get(hk_b, float("inf")) if submission_times else float("inf")
            if abs(sa - sb) <= tolerance:
                return -1 if ta < tb else (1 if ta > tb else 0)
            return -1 if sa > sb else 1

        return sorted(hotkeys, key=functools.cmp_to_key(_cmp))

    def _ranked_positive_eligible(
        self,
        miner_hotkeys: List[str],
        submission_times: Optional[Dict[str, float]] = None,
    ) -> List[str]:
        """Return eligible current-round scored miners with positive scores."""
        eligible = [hk for hk in miner_hotkeys if self.is_eligible(hk)]
        return [
            hk for hk in self._sort_by_round_score(
                eligible, submission_times, tolerance=ROUND_SCORE_TOLERANCE
            )
            if self.round_scores.get(hk, 0.0) > 0
        ]

    def needs_canonical_tiebreak(
        self,
        miner_hotkeys: List[str],
        submission_times: Optional[Dict[str, float]] = None,
    ) -> bool:
        """Return True when canonical ranking can affect winner selection."""
        ranked = self._ranked_positive_eligible(miner_hotkeys, submission_times)
        if len(ranked) < 2:
            return False

        top_score = self.round_scores.get(ranked[0], 0.0)
        return any(
            (top_score - self.round_scores.get(hk, 0.0))
            <= CANONICAL_TIEBREAK_TOLERANCE + ROUND_SCORE_TOLERANCE
            for hk in ranked[1:]
        )

    def get_winner_heavy_pruning_dust_weights(
        self,
        miner_hotkeys: List[str],
        submission_times: Optional[Dict[str, float]] = None,
        *,
        burn_rate: float,
        winner_weight: float,
        dust_top_n: int,
        dust_decay: float,
        canonical_top: Optional[str] = None,
        canonical_ranking: Optional[List[str]] = None,
    ) -> Dict[str, float]:
        """Compute round-only winner-heavy validator-vector miner weights."""
        weights = {hk: 0.0 for hk in miner_hotkeys}
        if not miner_hotkeys:
            return weights

        burn_rate = float(burn_rate)
        winner_weight = float(winner_weight)
        dust_top_n = int(dust_top_n)
        dust_decay = float(dust_decay)
        miner_budget = 1.0 - burn_rate
        if not 0.0 <= burn_rate <= 1.0:
            raise ValueError(f"burn_rate must be between 0 and 1, got {burn_rate}")
        if not 0.0 <= winner_weight <= miner_budget:
            raise ValueError(
                f"winner_weight must be between 0 and miner budget "
                f"{miner_budget}, got {winner_weight}"
            )
        if dust_top_n < 1:
            raise ValueError(f"dust_top_n must be >= 1, got {dust_top_n}")
        if dust_decay < 0.0:
            raise ValueError(f"dust_decay must be >= 0, got {dust_decay}")

        ranked = self._ranked_positive_eligible(miner_hotkeys, submission_times)
        if not ranked:
            logger.warning("No positive current-round scores — returning zero miner weights")
            return weights

        winner = ranked[0]
        canonical_candidates: List[str] = []
        if canonical_ranking:
            seen = set()
            for hk in canonical_ranking:
                if not isinstance(hk, str):
                    continue
                if not hk or hk in seen:
                    continue
                canonical_candidates.append(hk)
                seen.add(hk)
        elif canonical_top is not None:
            canonical_candidates = [canonical_top]

        if canonical_candidates:
            ranked_set = set(ranked)
            top_score = self.round_scores.get(ranked[0], 0.0)
            for candidate in canonical_candidates:
                if candidate not in ranked_set:
                    continue
                if candidate == ranked[0]:
                    winner = candidate
                    break
                canonical_score = self.round_scores.get(candidate, 0.0)
                gap = top_score - canonical_score
                if gap <= CANONICAL_TIEBREAK_TOLERANCE + ROUND_SCORE_TOLERANCE:
                    winner = candidate
                    logger.info(
                        f"Canonical tiebreak: local round rank-1 was "
                        f"{ranked[0][:16]}... (score={top_score:.4f}); "
                        f"deferring to canonical winner {candidate[:16]}... "
                        f"(score={canonical_score:.4f}, gap "
                        f"{gap*100:.2f}% within "
                        f"{CANONICAL_TIEBREAK_TOLERANCE*100:.1f}% tolerance)"
                    )
                    break

        weights[winner] = winner_weight

        dust_pool = max(0.0, miner_budget - winner_weight)
        dust_recipients = [hk for hk in ranked if hk != winner][:dust_top_n - 1]
        if dust_pool > 0 and dust_recipients:
            dust_raw = [dust_decay ** i for i in range(len(dust_recipients))]
            dust_total = sum(dust_raw)
            if dust_total > 0:
                for hk, raw in zip(dust_recipients, dust_raw):
                    weights[hk] = dust_pool * raw / dust_total

        logger.info(
            f"Round-only weights: winner={winner[:16]}... "
            f"winner_weight={winner_weight:.4f}, "
            f"dust_pool={dust_pool:.4f}, dust_recipients={len(dust_recipients)}"
        )
        return weights

    def get_rankings(self, miner_hotkeys: List[str]) -> Dict[str, Optional[int]]:
        """Get current-round rankings. Unscored/zero-score miners get None."""
        ranked = self._ranked_positive_eligible(miner_hotkeys)
        rankings: Dict[str, Optional[int]] = {hk: None for hk in miner_hotkeys}
        for rank, hk in enumerate(ranked, start=1):
            rankings[hk] = rank
        return rankings

    def build_weight_history(
        self,
        round_id: str,
        validator_hotkey: str,
        miner_hotkeys: List[str],
        weights: Dict[str, float],
    ) -> List[Dict[str, Any]]:
        """Build the platform weight-history payload."""
        rankings = self.get_rankings(miner_hotkeys)

        entries = []
        for hk in miner_hotkeys:
            entries.append({
                "miner_hotkey": hk,
                "raw_score": self.last_raw_scores.get(hk),
                # Legacy platform schema field. Round-only scoring leaves it
                # empty rather than mirroring the current score.
                "ema_score": None,
                "rank": rankings.get(hk),
                "weight": weights.get(hk, 0.0),
                "eligible": self.is_eligible(hk),
                "participation_count": self.get_participation_count(hk),
            })

        return entries

    def get_stats(self) -> Dict[str, Any]:
        """Get current round statistics for logging."""
        all_hotkeys = list(self.round_scores.keys())
        score_values = list(self.round_scores.values())

        return {
            "total_miners_tracked": len(all_hotkeys),
            "eligible_count": sum(1 for hk in all_hotkeys if self.is_eligible(hk)),
            "rounds_tracked": len(self.round_history),
            "min_rounds_required": self.min_rounds,
            "top_round_score": max(score_values) if score_values else 0.0,
            "mean_round_score": sum(score_values) / len(score_values) if score_values else 0.0,
        }
