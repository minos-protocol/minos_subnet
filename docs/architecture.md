# Minos Genomics Subnet – Architecture

Minos is a Bittensor subnet (SN107) that creates a decentralised market for genomic variant calling. Miners run variant-calling pipelines and are incentivized to maximize accuracy; validators score results trustlessly and set on-chain weights.

---

## 1. Problem Statement

Genomic variant calling accuracy is critical for real-world genomics, yet benchmarking is fragmented and untrustworthy. Minos turns this into a continuous, incentivized benchmarking network:

- **Miners** run variant-calling pipelines (GATK, DeepVariant, FreeBayes, or BCFtools) and earn rewards proportional to their accuracy.
- **Validators** re-run miner configurations against private hold-out data and score results with hap.py — no trust required.
- **The platform** generates synthetic benchmark BAMs (GIAB + HelixForge-inserted mutations) and coordinates rounds.

---

## 2. Round-Based Task Flow

Each scoring round follows this lifecycle:

```text
PLATFORM
  │  Creates round with: round_id, region (e.g. chr20:10M-15M, chr16:5M-10M),
  │  mutated BAM (presigned S3 URL), truth VCF (private)
  │
  ▼ status: "pending"  (round created, waiting for start time)
  │
  ▼ status: "open"
MINERS
  │  Poll /v2/round-status → download BAM → run variant calling
  │  Submit: tool_name + tool_config (quality parameters only, no VCF)
  │
  ▼ status: "scoring"  (submission window closes)
VALIDATORS
  │  Poll /v2/get-scoring-rounds → get all miner submissions
  │  For each miner: re-run their exact tool_config → score VCF with hap.py
  │  Submit scores to platform → compute EMA → set weights on chain
  │
  ▼ status: "completed"
```

Key design choice: **miners submit configs, not VCFs**. Validators independently reproduce each miner's output. This makes scoring trustless — a miner cannot submit a fabricated VCF.

---

## 3. Data Assets

| Pool | Contents | Visibility |
| --- | --- | --- |
| Reference | GRCh38 FASTA + index + RTG SDF (chr1-chr22) | Public (downloaded by all) |
| Benchmark BAM | GIAB donors (HG001-HG007) 100-300× per chromosome, downsampled | Public (via platform presigned URL) |
| Truth VCF | GIAB + HelixForge-inserted synthetic mutations | Validators only (presigned URL, round-scoped) |
| Mutations-only VCF | Synthetic mutations only (no GIAB variants) | Validators only (primary scoring scope) |
| Confident BED | GIAB high-confidence regions per chromosome | Validators only (legacy GIAB-scoring support) |

Synthetic mutations are injected by the platform using HelixForge into known positions. Validators require the **mutations-only VCF** (synthetic variants only) for current production scoring; if the platform does not provide it, the validator skips the round instead of falling back to GIAB/BED-only scoring. The confident BED path remains in the code for legacy GIAB-only scoring support.

---

## 4. Miner Interface

Miners register on Bittensor and poll the platform for active rounds:

```json
POST /v2/round-status  →  {
  "round_id", "status", "region",
  "bam_presigned_url", "bam_index_presigned_url",
  "time_remaining_seconds", "num_mutations"
}
```

On an active round, the miner:

1. Downloads the BAM (SHA256-verified, cached across rounds)
2. Runs its configured variant caller via Docker on the given region
3. Submits its tool config (quality parameters only — no VCF uploaded):

```json
POST /v2/submit-config  →  {
  "hotkey", "round_id", "tool_name", "tool_config",
  "variant_count", "runtime_seconds", "timestamp", "signature"
}
```

All API calls are authenticated via canonical request signing. Each request includes a `signature` (the Bittensor keypair signs `METHOD|PATH|BODY_HASH|TIMESTAMP|NONCE`) and a unique `nonce` to prevent replay attacks.

### Supported tools

