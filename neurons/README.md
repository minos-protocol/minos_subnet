# Minos Neurons

This folder contains the two main participants in the Minos subnet: **Miners** and **Validators**.

## Overview

In Minos, miners are rewarded for accurately calling genetic variants (finding mutations in DNA):

- **Miners** are the participants - they run variant calling on benchmark genomic data to earn rewards
- **Validators** are the judges - they score miner results and set on-chain weights

Task coordination and synthetic genome generation is handled via the Minos Platform API.

## Miner (miner.py)

### What Does a Miner Do?

A miner receives genomic data (DNA sequencing files) and finds genetic variants (mutations) in them. They are given a biological puzzle and finding all the differences from the reference is their task.

### How It Works

1. **Start Up**
   - Docker availability is verified (fails fast with actionable error if missing)
   - Connect to the Bittensor network
   - Register your hotkey to get a unique ID (or run in demo mode without registration)
   - Connect to the Minos Platform API to poll for available scoring rounds

2. **Receive a Task**
   - Poll platform for available scoring rounds
   - Contains round_id, region, and data source

3. **Process the Data**
   - Download the BAM file (raw genomic data) from platform presigned URL
   - Run variant calling tool  (GATK, DeepVariant, or BCFtools) locally with your preferred configs to ensure quality output
   - This analyzes the DNA data and identifies variants

4. **Return Results**
   - Submit tool config used to identify variants to platform
   - Validator re-runs your config to verify results
   - Include metadata (runtime, variant count)

5. **Get Rewarded**
   - Validators score your accuracy using hap.py - industry standard tool to benchmark variant calling in genomic data
   - More accurate results = higher weights
   - Earn alpha tokens based on your weight

### Running the Miner

The simplest way — handles wallet setup on first run:

```bash
bash start-miner.sh
```

The start script supports flags to pre-fill or override settings:

```bash
bash start-miner.sh --wallet-name miner --miner-template deepvariant
bash start-miner.sh --setup              # Re-run setup wizard with current defaults
bash start-miner.sh --help               # Show all options
```

Or manually:

```bash
python -m neurons.miner \
  --netuid 107 \
  --subtensor.network finney \
  --wallet.name my_wallet \
  --wallet.hotkey my_hotkey
```

Or use the `.env` file:
```bash
cp .env.miner.example .env
# Edit .env with your wallet details
python -m neurons.miner
```

### Miner Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `NETUID` | 107 | Subnet UID |
| `WALLET_NAME` | default | Bittensor wallet name |
| `WALLET_HOTKEY` | default | Bittensor hotkey name |
| `PLATFORM_URL` | https://api.theminos.ai | Platform API URL |
| `PLATFORM_TIMEOUT` | 60 | Platform API request timeout (seconds) |
| `MINER_TEMPLATE` | gatk | Variant calling tool: `gatk`, `deepvariant`, or `bcftools` (freebayes deprecated 2026-05-09) |
| `STORAGE_PRIMARY_BACKEND` | hippius | Storage preference: `hippius` tries Hippius SN75 first (Bittensor decentralized, recommended). `aws_s3` reverses the order. |

## Validator (validator.py)

### What Does a Validator Do?

A validator uses miners config file and selected hyperparameters to run the variant calling algorithm, scores their answers, and decides how rewards are distributed:

### How It Works

1. **Start Up**
   - Connect to the Bittensor network
   - Register as a validator (requires `metagraph.validator_permit` — the Bittensor flag set when a neuron has enough stake to set weights)
   - Load reference genomic data
   - Connect to Minos Platform (authenticated via keypair signature)

2. **Run and Score the Results** (subset-based scoring)
   - Fetch scoring assignment from platform (primary miner range based on validator stake)
   - Score primary miners first (no deadline pressure)
   - Score secondary miners for gap coverage until 3 min before deadline
   - Re-run each miner's tool config locally (independent verification)
   - Run hap.py to compare against known truth
   - Calculate quality scores for SNPs and INDELs

3. **Backfill and Update Weights**
   - After scoring window closes, fetch peer scores for miners not personally covered
   - Track personal and backfilled current-round AdvancedScorer scores
   - Record round participation once with the complete personal + backfilled hotkey set
   - Compute winner-heavy pruning dust weights from eligible current-round scores
   - Submit weight history to the platform for aggregation/dashboarding
   - Submit weights to Bittensor blockchain only when the validator hotkey is registered

