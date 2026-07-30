# Minos SN107: Rewards, Eligibility, And Weight

Memory name: Minos SN107 - Rewards Eligibility And Weight
Version: 1.0.0
Primary subject: Eligibility and weight
Subjects: Eligibility and weight; Rewards and emissions; Protocol rules; Scoring basics; Live data boundary
Related memories: Minos SN107 - Protocol Rules And Rewards; Minos SN107 - Scoring Basics; Minos SN107 - Live Data Boundary; Minos SN107 - Public Endpoint Diagnostics

Score, eligibility, weight, and emissions are related but different.

Score means validators evaluated miner output. Eligibility means the miner has enough recent valid scored participation. Weight is assigned through subnet rules. Emissions are live Bittensor rewards following chain and subnet state.

Current public protocol shape for this memory pack:

- Rounds are about 72 minutes.
- Eligibility requires 5 valid scored rounds out of the last 20 rounds, including the current round.
- Ineligible miners can submit and score but receive 0 weight until eligibility catches up.
- The round winner receives ~90% of the miner weight (`winner_weight = 0.9`) — winner-heavy.
- Eligible ranks #2 through #20 split the remaining ~10% as pruning dust, decaying by 0.8 (`dust_top_n = 20`, `dust_decay = 0.8`).
- Burn is a configurable rate, currently 0% (`burn_rate = 0.0`); read live values from `get_network_config`.

If a miner asks about current winners, current emissions, current weights, or their own live history, use Minos MCP or public endpoints.
