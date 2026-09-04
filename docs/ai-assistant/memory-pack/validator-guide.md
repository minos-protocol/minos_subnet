# Minos SN107 Validator Guide

Memory name: Minos SN107 - Validator Guide
Version: 1.0.0
Primary subject: Validators
Subjects: Minos SN107; Validators; Validator setup; Validator safety; hap.py benchmarking; AdvancedScorer; Protocol rules
Related memories: Minos SN107 - Validator Basics; Minos SN107 - Validator Safety; Minos SN107 - Scoring And AdvancedScorer; Minos SN107 - Hardware And Runtime Guide

This is public-safe validator education for Minos subnet 107. It is not private validator operations documentation.

## What Validators Do

Validators score miners. A miner submits a variant-calling config or pipeline through the miner software. Validators rerun that config, generate calls, compare the calls against benchmark truth data inside validator-side scoring, and set weights according to public protocol rules.

Validators do not trust miner output blindly. They rerun miner configs and score the generated calls.

## Validator Startup Concept

The public repo includes a validator entrypoint:

```bash
bash start-validator.sh
```

For process supervision, validators may use:

```bash
bash pm2-validator.sh
```

Use the public README and repo scripts for exact current setup steps. Do not copy private validator files or private configs into Ditto.

## Validator Workload

Validator workloads are heavier than basic miner operation. Validators need enough CPU, RAM, disk, Docker capacity, reference data, and scoring resources to rerun miner configs and benchmark outputs.

Validators use hap.py-style benchmarking and Minos scoring logic at a high level. Do not request or disclose unreleased round truth data or other non-public validator materials. Files officially released through the public verification endpoint after a round closes may be used for independent verification.

## Public-Safe Validator Troubleshooting

Safe public checks:

- process running
- Docker available
- reference data present
- assignment or round polling status
- public validator health/heartbeat if exposed
- redacted errors that do not contain secrets or private paths

Never paste:

- wallet secrets or seed phrases
- `.env`
- private validator files
- truth VCFs or confident regions
- presigned URLs
- signatures, nonces, or authorization headers
- database/admin output
- private infrastructure details

## Live Status Rule

For current validator health, current round assignments, subnet health, recent scoring, or reward distribution, use Minos MCP or public endpoints. Do not answer live validator state from static @minos memory.
