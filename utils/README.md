# Minos Utils

This folder contains all the genomics processing tools used by miners and validators.

## Overview

The utils folder is organized into 6 focused modules:

```text
utils/
├── scoring.py            # Validates and scores VCF results
├── weight_tracking.py    # Tracks miner performance over time
├── file_utils.py         # Downloads and manages data files
├── platform_client.py    # Platform API client for miners/validators
├── config_loader.py      # Config file loading for miner templates
└── path_utils.py         # Safe filesystem paths for round directories
```

## Module Details

### 1. Scoring (scoring.py)

**Purpose:** Validates miner VCF files against ground truth using hap.py.

**Main Classes:** `HappyScorer`, `AdvancedScorer`

```python
from utils import HappyScorer, AdvancedScorer

scorer = HappyScorer()

# Run hap.py validation
results = scorer.score_vcf(
    truth_vcf="merged_truth.vcf.gz",
    query_vcf="miner_output.vcf",
    reference_fasta="datasets/reference/chr20/chr20.fa",
    confident_bed="confident_regions.bed"
)
# Returns:
# {
#   'f1_snp': 0.95,
#   'f1_indel': 0.88,
#   'precision_snp': 0.96,
#   'recall_snp': 0.94,
#   'weighted_f1': 0.933
# }

# Compute advanced score
score = AdvancedScorer.compute_advanced_score(results)
# Returns: 87.5 (on 0-100 scale)
```

### 2. Weight Tracking (weight_tracking.py)

**Purpose:** Tracks miner performance over time with EMA smoothing and winner-takes-all weights.

**Main Class:** `ScoreTracker`

```python
from utils import ScoreTracker

tracker = ScoreTracker(alpha=0.1)

# Update a miner's score
tracker.update(hotkey="5xxx...", raw_score=0.92)

# Get normalized weights for blockchain
weights = tracker.get_winner_takes_all_weights(miner_hotkeys=["5xxx...", "5yyy..."])
# Returns: {hotkey: weight} mapping
```

**Algorithm:**

1. **EMA Smoothing:**
2. **Winner-Takes-All:** 
3. **Participation Gating:** 
4. **Normalization:** 

### 3. File Utils (file_utils.py)

**Purpose:** Downloads and manages genomic data files.

**Main Functions:**

```python
from utils import download_file
from utils.file_utils import download_file_verified, download_file_with_fallback

# Download a file with caching
download_file(
    url="https://example.com/data.bam",
    local_path="datasets/bams/data.bam",
    use_cache=True
)

# Download with SHA256 verification
download_file_verified(
    url="https://example.com/data.bam",
    local_path="datasets/bams/data.bam",
    expected_sha256="abc123..."
)

# Download with automatic fallback to a backup URL
download_file_with_fallback(
    primary_url="https://hippius.example.com/data.bam",  # tried first
    local_path="datasets/bams/data.bam",
    backup_url="https://s3.amazonaws.com/.../data.bam",  # fallback if primary fails
    expected_sha256="abc123..."
)
```

**Features:**

- Automatic caching
- SHA256 integrity verification
- HTTP and presigned URL support
- Primary/backup URL fallback (`STORAGE_PRIMARY_BACKEND` env var: `hippius` (default) or `aws_s3`)

### 4. Platform Client (platform_client.py)

**Purpose:** API client for communicating with the Minos Platform.

**Main Classes:** `MinerPlatformClient`, `ValidatorPlatformClient`

#### Miner Client

```python
from utils import MinerPlatformClient, PlatformConfig

config = PlatformConfig(base_url="http://localhost:8000")
client = MinerPlatformClient(keypair=my_keypair, config=config)

# Poll for active round (returns presigned BAM URL when round is open)
round_status = await client.get_round_status()

# Submit variant calling config (no VCF — validators re-run the config)
await client.submit_config(round_id="uuid", tool_name="gatk", tool_config=my_config)
```

#### Validator Client

```python
from utils import ValidatorPlatformClient, PlatformConfig

config = PlatformConfig(base_url="http://localhost:8000")
client = ValidatorPlatformClient(keypair=my_keypair, config=config)

# Get rounds ready for scoring
rounds = await client.get_scoring_rounds()

# Get all miner submissions for a round (includes presigned truth VCF URL)
submissions = await client.get_round_submissions(round_id="uuid")

# Submit score after hap.py validation
await client.submit_score(
    round_id="uuid",
    miner_hotkey="5xxx",
    snp_f1=0.95,
    snp_precision=0.96,
    snp_recall=0.94,
    indel_f1=0.88,
)
```

**Features:**

- Canonical request signing (each request includes `signature` over `METHOD|PATH|BODY_HASH|TIMESTAMP|NONCE`, plus a unique `nonce`)
- Round-based scoring API

## How They Work Together

### Validator Workflow

```text
1. ValidatorPlatformClient.get_scoring_rounds()
   |
2. ValidatorPlatformClient.get_round_submissions()
   download_file_verified() -> fetch BAM + truth data
   |
3. load_template() -> template.variant_call()
   (Run miner's variant calling config)
   |
4. HappyScorer.score_vcf()
   AdvancedScorer.compute_advanced_score()
   |
5. ScoreTracker.update()
   ScoreTracker.get_winner_takes_all_weights()
   |
6. Submit to blockchain + ValidatorPlatformClient.submit_score()
```

### Miner Workflow

```text
1. MinerPlatformClient.get_round_status() -> poll for active round + presigned BAM URL
   |
2. download_file_with_fallback() -> fetch BAM
   |
3. execute_template() (GATK/DeepVariant/FreeBayes/BCFtools via Docker)
   |
4. MinerPlatformClient.submit_config()
```

## Dependencies

| Module | Requires |
|--------|----------|
| scoring.py | Docker (hap.py, bcftools) |
| weight_tracking.py | (standard library only) |
| file_utils.py | urllib (stdlib), boto3 (optional, for authenticated S3), tqdm (optional, for progress bars) |
| platform_client.py | httpx, bittensor_wallet |
| path_utils.py | (standard library only) |

## Import Guide

```python
# Scoring
from utils import HappyScorer, AdvancedScorer

# Weight tracking
from utils import ScoreTracker

# File utilities
from utils import download_file

# Path utilities
from utils import safe_round_dir_name

# Platform client
from utils import (
    PlatformConfig,
    MinerPlatformClient,
    ValidatorPlatformClient,
    PlatformClientError,
    AuthenticationError,
)
```

## Troubleshooting

### hap.py Issues

| Symptom | Fix |
|---------|-----|
| No summary CSV created | Verify VCF files are valid - a simple check is to run bcftools index {file_name}.vcf.gz |
| SDF not found | Create SDF with `rtg format` |
| Return code 1 | Often just warnings, check if CSV exists |

### Platform Client Issues

| Symptom | Fix |
|---------|-----|
| 401 Unauthorized | Invalid signature or hotkey not registered on metagraph |
| 409 Conflict | Round not open for submission (already scored or closed) |
| Connection refused | Check PLATFORM_URL |

## Learn More

- See `neurons/` for how these modules are used
- See `base/` for configuration options
- Check [main README](../README.md) for full documentation