### Running the Validator

The simplest way — handles wallet setup on first run:

```bash
bash start-validator.sh
```

The start script supports flags to pre-fill or override settings:

```bash
bash start-validator.sh --wallet-name validator
bash start-validator.sh --setup           # Re-run setup wizard with current defaults
bash start-validator.sh --help            # Show all options
```

Or manually:

```bash
python -m neurons.validator \
  --netuid 107 \
  --subtensor.network finney \
  --wallet.name my_validator \
  --wallet.hotkey default
```

Or use the `.env` file:
```bash
cp .env.validator.example .env
# Edit .env with your wallet details
python -m neurons.validator
```

### Validator Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `NETUID` | 107 | Subnet UID |
| `WALLET_NAME` | default | Bittensor wallet name |
| `WALLET_HOTKEY` | default | Bittensor hotkey name |
| `PLATFORM_URL` | https://api.theminos.ai | Platform API URL |
| `PLATFORM_TIMEOUT` | 60 | Platform API request timeout (seconds) |
| `STORAGE_PRIMARY_BACKEND` | hippius | Storage preference: `hippius` tries Hippius SN75 first (Bittensor decentralized, recommended). `aws_s3` reverses the order. |
| `MIN_PARTICIPATION_ROUNDS` | 5 | Valid scored rounds required inside the recent eligibility window |
| `PARTICIPATION_WINDOW` | 20 | Recent finalized rounds considered for eligibility |
| `SCORING_THREADS` | auto | Override: threads per scoring Docker job. Auto-tuned from cores (clamped 2–8) |
| `SCORING_MEMORY_GB` | 16 | Override: memory per scoring Docker job. **Below 16 OOM-crashes DeepVariant** |
| `MINOS_VALIDATOR_CONCURRENCY` | auto | Override: concurrent miner jobs. Auto = `min(cores//threads, ram_gb//16, 8)` |
| `NETWORK` | finney | Chain to connect to. Read after CLI parsing, so a value here overrides `--subtensor.network` |
| `SCORE_GRACE_SECONDS` | 60 | Seconds waited past the round deadline before scoring. Combined with the network-supplied value using `max()`, so a large local value delays your own backfill and weight-setting |
| `SCORE_FINALIZATION_DELAY_SECONDS` | 5 | Delay between scoring and finalization. Also combined with `max()` |
| `MINOS_OWNER_SYNC_TIMEOUT` | 30 | Timeout on the pre-weights metagraph sync. On timeout the validator proceeds with the last synced hotkeys and permits, and skips coldkey deduplication for that round |
| `CANONICAL_MIN_VALIDATOR_COUNT` | 2 | Distinct validators required before the canonical ranking is used for the winner tiebreak. Network configuration overrides this when it publishes a value |
| `CANONICAL_MIN_VALIDATOR_STAKE` | 5000 | Combined with the count above as one aggregate floor: the ranking is used only when total considered stake reaches `count x stake`. Below that the validator falls back to its own ordering. Network configuration overrides this too |
| `MINOS_FALLBACK_PRIMARY_N` | auto | Override: how many miners are scored as primary when the platform returns no assignment |

## The Complete Cycle

```text
┌─────────────────────────────────────────────────────────────────┐
│                          PLATFORM                               │
│  • Generate synthetic genomes using HelixForge pipeline         │
│  • Task coordination                                            │
│  • Presigned URLs for file transfers                            │
│  • Miner registration & tracking                                │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│                          VALIDATOR                               │
│  1. Poll platform for available scoring rounds                   │
│  2. Get platform provided BAMs                                   │
│  3. Re-run miner configs, score VCFs with hap.py                 │
│  4. Submit scores and set weights on chain                       │
└──────────────────────────┬──────────────────────────────────────┘
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
    ┌──────────┐     ┌──────────┐     ┌──────────┐
    │ MINER 1  │     │ MINER 2  │     │ MINER N  │
    │          │     │          │     │          │
    │ Download │     │ Download │     │ Download │
    │ Run tool │     │ Run tool │     │ Run tool │
    │ Submit   │     │ Submit   │     │ Submit   │
    └────┬─────┘     └────┬─────┘     └────┬─────┘
         │                │                │
         └────────────────┼────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                          VALIDATOR                               │
│  5. Re-run each miner's tool config locally                      │
│  6. Score with hap.py against merged truth                       │
│  7. Score current round and compute pruning dust weights         │
│  8. Submit weights to blockchain                                 │
└──────────────────────────┬──────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                    BITTENSOR BLOCKCHAIN                          │
│  • Stores weights                • Calculates emissions          │
│  • Distributes TAO rewards       • Maintains consensus           │
└─────────────────────────────────────────────────────────────────┘
```

