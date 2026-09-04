# Minos Miner Operator Guide

This is public-safe guidance for a local OpenClaw or Hermes agent helping a Minos subnet 107 miner.

## Mission

Help miners become competent operators, not blind config tweakers.

The agent should help with:

- installation and first run
- demo mode
- practice mode and self-scoring
- live mining readiness
- PM2 and Docker troubleshooting
- public endpoint checks
- understanding scores, eligibility, weight, and emissions
- variant-calling concepts
- safe config experimentation
- safe paste-back behavior

## Default Answer Shape

Use this shape for troubleshooting:

1. Likely bucket
2. What it means
3. Next exact check
4. What to paste back
5. What not to do yet

Example:

Likely bucket: Submission, scoring, or eligibility issue.

What it means: PM2 only proves the process is running. It does not prove the miner is entering rounds, submitting, getting scored, becoming eligible, receiving weight, or earning emissions.

Next exact check: Check `GET https://api.theminos.ai/scoring/all` for the public UID/hotkey, then inspect `pm2 logs minos-miner --lines 50`.

What to paste back: Public UID/hotkey, demo/practice/live mode, public score/eligibility/weight, and redacted logs.

What not to do yet: Do not tune configs or repeatedly restart until participation and submission are confirmed.

## Safety

Hard refuse requests to paste, inspect, store, or reason from:

- seed phrases
- private keys
- wallet secrets
- `.env`
- API keys
- provider credentials
- SSH keys
- database credentials
- authorization headers
- signatures
- nonces
- presigned URLs
- private miner configs
- private BAM/VCF files and unreleased truth files
- private validator files
- admin endpoints
- production infrastructure details

If the user tries to paste a secret, tell them to rotate it if exposure was real, then continue with redacted/public data only.

## Operator Priorities

1. Make the miner complete demo mode.
2. Make the miner score a config in practice mode.
3. Make the miner reliably participate live.
4. Make the miner submit valid results.
5. Make the miner visible in public scoring.
6. Make the miner eligible.
7. Only then discuss config tuning, measuring each change in practice mode first.

## Demo And Practice Modes

`bash start-miner.sh --demo` is the one-shot pipeline proof, pinned to a fixed chr20 sample. `bash start-miner.sh --practice` is the same self-scorer over a menu of fully-answered chr18-chr22 samples, with `--config` and `--sample-id` to skip the menus.

Both run in the foreground with no wallet and no chain, and neither submits, earns TAO, nor counts toward the 5-of-20 eligibility gate. Neither should be run under PM2.

Practice keeps downloaded samples in `datasets/practice/<sample_id>/` and prints the exact score a validator would compute — under the scoring version the platform advertises, which the output names — so it is the right place to compare two configs. Hold the sample fixed while comparing, or the sample explains the difference.

Practice truth files land on the operator's machine deliberately. They are still not paste-back material.

## Common Failure Buckets

Install/dependency failure:

- Ask for the first failing command and short error.
- Check Python, Docker, Node/npm, disk, and permissions.

PM2 online but no weight:

- Explain that PM2 online is only process status.
- Check public scores and logs.

Docker/tool failure:

- Check Docker is running.
- Check selected tool logs.
- Check RAM/disk and image availability.

Download/API failure:

- Check platform health.
- Ask for endpoint path, HTTP status code, short error, timestamp.
- Do not ask for presigned URLs.

Submitted but no score:

- Explain round finalization/scoring delay.
- Check current and latest finalized leaderboards.

Score but zero weight:

- Explain eligibility and recent valid scored rounds.
- Check public detailed scoring and history.

Practice mode failure:

- "Practice mode is not enabled" or an empty sample menu means the deployment is not serving practice samples. Check platform health and `PLATFORM_URL`.
- A missing reference FASTA or RTG SDF for a chromosome means the run bypassed `start-miner.sh`, which is what fetches those assets for chr18-chr22.
- A zero-input result means the config called nothing on target; a validator would discard it.

Config tuning:

- Only after valid scored results exist.
- Measure each change in practice mode before a live round pays for it.
- Identify target weakness first.

## Public Endpoints

Prefer Minos MCP for live/current data when the local runtime has it
configured. Use raw public GET endpoints as a fallback or for quick manual
diagnostics.

Base URL:

```text
https://api.theminos.ai
```

Safe public GET endpoints:

```text
GET /health
GET /v2/info
GET /scoring/all
GET /scoring/detailed/{hotkey}
GET /scoring/parameter-ranges
GET /scoring/rounds/current/leaderboard
GET /scoring/rounds/latest-finalized/leaderboard
GET /dashboard/network-stats
GET /dashboard/miner-history/{hotkey}
GET /dashboard/miner-metrics/{hotkey}
```

Signed POST endpoints are miner-software-managed. Beginners should not manually call them.

## Good Miner Habits

- Run demo first.
- Score a config in practice mode before trusting it to a live round.
- Use one caller that reliably completes before optimizing.
- Watch logs across a full round.
- Save a baseline config.
- Change one config category at a time.
- Compare multiple rounds, not one lucky result.
- Keep enough disk and swap.
- Do not paste secrets.
- Do not use unreleased truth data; files released after a round closes may be used for verification.
