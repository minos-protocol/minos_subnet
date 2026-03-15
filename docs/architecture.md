# Minos Genomics Subnet – Architecture

Minos is a Bittensor subnet (SN107) that creates a decentralised market for genomic variant calling. Miners run variant-calling pipelines and are incentivized to maximize accuracy; validators score results trustlessly and set on-chain weights.

---

## 1. Problem Statement

Genomic variant calling accuracy is critical for real-world genomics, yet benchmarking is fragmented and untrustworthy. Minos turns this into a continuous, incentivized benchmarking network:

- **Miners** run any variant-calling pipeline they choose (GATK, DeepVariant, FreeBayes, BCFtools, or custom) and earn rewards proportional to their accuracy.
- **Validators** re-run miner configurations against private hold-out data and score results with hap.py — no trust required.
- **The platform** generates synthetic benchmark BAMs (GIAB + HelixForge-inserted mutations) and coordinates rounds.

---

## 2. Round-Based Task Flow

Each scoring round follows this lifecycle:

```text
PLATFORM
  │  Creates round with: round_id, region (e.g. chr20:10M-15M),
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
| Reference | GRCh38 chr20 FASTA + index + RTG SDF | Public (downloaded by all) |
| Benchmark BAM | GIAB HG002 300× chr20, downsampled | Public (via platform presigned URL) |
| Truth VCF | GIAB + HelixForge-inserted synthetic mutations | Validators only (presigned URL, round-scoped) |
| Confident BED | GIAB high-confidence regions for chr20 | Public |

Synthetic mutations are injected by the platform using HelixForge into known positions, creating a merged truth (`GIAB ∪ synthetic variants`) that miners cannot pre-compute answers for.

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
| `bcftools` | `quay.io/biocontainers/bcftools:1.20` |

Miners tune quality parameters via `configs/<tool>.conf`. Infrastructure parameters (`threads`, `memory_gb`, `timeout`, `ref_build`, `num_threads`) are stripped before submission and cannot influence scoring.

---

## 5. Validator Loop

```python
while True:
    rounds = get_scoring_rounds()          # poll platform for rounds in scoring phase
    for round in rounds:
        submissions = get_submissions(round)   # miner configs + presigned BAM/truth URLs
        download_bam_and_truth(round)          # cached; skipped if SHA256 matches
        for miner in submissions:
            vcf = run_tool(miner.tool_config)  # reproduce miner's exact output
            metrics = score_with_happy(vcf, truth_vcf)
            score = compute_advanced_score(metrics)
            update_ema(miner.hotkey, score)
        set_weights_on_chain()             # winner-takes-all by EMA
    sleep(query_interval)
```

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
- **Warmup phase** (before any miner reaches 10 rounds): reward split 50/30/20 among the top 3 active miners by EMA score; tiebreak by earliest config submission time
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
│   └── validator.py       # Validator loop: score submissions, set chain weights
├── templates/
│   ├── gatk.py            # GATK HaplotypeCaller template
│   ├── deepvariant.py     # Google DeepVariant template
│   ├── freebayes.py       # FreeBayes template
│   ├── bcftools.py        # BCFtools mpileup/call template
│   └── tool_params.py     # Parameter definitions and validation
├── utils/
│   ├── scoring.py         # hap.py Docker runner + AdvancedScorer
│   ├── weight_tracking.py # EMA score tracker + winner-takes-all weights
│   ├── platform_client.py # Authenticated API client (miner + validator)
│   └── file_utils.py      # SHA256-verified file download + caching
├── base/
│   ├── genomics_config.py # Central config (Docker images, timeouts, EMA params)
│   └── s3_manifest.json   # Reference data paths (local + S3)
├── configs/
│   ├── gatk.conf          # Miner-tunable GATK quality parameters
│   ├── deepvariant.conf   # Miner-tunable DeepVariant parameters
│   ├── freebayes.conf     # Miner-tunable FreeBayes parameters
│   └── bcftools.conf      # Miner-tunable BCFtools parameters
└── setup.py               # Interactive setup wizard
```