### Weight Distribution

Validators apply the `/scoring/network-config` reward policy (dynamic — check it
for current values). The top eligible miner gets the `winner_weight` share and
eligible ranks #2 through `dust_top_n` split the remainder as ranked pruning
dust; `burn_rate` goes to burn.
Eligibility requires at least 5 valid scored rounds in the last 20 finalized rounds, and
the current round counts toward that threshold. Eligible miners are ranked only
by the current round's AdvancedScorer score; ineligible miners receive 0
weight.

**Tiebreaks.** Exactly equal scores (within `1e-9`) are broken by earliest
submission timestamp, then by hotkey so every validator orders them the same
way. Separately, when a canonical cross-validator ranking is available its
winner is adopted if that miner's score is within `0.001` of local rank-1 — 0.1
points on the raw 0–100 scale — so a miner who is marginally ahead locally can
still not receive the winner share. The canonical ranking is used only when it
carries at least `CANONICAL_MIN_VALIDATOR_COUNT` validators and, in aggregate,
`count × CANONICAL_MIN_VALIDATOR_STAKE` of stake; below that the validator uses
its own ordering.

**One reward per coldkey.** When `/scoring/network-config` sets
`reward_normalization: true`, only each coldkey's best-ranked hotkey stays in
the reward set — every other hotkey under that coldkey earns 0 for the round,
whatever its own score. Ownership is read from the metagraph, falling back to
`POST /v2/owner-map`; if neither resolves, the deduplication is skipped for that
round rather than guessing.

## Requirements

### Both Need

- Bittensor wallet with registered hotkey
- Docker installed (for running genomics tools)
- Network connection to Bittensor network
- Internet access to platform API

### Miner Needs

- Docker image for chosen template (e.g., `broadinstitute/gatk:4.5.0.0`)
- 8GB+ RAM (16GB+ required for DeepVariant template)
- Storage for temporary BAM/VCF files
- Outbound internet access to platform API (no inbound ports required)

### Validator Needs

- Reference genomic data (~9GB for chr1-chr22)
- hap.py Docker image: `genonet/hap-py` (SHA256-pinned for reproducibility)
- bcftools/samtools Docker images
- 100GB+ storage for datasets and temporary files
- 32GB+ RAM

## Troubleshooting

### Miner Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| BAM index not found | Missing .bai file | Run `samtools index {file_name}.bam` |
| Tool timeout | Slow processing | Increase `variant_calling_timeout` in `base/genomics_config.py` or adjust `MINER_CONFIG["num_threads"]`; `configs/<tool>.conf` contains quality parameters only |
| Platform 404 on round-status | Not registered | Ensure miner hotkey is active in metagraph |

### Validator Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| hap.py zero scores | VCF format issue | Ensure single-sample VCF or non-corrupted VCF |
| Platform 409 error | Round not in scoring phase | Check that the round is in the scoring window |
| No miners available | Empty metagraph | Wait for miners to register |

## Demo Mode

The platform runs in **demo mode** before going live. In demo mode:
- Registration is **not required** — anyone can test their setup
- Variant calling runs normally on a sample BAM file
- Submission is disabled (scores are not recorded)
- A "DEMO COMPLETE" message confirms your system is ready

Use `bash scripts/verify.sh --miner` to check prerequisites, or `bash scripts/demo.sh` to run a full end-to-end demo round.

## Learn More

- [docs/tuning_guide.md](../docs/tuning_guide.md) — Parameter tuning, scoring breakdown, strategy
- See `utils/` folder for genomics processing tools
- See `base/` folder for configuration options
- Check [main README](../README.md) for full documentation
