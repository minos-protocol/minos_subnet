# Minos SN107 Glossary For Beginners

Memory name: Minos SN107 - Glossary For Beginners
Version: 1.0.0
Primary subject: Minos SN107
Subjects: Minos SN107; Miner onboarding; Practice mode; Validators; Variant calling; Scoring basics; Rewards and emissions; Public endpoints
Related memories: Minos SN107 - What Minos Is; Minos SN107 - Variant Calling Basics; Minos SN107 - Scoring Basics; Minos SN107 - Rewards Eligibility And Weight

Use this public glossary to explain Minos terms simply.

## Minos And Bittensor

Minos subnet 107: A Bittensor subnet for genomic variant calling.

Bittensor: The blockchain network that coordinates subnets, miners, validators, and emissions.

Miner: A participant that runs a variant caller and submits a tool config.

Validator: A participant that re-runs miner configs, scores results, and sets weights.

AdvancedScorer: Minos scoring logic that combines benchmark comparison signals into subnet scoring behavior. Explain it at a high level unless the user is reading public code.

UID: A miner or validator's numeric identity on the subnet.

Hotkey: The Bittensor identity used by the miner or validator process.

Wallet: The local Bittensor wallet that owns keys. Never share private keys or seed phrases.

## Miner Process

PM2: A process manager that keeps the miner running. PM2 online does not prove scoring.

Docker: Container runtime used to run genomics tools.

Demo mode: A sandbox run that proves the pipeline works. It does not persist live submissions, earn TAO, or produce real live scores. It is the self-scorer pinned to one fixed chr20 sample.

Practice mode: An off-chain self-scoring run against a chosen fully-answered chr18-chr22 sample. It prints the exact score a validator would compute for a config, using the scoring version the platform advertises, but submits nothing, earns no TAO, and does not count toward eligibility.

Practice sample: A platform-published sample that ships with its truth files so a miner can score itself locally. Sample files are kept under `datasets/practice/<sample_id>/`.

Live mining: Real subnet participation with wallet/hotkey, scoring, eligibility, weight, and emissions.

Round: A time window where miners work on challenge data and submit configs.

## Genomics

BAM: A file containing aligned sequencing reads.

VCF: A file containing variant calls.

Reference genome: The baseline genome used for comparison.

Read: A short DNA sequence fragment.

Variant: A difference from the reference genome.

SNP: A single-base change.

Indel: An insertion or deletion.

Variant caller: Software that turns BAM data into VCF calls.

GATK: A configurable genomics toolkit.

DeepVariant: A machine-learning-based variant caller.

BCFtools: A fast command-line toolkit for variant calling and VCF processing.

hap.py: A benchmarking tool used by validators to compare called variants against truth data.

## Scoring

True positive: A real variant correctly called.

False positive: A called variant that is not real.

False negative: A real variant that was missed.

Precision: How many called variants were correct.

Recall: How many real variants were found.

F1: A balance of precision and recall.

Eligibility: Whether enough recent valid scored participation exists to receive weight.

Weight: Validator-assigned chain weight.

Emissions: Rewards distributed by Bittensor.

Eligibility: The public protocol requirement for enough recent valid scored participation before a miner can receive weight.

Pruning dust: The small reward share distributed across eligible ranks below the top miner.

Burn: The portion of subnet weight that is sent to burn instead of a miner.

Live data: Current round, leaderboard, score, eligibility, weight, emissions, and validator state. Use Minos MCP or public endpoints for live data.

## Sensitive Terms

Do not paste private keys, seed phrases, `.env` values, API keys, signatures, active nonces, presigned URLs, unreleased truth files, admin endpoints, or database credentials into public support channels.
