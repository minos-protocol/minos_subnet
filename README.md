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
- [Running with PM2 (optional)](#running-with-pm2-optional)
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
│    (Hippius SN75 + Cloudflare R2 + AWS S3 fallback)          │
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
│   ├── miner.py              # Miner loop: poll, download, call variants, submit config
│   ├── validator.py          # Validator loop: subset scoring, set chain weights
│   ├── status.py             # Health checks and system status
│   └── README.md             # Neurons documentation
├── templates/                # Variant-calling tool templates
│   ├── gatk.py               # GATK HaplotypeCaller template
│   ├── deepvariant.py        # Google DeepVariant template
│   ├── freebayes.py          # FreeBayes template (DEPRECATED 2026-05-09; retained so in-flight pre-cutover rounds can still be scored; will be removed in a follow-up release)
│   ├── bcftools.py           # BCFtools mpileup/call template
│   ├── _common.py            # Shared template utilities
│   └── tool_params.py        # Parameter definitions and validation
├── utils/                    # Genomics utility modules
│   ├── scoring.py            # hap.py Docker runner + AdvancedScorer
│   ├── weight_tracking.py    # Round score tracker + winner-heavy pruning dust weights
│   ├── platform_client.py    # Authenticated API client (miner + validator)
│   ├── subset_scoring.py     # Subset scoring helpers (assignments, deadlines)
│   ├── config_loader.py      # Tool config file parser
│   ├── path_utils.py         # Safe filesystem paths
│   ├── file_utils.py         # SHA256-verified file download + caching
│   └── README.md             # Utils documentation
├── base/                     # Core subnet config
│   └── genomics_config.py    # Central config (Docker images, timeouts, scoring params)
├── configs/                  # Miner-tunable quality parameters
│   ├── gatk.conf
│   ├── deepvariant.conf
│   ├── freebayes.conf        # DEPRECATED 2026-05-09; retained for legacy parsing
│   └── bcftools.conf
├── docs/                     # Architecture and integration docs
│   ├── architecture.md       # System architecture deep dive
│   ├── tuning_guide.md       # Miner tuning reference (scoring, parameters, strategy)
│   └── hap_py_docker.md      # hap.py Docker image reference
├── scripts/                  # Developer tools
│   ├── verify.sh             # Pre-flight environment check
│   ├── demo.sh               # End-to-end demo runner
│   └── auto_update.sh        # PM2 auto-update worker used by enable_auto_update.sh
├── tests/                    # Unit and integration tests
│   ├── conftest.py
│   └── test_*.py             # Tests for scoring, config, platform client, etc.
├── install.sh                # Installer (full setup or update mode)
├── enable_auto_update.sh     # Enable safe git pull + PM2 restart updates
├── disable_auto_update.sh    # Stop the PM2 auto-updater
├── setup.py                  # Interactive setup wizard
├── start-miner.sh            # Start miner (with inline wallet setup)
├── start-validator.sh        # Start validator (with inline wallet setup)
├── pm2-miner.sh              # Start / restart miner under PM2 (wraps start-miner.sh)
├── pm2-validator.sh          # Start / restart validator under PM2 (wraps start-validator.sh)
├── ecosystem.miner.config.js # PM2 app config (miner)
├── ecosystem.validator.config.js # PM2 app config (validator)
├── min_compute.yml           # Minimal compute requirements
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
| CPU/RAM (Miner) | ≥4 cores / 8–16 GB RAM | 8 GB for BCFtools, 16 GB for DeepVariant |
| Disk | ≥60 GB (miner) / ≥100 GB (validator) | Reference: ~2 GB miner, ~14 GB validator (SDF expands ~6×). Plus per-round BAMs (~6 GB each) until cleaned. |
| Docker | 20.10+ (24.0+ recommended) | Required for GATK, hap.py, bcftools |
| Python | 3.10+ | We test on 3.12 |
| Bittensor | Latest pip install | Provides wallet/subtensor/dendrite APIs |

---

## Quick Start

```bash
git clone https://github.com/minos-protocol/minos_subnet.git
cd minos_subnet
bash install.sh          # First-time: full setup (venv, deps, Docker, reference data, wallet)
bash start-miner.sh      # Start as miner (choose one)
# OR
bash start-validator.sh  # Start as validator (choose one)
```

The `start-*.sh` scripts handle wallet setup on first run — no manual `.env` editing needed. Run with `--help` to see all options, or `--setup` to re-run the setup wizard. If you already ran `install.sh` before, running it again will only update dependencies and download any new reference data (use `--fresh` to redo everything).

### Try mining without registering

Want to verify your variant-calling pipeline works before bonding TAO or registering a hotkey? Run the miner with **`--demo`**:

```bash
bash start-miner.sh --demo            # ephemeral keypair, no wallet needed
# or, with PM2 / systemd:
MINER_DEMO=true bash start-miner.sh   # equivalent — env var works the same as --flag
bash pm2-miner.sh --demo              # PM2 launcher with the flag
```

In demo mode the miner skips the chain entirely, generates an ephemeral keypair, downloads a static sandbox BAM from the platform's `/v2/demo/*` endpoints, runs your variant caller, and submits the result for acknowledgement. **Nothing is persisted, no score is computed, no TAO is earned** — it's purely a pipeline smoke test. When it prints `DEMO COMPLETE`, you know your Docker setup, reference data, variant caller, and platform connectivity all work end-to-end.

**Switching from demo to live**: the demo run creates a template `.env` with `MINER_DEMO=true` and placeholder `WALLET_NAME=default` / `WALLET_HOTKEY=default`. To switch to real mining: edit `.env` to set your real wallet name/hotkey, change `MINER_DEMO=true` → `MINER_DEMO=false` (or delete the line), and re-run `bash start-miner.sh`. Alternatively, run `bash start-miner.sh --setup` to redo the wallet wizard.

**MinosVM:** If using the MinosVM Docker image, everything is pre-installed. Just SSH in and run `bash start-miner.sh` or `bash start-validator.sh`.

### Running with PM2 (optional)

[PM2](https://pm2.keymetrics.io/) keeps the miner or validator running with restarts and log management. This repo runs the same **`start-miner.sh`** / **`start-validator.sh`** entrypoints under PM2 (so your venv, `.env`, and prerequisite checks stay identical to a manual start).

1. **Install PM2** — **`bash install.sh`** runs **`npm install -g pm2`** automatically when **`npm`** is on your `PATH`. If **`npm`** is missing, install Node.js and run **`npm install -g pm2`** once.
2. **Create `.env` first** — Run **`bash start-validator.sh`** or **`bash start-miner.sh`** once in a normal terminal so wallet/setup can run (PM2 does not provide a TTY for the interactive wizard).
3. **Launch under PM2:**

```bash
bash pm2-validator.sh   # validator: start, or restart if already registered
bash pm2-miner.sh       # miner: start, or restart if already registered
```

Or start from the ecosystem files directly:

```bash
pm2 start ecosystem.validator.config.js
pm2 start ecosystem.miner.config.js
```

Useful commands: **`pm2 status`**, **`pm2 logs minos-validator`** / **`pm2 logs minos-miner`**, **`pm2 save`** (persist process list after reboot — pair with **`pm2 startup`** per PM2 docs). The interactive setup wizard can also generate **`ecosystem.<role>.config.js`** when you choose the PM2 process-manager option.

### Automatic Updates with PM2

If your miner or validator already runs under PM2, you can enable safe automatic git pulls and PM2 restarts:

```bash
./enable_auto_update.sh
```

The setup script finds likely PM2 miner/validator processes, asks you to confirm the exact process, then starts a small PM2 updater named **`minos-auto-update`**.

Safety rules:

- Local git changes or untracked files make the updater skip the pull.
- Only fast-forward git updates are applied.
- Only the PM2 process you confirmed is restarted.
- The selected repo, branch, and PM2 process are saved in `~/.minos/auto_update.env`.

Useful commands:

```bash
pm2 logs minos-auto-update
./disable_auto_update.sh
```

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
docker pull genonet/hap-py@sha256:03acabe84bbfba35f5a7234129d524c563f5657e1f21150a2ea2797f8e6d05f2
docker pull quay.io/biocontainers/bcftools:1.20--h8b25389_0
docker pull quay.io/biocontainers/samtools:1.20--h50ea8bc_0
# Validators only — the freebayes image is retained until in-flight
# pre-cutover rounds finish scoring, then removed in a follow-up release:
# docker pull staphb/freebayes:1.3.7
```

> **Note:** The hap.py image is pinned by SHA256 digest for reproducible scoring. The tag `genonet/hap-py:0.3.15` points to the same image but the digest is what validators use internally.

#### 4. Run Setup Wizard

```bash
# Interactive wizard: configures wallet, downloads reference data, sets up .env
python setup.py
```

</details>

---

## Validator Setup

### Environment Variables

```bash
# Network
NETUID=107

# Wallet
WALLET_NAME=validator
WALLET_HOTKEY=default

# Platform
PLATFORM_URL=https://api.theminos.ai
PLATFORM_TIMEOUT=60

# Storage preference: hippius = Hippius SN75 first (recommended, Bittensor decentralized).
# aws_s3 = R2/AWS first. Both serve the same files; this just controls fetch order.
STORAGE_PRIMARY_BACKEND=hippius
```

### Running the Validator

```bash
bash start-validator.sh
bash start-validator.sh --wallet-name validator   # Pre-fill wallet name
bash start-validator.sh --setup                   # Re-run setup wizard
```

For production-style supervision, use **[Running with PM2 (optional)](#running-with-pm2-optional)** (`bash pm2-validator.sh`).

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

1. **Fetch Rounds and Assignment**: Poll platform for scoring rounds and this validator's primary/secondary miner assignment
2. **Download Round Data**: Download the mutated BAM, merged truth VCF, and mutations-only VCF from platform presigned URLs
3. **Run Miner Tools**: Execute assigned miners' variant-calling configs via templates, scoring primaries first and secondaries only while enough time remains
4. **Scoring**: Validate each generated VCF with hap.py against the platform truth data, then compute the AdvancedScorer result
5. **Submit Scores**: Report each per-miner score and audit artifact pointers back to the platform
6. **Backfill and Record Round**: After the scoring window closes, fetch peer scores for miners not personally covered and record complete participation
7. **Weight Update**: Submit weight history to the platform, then set on-chain weights if the validator is registered

### Validator Parallelism

Each round, the validator runs many miners' variant-calling configs concurrently. Concurrency, per-job thread count, and memory ceiling are auto-tuned at startup from host CPU/RAM:

- Reserves 4 cores + 16 GB for the OS, Docker daemon, and hap.py
- Picks `threads_per_job` between 2 and 8 (DeepVariant's call_variants is single-threaded past 8)
- Pins memory at 16 GB per job (DeepVariant's documented minimum)
- `concurrency = min(usable_cores // threads_per_job, usable_ram // 16, 8)`

Examples:

| Host | Auto-tune |
|---|---|
| 8c / 32 GB | 1 concurrent × 2 threads × 16 GB |
| 16c / 64 GB | 3 concurrent × 3 threads × 16 GB |
| 32c / 128 GB | 4 concurrent × 7 threads × 16 GB |
| 64c / 256 GB | 7 concurrent × 8 threads × 16 GB |

To override, set `MINOS_VALIDATOR_CONCURRENCY`, `SCORING_THREADS`, or `SCORING_MEMORY_GB` in `.env`. Note `SCORING_MEMORY_GB < 16` will OOM-crash DeepVariant submissions.

---

## Miner Setup

### Environment Variables

```bash
# Network
NETUID=107

# Wallet
WALLET_NAME=miner
WALLET_HOTKEY=default

# Miner
MINER_TEMPLATE=gatk

# Platform
PLATFORM_URL=https://api.theminos.ai
PLATFORM_TIMEOUT=60

# Storage preference: hippius = Hippius SN75 first (recommended, Bittensor decentralized).
# aws_s3 = R2/AWS first. Both serve the same files; this just controls fetch order.
STORAGE_PRIMARY_BACKEND=hippius
```

### Running the Miner

```bash
bash start-miner.sh
bash start-miner.sh --wallet-name miner --miner-template deepvariant  # Pre-fill values
bash start-miner.sh --setup                                           # Re-run setup wizard
```

For production-style supervision, use **[Running with PM2 (optional)](#running-with-pm2-optional)** (`bash pm2-miner.sh`).

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
4. **Variant Calling**: Run configured variant caller (GATK, DeepVariant, or bcftools — freebayes deprecated 2026-05-09)
5. **Config Submit**: Submit tool config you've used (hyperparameters only based on the template)
6. **Reward**: Earn alpha based on accuracy score — validators re-run the config to verify

---

## Platform Service

The Minos Platform is a hosted service at `https://api.theminos.ai` that handles BAM generation and synthetic mutation injection with HelixForge, file transfers via presigned URLs, and round coordination. Validators and miners connect automatically via the `PLATFORM_URL` environment variable — no self-hosting required.

### Key Endpoints

| Endpoint                         | Used by   | Description                                    |
| ---------------------------------|-----------|------------------------------------------------|
| `POST /v2/round-status`         | Miner     | Poll for active rounds and presigned BAM URL   |
| `POST /v2/submit-config`        | Miner     | Submit variant-calling tool config             |
| `POST /v2/get-scoring-rounds`   | Validator | Fetch rounds ready for scoring                 |
| `POST /v2/get-submissions`      | Validator | Fetch all miner configs for a round            |
| `POST /v2/get-assignment`       | Validator | Get primary/secondary miner scoring assignment |
| `POST /v2/submit-score`         | Validator | Submit per-miner scores                        |
| `POST /v2/get-backfill-scores`  | Validator | Fetch peer scores after scoring window closes  |
| `POST /v2/submit-weight-history`| Validator | Submit round scores, eligibility, and weights   |
| `POST /v2/get-validator-state`  | Validator | Recover round score state after validator restart |

### Storage Backends

The platform serves all per-round files (BAMs, truth VCFs, mutations VCFs) via short-lived presigned URLs and a primary + backup pair, so a miner or validator never needs storage credentials of its own. Three backends sit behind the platform:

| Backend          | Role                                                                          |
|------------------|-------------------------------------------------------------------------------|
| Cloudflare R2    | Primary for per-round artifacts when enabled platform-side (free egress)      |
| AWS S3           | Fallback for per-round artifacts (`genotypenet-platform` bucket)              |
| Hippius (SN75)   | Decentralized backup, Bittensor-native (`genotypenet-mutations` bucket)       |

Reference data (FASTA, FAI, dict, RTG SDF) is served via the indirected URL `https://api.theminos.ai/reference/...` which 302-redirects to the active backend (currently a public R2 bucket). Setup downloads through this URL only — no direct R2/S3 hostnames are baked into the subnet code.

The `STORAGE_PRIMARY_BACKEND` env var only controls **fetch order on the client side**:

- `hippius` (default) — try the Hippius URL first, fall back to the R2/AWS URL.
- `aws_s3` — try the R2/AWS URL first, fall back to Hippius. (The label says "aws_s3" for backwards compatibility, but the platform may serve either R2 or AWS in this slot — both behave identically from the client's perspective.)

Both URLs always point at the same files. Pick whichever is faster from your network.

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

### Round Score Tracking

The raw AdvancedScorer output (0-100) is normalized to a 0-1 scale and used as the miner's score for that round. Winners are selected from the current round's valid Advanced Score only; historical score carryover is not used.

```python
# AdvancedScorer returns 0-100, normalized to 0-1 for round scoring
combined_final = advanced_score / 100.0
current_score = combined_final
```

Only finite scores where `0 < current_score <= 1.0` are eligible for ranking. Rounds with no valid local or backfilled scores fail closed and do not reuse previous-round scores. The validator and platform also filter the synthetic all-zero input fingerprint around 0.25 so empty or invalid calls cannot become winners.

### Weight Distribution

Validators burn 87% of their weight, give the top eligible current-round miner 10%, and split the remaining 3% as ranked pruning dust across eligible ranks #2 through #10 using 0.8 geometric weighting. If fewer dust recipients exist, that 3% is renormalized across the available ranks; if no dust recipients exist, unused dust is sent to burn.

Eligibility requires scoring in at least 10 of the last 20 rounds, including the current round. Miners below the participation threshold receive 0 weight until they qualify. Close current-round ties use deterministic round data from the validator/platform flow; historical scores are not part of the ranking.

---

## Monitoring & Troubleshooting

### Common Issues

| Symptom                       | Cause                                      | Fix                                                                                                              |
|-------------------------------|--------------------------------------------|------------------------------------------------------------------------------------------------------------------|
| `docker: permission denied`   | User not in docker group                   | `sudo usermod -aG docker $USER && newgrp docker`                                                                 |
| `GATK timeout`                | Insufficient resources                     | Increase threads/memory or timeout                                                                               |
| DeepVariant OOM / killed      | <16 GB available to the container          | DeepVariant needs ≥16 GB. Free RAM, close other tools, or switch template to GATK/BCFtools                       |
| `Platform 401 error`          | Invalid sig or unregistered hotkey         | Ensure wallet hotkey is registered on the metagraph                                                              |
| `No miners available`         | No registered miners                       | Check metagraph for active miners                                                                                |
| `hap.py zero scores`          | VCF format issues                          | Ensure single-sample VCF output                                                                                  |
| Round skipped, "<10min left"  | Submitted too late                         | Miner needs ≥10 min remaining to start a round. Check clock skew (`timedatectl`) and platform connectivity speed |
| Reference download stuck/slow | Platform redirect or transient network     | Setup retries once automatically. If it still fails, re-run `bash install.sh --update-only`                      |
| Validator: "no scoring rounds"| Round still in submission window           | Validators only score AFTER the submission window closes. Wait for the next tempo boundary                       |
| Earning 0 weight as miner     | Not eligible yet, no valid current score, or outside the top current-round winners | Need >=10 of last 20 rounds participated, then a stronger current-round Advanced Score. See [tuning_guide](docs/tuning_guide.md) |

### Logs

```bash
# PM2 (recommended)
pm2 logs minos-miner
pm2 logs minos-validator

# systemd (if using systemd service)
journalctl -u minos-miner -f
journalctl -u minos-validator -f

# Direct (if running manually)
# Output streams to terminal
```

### Health Checks

```bash
# Platform health
curl https://api.theminos.ai/health

# Metagraph status
btcli subnet metagraph --netuid 107
```

---

## Additional Documentation

- [neurons/README.md](neurons/README.md) - Detailed miner/validator documentation
- [utils/README.md](utils/README.md) - Utility modules reference
- [docs/architecture.md](docs/architecture.md) - System architecture deep dive
- [docs/tuning_guide.md](docs/tuning_guide.md) - Miner tuning guide (scoring breakdown, parameters, strategy)
- [docs/hap_py_docker.md](docs/hap_py_docker.md) - hap.py Docker image reference
- [scripts/verify.sh](scripts/verify.sh) - Pre-flight environment check (`bash scripts/verify.sh --miner`)
- [scripts/demo.sh](scripts/demo.sh) - Run a single demo round end-to-end (`bash scripts/demo.sh`)

---

## Links

- **GitHub**: [github.com/minos-protocol/minos_subnet](https://github.com/minos-protocol/minos_subnet)
