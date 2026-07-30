# Minos SN107 Troubleshooting Playbook

Memory name: Minos SN107 - Troubleshooting Playbook
Version: 1.0.0
Primary subject: Troubleshooting
Subjects: Minos SN107; Troubleshooting; Miner onboarding; Docker runtime; PM2 supervision; Public endpoints; Eligibility and weight; Safe paste-back
Related memories: Minos SN107 - Miner Lifecycle; Minos SN107 - Public Endpoint Diagnostics; Minos SN107 - PM2 And Docker Runtime; Minos SN107 - Safe Paste-Back Template

This public playbook helps Ditto support Minos miners without needing secrets or private infrastructure data.

## Principle

Do not jump to config tuning. First identify which lifecycle step is failing.

## Miner Lifecycle

1. Process starts.
2. Miner discovers an open round.
3. Miner downloads round files.
4. Tool runs in Docker.
5. Miner produces a result/config.
6. Miner submits through the miner software.
7. Validators score after the round moves to scoring.
8. Miner becomes eligible after enough valid recent scored participation.
9. Weight and emissions depend on eligibility and rank.

## First Question

Ask whether the miner is in demo mode or live mode.

## Support Format

- Likely bucket
- What it means
- Next exact check
- What to paste back
- What not to do yet

## Common Buckets

### PM2 Online But Not Participating

PM2 online only means the process is running.

Next checks:

- `pm2 logs minos-miner`
- `GET https://api.theminos.ai/scoring/all`
- `GET https://api.theminos.ai/scoring/detailed/{hotkey}`

Ask for public UID/hotkey, demo/live mode, and redacted logs.

### Docker Or Tool Execution Problem

The miner may be running, but the selected caller is failing.

Next checks:

- confirm Docker is running
- inspect logs for GATK, DeepVariant, or BCFtools errors
- check machine RAM and disk

Do not tune before the tool finishes successfully.

### Download Or API Problem

If files do not download, check platform health and logs.

Next check:

- `GET https://api.theminos.ai/health`

Ask for endpoint path, HTTP status code, short error message, and timestamp. Do not ask for presigned URLs.

### Submitted But No Score Yet

The round may not have finalized or validators may still be scoring.

Next checks:

- current round leaderboard
- latest finalized leaderboard
- miner detail endpoint

### Score Exists But Weight Is 0

The miner may not be eligible yet, may lack enough recent valid scored rounds, or may rank outside the rewarded set. Eligibility requires 5 valid scored rounds out of the last 20 rounds, including the current round.

Next checks:

- `/scoring/all` eligibility and weight
- `/scoring/detailed/{hotkey}` recent score history

## HTTP Status Guide

- `400`: malformed request, invalid config, stale timestamp, or bad request body
- `401`: auth, hotkey, registration, signature, or clock freshness issue
- `404`: wrong path, wrong round ID, or missing miner/round data
- `429`: rate limit
- `500` or `503`: platform/backend issue or transient unavailability

## Safe Paste-Back

Safe:

- public UID or hotkey
- demo/live mode
- endpoint path
- HTTP status code
- short error text
- redacted PM2 logs
- public score/eligibility/weight values

Unsafe:

- seed phrase
- private key
- `.env`
- API key
- SSH key
- authorization header
- signature
- active nonce
- presigned URL
- truth file
- admin endpoint output
- database credentials
