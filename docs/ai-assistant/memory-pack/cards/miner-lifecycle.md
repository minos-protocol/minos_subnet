# Minos SN107: Miner Lifecycle

Memory name: Minos SN107 - Miner Lifecycle
Version: 1.0.0
Primary subject: Miner onboarding
Subjects: Miner onboarding; Demo mode; Practice mode; Live mining; Bittensor wallet and hotkey; PM2 supervision; Docker runtime; Eligibility and weight
Related memories: Minos SN107 - Miner Quickstart; Minos SN107 - Demo Mode; Minos SN107 - Practice Mode; Minos SN107 - Live Mining And Registration; Minos SN107 - PM2 And Docker Runtime; Minos SN107 - Rewards Eligibility And Weight

A normal Minos miner lifecycle is:

1. Install the public Minos miner code.
2. Make sure Docker works.
3. Create or attach a Bittensor wallet and hotkey.
4. Run demo mode first.
5. Score a config in practice mode.
6. Register the hotkey on subnet 107 when ready.
7. Start live mining through the official script.
8. Watch public score, eligibility, weight, and history.
9. Tune carefully only after valid scored results exist, and measure each change in practice mode first.

Demo success does not guarantee live rewards, but it is the right first gate because it confirms the local runtime can complete the workflow. Demo mode does not persist live submissions, does not earn TAO, and does not produce real live scores. Practice mode is the same self-scorer over a menu of fully-answered samples; it also stays off-chain, so neither mode adds a scored round or moves a miner toward eligibility.

PM2 "online" means the process manager sees a running process. It does not prove the miner is registered, submitting, scoring, eligible, weighted, or earning.

For current miner status, use Minos MCP or public endpoints. Static `@minos` memory should explain how to interpret the results.
