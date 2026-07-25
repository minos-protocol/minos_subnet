# Minos SN107 Protocol Rules And Rewards

Memory name: Minos SN107 - Protocol Rules And Rewards
Version: 1.0.0
Primary subject: Protocol rules
Subjects: Minos SN107; Protocol rules; Rewards and emissions; Eligibility and weight; Scoring basics; Live data boundary
Related memories: Minos SN107 - Rewards Eligibility And Weight; Minos SN107 - Scoring And AdvancedScorer; Minos SN107 - Live Data Boundary; Minos SN107 - Miner Lifecycle

This is public-safe stable knowledge for Minos subnet 107. Use live Minos MCP or public endpoints for current rounds, current weights, latest winners, or miner-specific status.

## What Minos Is

Minos is Bittensor subnet 107 for genomic variant calling. Miners run supported variant-calling tools on challenge genomic data. Validators rerun miner-submitted configs or pipelines and score the generated calls.

Miners submit variant-calling configs/pipelines through the official miner software. Miners should not manually upload raw VCF outputs for support.

## Round Cadence

Rounds are about 72 minutes. A miner can be running correctly while waiting for an open round, a scoring transition, or a finalized leaderboard.

## Eligibility

Eligibility requires 10 valid scored rounds out of the last 20 rounds, including the current round. A new miner can submit and score but still receive 0 weight until eligibility catches up.

Ineligible miners receive 0 weight even if they submit. This is expected behavior, not proof that the config is bad.

## Reward And Weight Split

Public protocol shape (live values in the network config):

- The round winner receives ~90% of the miner weight (`winner_weight = 0.9`) — the split is winner-heavy.
- Eligible ranks #2 through #20 split the remaining ~10% as pruning dust, decaying by 0.8 (`dust_top_n = 20`, `dust_decay = 0.8`).
- Burn is a configurable protocol rate, currently 0% (`burn_rate = 0.0`, sent to `burn_uid = 0`).

These are dynamic protocol values — read the current numbers from `get_network_config` rather than assuming them. If fewer eligible dust recipients exist, the dust budget spreads across the available eligible ranks; any weight that cannot be assigned to an eligible miner goes to burn.

## Beginner Interpretation

Score, eligibility, weight, and emissions are related but not the same.

Score means validators evaluated the generated calls. Eligibility means the miner has enough recent valid scored participation. Weight is assigned through validator scoring and subnet rules. Emissions are rewards that follow live chain/subnet state.

## Safe Support Rule

For current winners, live reward distribution, current weights, miner history, or subnet health, use Minos MCP or public endpoints. Do not answer live status from static @minos memory.