| Template | Docker image |
| --- | --- |
| `gatk` | `broadinstitute/gatk:4.5.0.0` |
| `deepvariant` | `google/deepvariant:1.5.0` |
| `freebayes` | `staphb/freebayes:1.3.7` |
| `bcftools` | `quay.io/biocontainers/bcftools:1.20--h8b25389_0` |

Miners tune quality parameters via `configs/<tool>.conf`. Infrastructure parameters (`threads`, `memory_gb`, `timeout`, `ref_build`, `num_threads`) are stripped before submission and cannot influence scoring.

---

## 5. Validator Loop

Validators use **subset-based scoring** to scale across many miners. Instead of every validator scoring every miner (O(V×M)), the platform assigns each validator a primary miner range based on stake rank. Adjacent validators share a 20% overlap zone for integrity cross-checks.

```python
while True:
    rounds = get_scoring_rounds()              # poll platform for rounds in scoring phase
    for round in rounds:
        assignment = get_assignment(round)      # primary + secondary miner lists
        submissions = get_submissions(round)    # miner configs + presigned BAM/truth URLs
        download_round_files(round)             # BAM, truth VCF, mutations VCF; SHA256-verified

        # Per-job thread/memory and total concurrency are auto-tuned from
        # host CPU/RAM (see auto_scoring_config). Primaries run as a barrier;
        # secondaries are skipped if they'd start within 3 min of the deadline.
        with bounded_concurrency(N=auto):
            for miner in score_in_parallel(assignment.primary):
                submit_score(miner)             # per-miner score + artifact pointers
                update_ema(miner.hotkey, miner.combined_final)
            if not approaching_deadline():
                for miner in score_in_parallel(assignment.secondary):
                    submit_score(miner)
                    update_ema(miner.hotkey, miner.combined_final)

        # After scoring window closes: fetch peer scores for gap miners,
        # then record participation once using personal + backfilled hotkeys.
        backfill = get_backfill_scores(round)   # commit-then-reveal
        for entry in backfill:
            update_ema(entry.hotkey, entry.score)
        record_round(personal_hotkeys + backfill_hotkeys)

        weights = compute_weights()             # warmup split or winner-takes-all
        submit_weight_history(weights)          # platform dashboard/audit
        if registered_on_subnet:
            set_weights_on_chain(weights)       # Bittensor chain write
    sleep(query_interval)
```

Each `score_in_parallel(miners)` starts each miner's tool in its own Docker container under the auto-tuned semaphore, then scores the resulting VCF with hap.py and updates/submits that miner's score. The platform weight-history submission happens for registered and unregistered validators; on-chain `set_weights` only runs when the validator hotkey is registered on the subnet.

If the platform does not support assignments (e.g. single-validator testnet), the validator scores all miners concurrently in a single batch.

### 5.1 Scoring formula

hap.py computes SNP and INDEL precision/recall against the truth VCF. The `AdvancedScorer` combines these into a final score (0–100) with four components:

| Component | Weight | What it measures |
| --- | --- | --- |
| **Core F1** | 60% | Truth-weighted F1 across SNPs and INDELs, with nonlinear emphasis (γ=0.5) rewarding near-perfect callers |
| **Completeness** | 15% | Average recall + coverage (1 − fraction unassessed) |
| **FP Rate** | 15% | Penalises excess false positives and call counts diverging from truth |
| **Quality** | 10% | Ti/Tv and Het/Hom ratio match against truth — rewards biologically consistent calls |

SNP/INDEL weighting is truth-count-proportional (fallback: 70/30).

### 5.2 Weight assignment (EMA + winner-takes-all)

