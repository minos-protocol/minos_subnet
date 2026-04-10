```
  A---T
 { \ / }
  \ X /
  / X \     __  __ _
 { / \ }   |  \/  (_)_ __   ___  ___
  G---C    | |\/| | | '_ \ / _ \/ __|
 { \ / }   | |  | | | | | | (_) \__ \
  \ X /    |_|  |_|_|_| |_|\___/|___/
  / X \
 { / \ }
  T---A
```

# Minos – Decentralized Genomic Variant Calling & Benchmarking Platform

Minos (SN107) is a subnet for genomic variant calling and benchmarking powered by Bittensor. Every 72 minutes, the platform generates a fresh challenge genome (BAM file) containing hidden synthetic mutations injected using HelixForge at read level. Miners are rewarded for performing hyperparameter search and providing configurations for state-of-the-art variant calling tools that can accurately identify these hidden mutations in the genome. Once the hyperparameter space has been saturated, miners will compete to provide their own custom algorithms to identify mutations. Validators are responsible for downloading miner's hyperparam config and they will run each miner's submitted config, and evaluate the results using industry standard tools and approaches such as hap.py. Miners will never be asked to upload outputs, they submit their variant-calling configuration (and pipelines in later stages), which the validator executes trustlessly.

> **Subnet 107** on Bittensor mainnet (finney).

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Repository Layout](#repository-layout)
- [System Prerequisites](#system-prerequisites)
- [Quick Start](#quick-start)
- [Validator Setup](#validator-setup)
- [Miner Setup](#miner-setup)
- [Platform Service](#platform-service)
- [Scoring System](#scoring-system)
- [Monitoring & Troubleshooting](#monitoring--troubleshooting)
- [Additional Documentation](#additional-documentation)

---

## Architecture Overview

```
┌────────────────────────────────────────────────────────────-─┐
│                    MINOS PLATFORM                            │
│              (Task Coordination & File Conveyance)           │
├────────────────────────────────────────────────────────────-─┤
│  • Prepares BAMs with synthetic mutations using HelixForge   │
│  • Presigned URL generation for BAM transfer                 │
│    (AWS S3 + Hippius SN75 decentralized storage)             │
│  • Continuous 72-minute rounds aligned to Bittensor tempo    │
│  • Lag scoring: miners submit in cycle N, validators score   │
│    cycle N while miners work on cycle N+1                    │
│  • Miner registration & status tracking                      │
└──────────────┬──────────────────────────────┬────────────────┘
               │                              │
    ┌───────────▼────────────────┐      ┌──────▼─────────────────────-------------------┐
    │     VALIDATOR              │      │      MINER                                    │
    │  • Downloads BAMs          │      │  • Download and run variant caller on BAM     │
    │  • Runs miner config       │      │ • Submits hyperparam config from their run    │
    │  • Scores with hap.py      │      │  • Top performer wins                         │
    │  • Sets blockchain weights │      └────────────────────────────-------------------┘
    └────────────────────────────┘
               │
    ┌──────────▼──────────┐
    │     BITTENSOR       │
    │    BLOCKCHAIN       │
    │  • Weight storage   │
    │  • Emission calc    │
    │  • Alpha emissions  │
    └─────────────────────┘
```

---

## Repository Layout

```
minos_subnet/
├── neurons/                  # Bittensor neuron entrypoints
│   ├── validator.py          # Full validator orchestration
│   ├── miner.py              # Miner loop: poll, download, call variants, submit config
│   ├── status.py             # Health checks and system status
│   └── README.md             # Neurons documentation
├── templates/                # Variant-calling tool templates
│   ├── gatk.py               # GATK HaplotypeCaller template
│   ├── deepvariant.py        # Google DeepVariant template
│   ├── freebayes.py          # FreeBayes template
│   ├── bcftools.py           # BCFtools mpileup/call template
│   └── tool_params.py        # Parameter definitions and validation
├── utils/                    # Genomics utility modules
│   ├── scoring.py            # hap.py validation and scoring
│   ├── weight_tracking.py    # EMA score tracking
│   ├── file_utils.py         # File download and management
│   ├── platform_client.py    # Platform API client for miners/validators
│   └── README.md             # Utils documentation
├── base/                     # Core subnet config
│   ├── genomics_config.py    # Genomics settings (Docker images, timeouts, EMA params)
│   └── s3_manifest.json      # Reference data paths (local + S3)
├── configs/                  # Miner-tunable quality parameters
│   ├── gatk.conf
│   ├── deepvariant.conf
│   ├── freebayes.conf
│   └── bcftools.conf
├── docs/                     # Architecture and integration docs
├── install.sh                # Installer (full setup or update mode)
├── setup.py                  # Interactive setup wizard
├── start-miner.sh            # Start miner (with inline wallet setup)
├── start-validator.sh        # Start validator (with inline wallet setup)
├── requirements.txt          # Python dependencies
├── .env.miner.example        # Miner environment configuration
├── .env.validator.example    # Validator environment configuration
└── README.md                 # This document
```

---

## System Prerequisites

| Component | Requirement | Notes |
|-----------|-------------|-------|
| OS | Linux (Ubuntu 20.04+), macOS 13+ | Docker + Bittensor run best on Linux |
| CPU/RAM (Validator) | ≥8 cores / 32 GB RAM | hap.py scoring benefits from cores |
| CPU/RAM (Miner) | ≥4 cores / 8–16 GB RAM | 8 GB for BCFtools/FreeBayes, 16 GB for DeepVariant |
| Disk | ≥60 GB (miner) / ≥100 GB (validator) | Reference data ~9 GB + temporary files |
| Docker | 24.0+ | Required for GATK, hap.py, bcftools |
| Python | 3.10+ | We test on 3.12 |
| Bittensor | Latest pip install | Provides wallet/subtensor/dendrite APIs |

---

## Quick Start

```bash
git clone https://github.com/minos-protocol/minos_subnet.git
cd minos_subnet
bash install.sh          # First-time: full setup (venv, deps, Docker, reference data, wallet)
bash start-miner.sh      # Start as miner
bash start-validator.sh  # Start as validator
```

The `start-*.sh` scripts handle wallet setup on first run — no manual `.env` editing needed. If you already ran `install.sh` before, running it again will only update dependencies and download any new reference data (use `--fresh` to redo everything).

The platform is in **demo mode** — you can run the miner immediately to test your pipeline without registering on the subnet. Register when you're ready to earn alpha.

**MinosVM:** If using the MinosVM Docker image, everything is pre-installed. Just SSH in and run `bash start-miner.sh` or `bash start-validator.sh`.

### Updating

Already running Minos? Pull the latest code and run the installer — it detects your existing setup and only downloads new reference data:

```bash
git pull
bash install.sh
```

Your wallet, `.env`, and existing data are preserved. Use `bash install.sh --fresh` to redo a full setup if needed.

<details>
<summary>Manual setup (if you prefer not to use the install script)</summary>

#### 1. Setup Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

#### 2. Configure Environment

```bash
cp .env.miner.example .env    # for miners
cp .env.validator.example .env # for validators
```

#### 3. Pull Docker Images

```bash
docker pull broadinstitute/gatk:4.5.0.0
docker pull google/deepvariant:1.5.0
docker pull staphb/freebayes:1.3.7
docker pull genonet/hap-py:0.3.15
docker pull quay.io/biocontainers/bcftools:1.20--h8b25389_0
docker pull quay.io/biocontainers/samtools:1.20--h50ea8bc_0
```

#### 4. Download Reference Data

```bash
# Downloads from our public S3 bucket (see base/s3_manifest.json for URLs)
python setup.py
```

</details>

---

## Validator Setup

### Environment Variables

```bash
# Network
NETUID=107
NETWORK=finney

# Wallet
WALLET_NAME=validator
WALLET_HOTKEY=default

# Platform
PLATFORM_URL=https://api.theminos.ai
PLATFORM_TIMEOUT=60

# Storage backend preference (hippius = Hippius SN75 first, aws_s3 = S3 first) # we recommend hippius.
STORAGE_PRIMARY_BACKEND=hippius
```

### Running the Validator

```bash
bash start-validator.sh
```

Or manually:

```bash
source .venv/bin/activate
python -m neurons.validator \
  --netuid 107 \
  --subtensor.network finney \
  --wallet.name validator \
  --wallet.hotkey default
```

### Validator Workflow

1. **Fetch Rounds**: Poll platform for scoring rounds with prepared mutated BAMs
2. **Download BAM**: Download mutated BAM from platform via presigned URL
3. **Run Miner Tools**: Execute each miner's variant calling config via templates
4. **Scoring**: Validate generated variant call file (VCFs) with hap.py against truth data provided by the platform
5. **Submit Scores**: Report scores back to platform
6. **Weight Update**: Set weights on-chain via Bittensor

---

## Miner Setup

### Environment Variables

```bash
# Network
NETUID=107
NETWORK=finney

# Wallet
WALLET_NAME=miner
WALLET_HOTKEY=default

# Miner
MINER_TEMPLATE=gatk

# Platform
PLATFORM_URL=https://api.theminos.ai
PLATFORM_TIMEOUT=60

# Storage backend preference (hippius = Hippius SN75 first, aws_s3 = S3 first) # we recommend hippius
STORAGE_PRIMARY_BACKEND=hippius
```

### Running the Miner

```bash
bash start-miner.sh
```

Or manually:

```bash
source .venv/bin/activate
python -m neurons.miner \
  --netuid 107 \
  --subtensor.network finney \
  --wallet.name miner \
  --wallet.hotkey default
```

### Miner Workflow

1. **Registration**: Register with platform via hotkey authentication
2. **Task Poll**: Poll platform for pending evaluation tasks
3. **BAM Download**: Fetch benchmark BAM from platform via presigned URL
4. **Variant Calling**: Run configured variant caller (GATK, DeepVariant, freebayes, or bcftools)
5. **Config Submit**: Submit tool config you've used (hyperparameters only based on the template)
6. **Reward**: Earn alpha based on accuracy score — validators re-run the config to verify

---

## Platform Service

The Minos Platform is a hosted service at `https://api.theminos.ai` that handles BAM generation and synthetic mutation injection with HelixForge, file transfers via presigned URLs, and round coordination. Validators and miners connect automatically via the `PLATFORM_URL` environment variable — no self-hosting required.

### Key Endpoints

| Endpoint                      | Used by   |               Description                    |
| ------------------------------|-----------|----------------------------------------------|
| `POST /v2/round-status`       | Miner     | Poll for active rounds and presigned BAM URL |
| `POST /v2/submit-config`      | Miner     | Submit variant-calling tool config           |
| `POST /v2/get-scoring-rounds` | Validator | Fetch rounds ready for scoring               |
| `POST /v2/get-submissions`    | Validator | Fetch all miner configs for a round          |
| `POST /v2/submit-score`       | Validator | Submit per-miner scores                      |

---

## Scoring System

### hap.py Validation

Validators run each miner's tool config and score the resulting VCF from that against the truth data shared by the platform with them using hap.py. Scores are combined by Minos' developed `AdvancedScorer` into a scaled 0–100 final score that balances accuracy and precision with the following components:

| Component    | Weight |
| -------------|--------|
| Core F1      | 60%    |
| Completeness | 15%    |
| FP Rate      | 15%    |
| Quality      | 10%    |

### EMA Weight Tracking

Scores are smoothed over time using Exponential Moving Average:

```python
# EMA starts at 0; first round yields 10% of first score
ema = (1 - alpha) * ema + alpha * raw_score
# alpha = 0.1 (10% weight on new scores)
```

### Weight Distribution

Weights are assigned in two phases:

**Warmup** (until any miner has scored in ≥10 rounds): reward is split among the top 3 miners by EMA score — 50% to 1st, 30% to 2nd, 20% to 3rd. We want to do this to incentivise participation while the network bootstraps at the beginning.

**Normal** (once any miner reaches eligibility): the single top-performing miner by EMA receives 100% of the weight. Eligibility requires scoring in at least 10 of the last 20 rounds. Absent miners' EMA decays each round they miss (×0.95), preventing stale scores from holding weight indefinitely.

In the warmup phase, miners with scores within 0.5% of each other are tiebroken by earliest config submission time. In the normal phase, tiebreaks only apply when EMA scores are essentially identical (floating-point tolerance).

---

## Monitoring & Troubleshooting

### Common Issues

| Symptom                     |   Cause                            |  Fix                                                |
|-----------------------------|------------------------------------|-----------------------------------------------------|
| `docker: permission denied` | User not in docker group           | `sudo usermod -aG docker $USER && newgrp docker`    |
| `GATK timeout`              | Insufficient resources             | Increase threads/memory or timeout                  |
| `Platform 401 error`        | Invalid sig or unregistered hotkey | Ensure wallet hotkey is registered on the metagraph |
| `No miners available`       | No registered miners               | Check metagraph for active miners                   |
| `hap.py zero scores`        | VCF format issues                  | Ensure single-sample VCF output                     |

### Logs

```bash
# Validator logs
journalctl -u minos-validator -f

# Miner logs
journalctl -u minos-miner -f
```

### Health Checks

```bash
# Platform health
curl <PLATFORM_URL>/health

# Metagraph status
btcli subnet metagraph --netuid 107
```

---

## Additional Documentation

- [neurons/README.md](neurons/README.md) - Detailed miner/validator documentation
- [utils/README.md](utils/README.md) - Utility modules reference
- [docs/architecture.md](docs/architecture.md) - System architecture deep dive
- [docs/hap_py_docker.md](docs/hap_py_docker.md) - hap.py Docker image reference

---

## Links

- **GitHub**: [github.com/minos-protocol/minos_subnet](https://github.com/minos-protocol/minos_subnet)
