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
   - Connect to the Bittensor network
   - Register your hotkey to get a unique ID
   - Connect to the Minos Platform API to poll for available scoring rounds

2. **Receive a Task**
   - Poll platform for available scoring rounds
   - Contains round_id, region, and data source

3. **Process the Data**
   - Download the BAM file (raw genomic data) from platform presigned URL
   - Run variant calling tool  (GATK, DeepVariant, FreeBayes, or BCFtools) locally with your preferred configs to ensure quality output
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
| `NETWORK` | finney | Bittensor network |
| `WALLET_NAME` | default | Bittensor wallet name |
| `WALLET_HOTKEY` | default | Bittensor hotkey name |
| `PLATFORM_URL` | https://api.theminos.ai | Platform API URL |
| `PLATFORM_TIMEOUT` | 60 | Platform API request timeout (seconds) |
| `MINER_TEMPLATE` | gatk | Variant calling tool: `gatk`, `deepvariant`, `freebayes`, or `bcftools` |
| `STORAGE_PRIMARY_BACKEND` | hippius | File download order: `hippius` tries Hippius SN75 first (fallback S3), `aws_s3` tries S3 first |

## Validator (validator.py)

### What Does a Validator Do?

A validator uses miners config file and selected hyperparameters to run the variant calling algorithm, scores their answers, and decides how rewards are distributed:

### How It Works

1. **Start Up**
   - Connect to the Bittensor network
   - Register as a validator (requires `metagraph.validator_permit` — the Bittensor flag set when a neuron has enough stake to set weights)
   - Load reference genomic data
   - Connect to Minos Platform (authenticated via keypair signature)

2. **Run and Score the Results**
   - Re-run each miner's tool config locally (trustless verification)
   - Run hap.py to compare against known truth
   - Calculate quality scores for SNPs and INDELs
   - Compute advanced score that will be used to assign the weights to the blockchain

3. **Update Weights**
   - Track scores with EMA (exponential moving average)
   - Winner-takes-all: best eligible miner gets 100% weight
   - Submit weights to Bittensor blockchain
   - Submit scores to platform for aggregation

### Running the Validator

The simplest way — handles wallet setup on first run:

```bash
bash start-validator.sh
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
| `NETWORK` | finney | Bittensor network |
| `WALLET_NAME` | default | Bittensor wallet name |
| `WALLET_HOTKEY` | default | Bittensor hotkey name |
| `PLATFORM_URL` | https://api.theminos.ai | Platform API URL |
| `PLATFORM_TIMEOUT` | 60 | Platform API request timeout (seconds) |
| `STORAGE_PRIMARY_BACKEND` | hippius | File download order: `hippius` tries Hippius SN75 first (fallback S3), `aws_s3` tries S3 first |
| `EMA_ALPHA` | 0.1 | EMA smoothing factor (higher = more weight on recent scores) |
| `EMA_DECAY_FACTOR` | 0.95 | EMA decay multiplier applied per missed round |
| `SCORING_THREADS` | 4 | Fixed thread count for reproducible scoring |
| `SCORING_MEMORY_GB` | 8 | Fixed memory (GB) for scoring Docker containers |

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
│  7. Update EMA scores, winner-takes-all weights                  │
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

Weights are assigned in two phases:

**Warmup** (until any miner has scored in ≥10 rounds): reward is split among the top 3 miners by EMA score — 50% to 1st, 30% to 2nd, 20% to 3rd. Scores within 0.5% of each other are tiebroken by earliest submission time.

**Normal** (once any miner reaches eligibility): the single top-performing eligible miner by EMA receives 100% of the weight. Eligibility requires scoring in at least 10 of the last 20 rounds. Absent miners' EMA decays each round they miss (×0.95). Tiebreaker: earliest submission timestamp (applied only at floating-point tolerance).

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
- hap.py Docker image: `genonet/hap-py:0.3.15`
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

## Learn More

- See `utils/` folder for genomics processing tools
- See `base/` folder for configuration options
- Check [main README](../README.md) for full documentation