- Each scored round updates each miner's EMA: `ema = α × score + (1−α) × ema` (α = 0.1); EMA starts at 0 so the first round yields 10% of the first score
- Miners must participate in ≥ 10 of the last 20 rounds to be eligible for weights
- Missed rounds decay the EMA by 0.95× per missed round
- **Warmup phase** (before any miner reaches 10 rounds): reward split 50/30/20 among the top 3 active miners with positive EMA; the split is renormalized if fewer than three qualify; tiebreak by earliest config submission time
- **Normal phase** (once any miner reaches eligibility): the eligible miner with the highest EMA receives **100% of the weight** on-chain; tiebreak by earliest config submission time
- Miners below the participation threshold receive 0 weight

---

## 6. Anti-Cheating

| Mechanism | How it works |
| --- | --- |
| **Config re-execution** | Validators run the miner's tool independently — a fabricated VCF cannot be submitted |
| **Synthetic mutations** | HelixForge inserts mutations at positions unknown to miners; GIAB alone is insufficient to score well |
| **Keypair authentication** | Every API call is signed with the Bittensor wallet keypair — submissions cannot be spoofed |
| **Infrastructure stripping** | `threads`, `memory_gb`, `timeout`, `ref_build`, `num_threads` are removed from submitted configs — only quality parameters count |
| **Winner-takes-all** | Only one miner earns rewards per round — copying another miner's config yields zero differentiation |

---

## 7. Repository Layout

```text
minos_subnet/
├── neurons/
│   ├── miner.py           # Miner loop: poll, download, call variants, submit config
│   ├── validator.py       # Validator loop: subset scoring, set chain weights
│   ├── status.py          # Health checks and system status
│   └── README.md          # Neurons documentation
├── templates/
│   ├── gatk.py            # GATK HaplotypeCaller template
│   ├── deepvariant.py     # Google DeepVariant template
│   ├── freebayes.py       # FreeBayes template
│   ├── bcftools.py        # BCFtools mpileup/call template
│   ├── _common.py         # Shared template utilities
│   └── tool_params.py     # Parameter definitions and validation
├── utils/
│   ├── scoring.py         # hap.py Docker runner + AdvancedScorer
│   ├── weight_tracking.py # EMA score tracker + winner-takes-all weights
│   ├── platform_client.py # Authenticated API client (miner + validator)
│   ├── subset_scoring.py  # Subset scoring helpers (assignments, deadlines)
│   ├── config_loader.py   # Tool config file parser
│   ├── path_utils.py      # Safe filesystem paths
│   ├── file_utils.py      # SHA256-verified file download + caching
│   └── README.md          # Utils documentation
├── base/
│   └── genomics_config.py # Central config (Docker images, timeouts, EMA params)
├── configs/
│   ├── gatk.conf          # Miner-tunable GATK quality parameters
│   ├── deepvariant.conf   # Miner-tunable DeepVariant parameters
│   ├── freebayes.conf     # Miner-tunable FreeBayes parameters
│   └── bcftools.conf      # Miner-tunable BCFtools parameters
├── docs/
│   ├── architecture.md    # This document
│   ├── tuning_guide.md    # Miner tuning reference (scoring, parameters, strategy)
│   └── hap_py_docker.md   # hap.py Docker image reference
├── scripts/
│   ├── verify.sh          # Pre-flight environment check
│   └── demo.sh            # End-to-end demo runner
├── tests/                 # Unit and integration tests
│   ├── conftest.py
│   └── test_*.py          # Tests for scoring, config, platform client, etc.
├── install.sh             # Installer (full setup or update mode)
├── setup.py               # Interactive setup wizard
├── start-miner.sh         # Start miner (with inline wallet setup)
├── start-validator.sh     # Start validator (with inline wallet setup)
├── pm2-miner.sh           # Start / restart miner under PM2
├── pm2-validator.sh       # Start / restart validator under PM2
├── ecosystem.miner.config.js     # PM2 config (miner)
├── ecosystem.validator.config.js # PM2 config (validator)
├── min_compute.yml        # Minimal compute requirements
├── requirements.txt       # Python dependencies
├── .env.miner.example     # Miner environment configuration
└── .env.validator.example # Validator environment configuration
```
