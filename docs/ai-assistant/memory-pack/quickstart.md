# Minos SN107 Miner Quickstart

Memory name: Minos SN107 - Miner Quickstart
Version: 1.0.0
Primary subject: Miner onboarding
Subjects: Minos SN107; Miner onboarding; Demo mode; Practice mode; Live mining; Docker runtime; PM2 supervision; Public endpoints; Endpoint safety
Related memories: Minos SN107 - Protocol Rules And Rewards; Minos SN107 - Practice Mode; Minos SN107 - Troubleshooting Playbook; Minos SN107 - Public Endpoint Diagnostics; Minos SN107 - Safe Paste-Back Template

This is a public beginner guide for using Ditto while onboarding as a Minos subnet 107 miner.

## What Minos Mining Is

Minos is a Bittensor subnet for genomic variant calling. Miners run tools such as GATK, DeepVariant, or BCFtools on benchmark genomic data. Validators re-run submitted tool configs and score the resulting calls against benchmark truth data.

Miners submit variant-calling configurations, not VCF files.

## What Ditto Can Help With

Ditto can help explain:

- setup, demo mode, and practice mode
- Docker and PM2 basics
- BAM, VCF, reads, variants, SNPs, and indels
- why a miner has 0 weight
- eligibility, weight, rewards, and pruning dust at a high level
- public validator basics
- public endpoint checks
- safe troubleshooting
- basic config-tuning tradeoffs

## What Ditto Cannot Help With

Ditto should not handle secrets or private infrastructure data. Do not paste private keys, seed phrases, `.env` files, API keys, SSH keys, presigned URLs, truth files, private validator data, or admin/database details.

## Recommended Beginner Path

1. Set up the miner.
2. Run demo mode first.
3. Confirm files download.
4. Confirm Docker and the selected variant caller run.
5. Confirm a result/config is produced.
6. Score a config in practice mode.
7. Move to live mining.
8. Wait for scored rounds.
9. Check eligibility and weight.
10. Only then tune configs.

## Common Commands

Use the official installer first, then start in demo mode if you are new:

```bash
bash install.sh
bash start-miner.sh --demo
```

To score a config against any available sample instead of the fixed demo one,
use practice mode:

```bash
bash start-miner.sh --practice
bash start-miner.sh --practice --config configs/gatk.conf
```

When you are ready for live mining, configure/register your wallet first, then
start the miner:

```bash
bash start-miner.sh --setup
bash start-miner.sh
bash pm2-miner.sh
```

Useful checks:

```bash
pm2 status
pm2 logs minos-miner --lines 50
curl https://api.theminos.ai/health
```

## Demo Mode First

Demo mode is a pipeline smoke test. It is the self-scorer pinned to one fixed chr20 sample, so the first run is repeatable. Demo mode does not earn TAO. Demo mode does not persist live submissions. Demo mode does not produce real live scores. It is useful because it proves your local environment can run the workflow before you risk live rounds.

## Practice Mode Before Live Rounds

Practice mode (`--practice`) is the same self-scorer over a menu of samples. It picks a fully-answered chr18/chr19/chr20/chr21/chr22 sample, downloads its BAM and truth files, runs the selected caller, and prints the exact score a validator would compute. It scores with the version the platform advertises in `/scoring/network-config` and names that version in its output; unreachable, it uses the version the machine last resolved, or v1 if it never resolved one.

Practice mode needs no wallet and touches no chain. It does not submit, does not earn TAO, and does not count toward eligibility. Use it to compare configs before a live round pays for the experiment.

Like demo mode, practice mode is a one-shot foreground run. Do not run it under PM2.

## PM2 Online Is Not Enough

PM2 online means the process is running. It does not prove that the miner is participating, submitting, scored, eligible, weighted, or earning emissions.

## Safe Public Checks

- Platform health: `GET https://api.theminos.ai/health`
- Current public scores: `GET https://api.theminos.ai/scoring/all`
- One miner detail: `GET https://api.theminos.ai/scoring/detailed/{hotkey}`
- Accepted params + valid ranges: `GET https://api.theminos.ai/scoring/parameter-ranges`
- Current leaderboard: `GET https://api.theminos.ai/scoring/rounds/current/leaderboard`
- Latest finalized leaderboard: `GET https://api.theminos.ai/scoring/rounds/latest-finalized/leaderboard`

## How To Ask Ditto For Help

Good prompt:

```text
I am a Minos miner in live mode. PM2 says online but I have 0 weight. My public UID is 38. /scoring/all shows score X, eligible false, weight 0. Here are the last 40 redacted PM2 log lines.
```

Bad prompt:

```text
Here is my .env and presigned URL. Tell me why mining failed.
```
