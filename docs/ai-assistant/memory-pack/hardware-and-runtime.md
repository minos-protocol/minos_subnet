# Minos SN107 Hardware And Runtime Guide

Memory name: Minos SN107 - Hardware And Runtime Guide
Version: 1.0.0
Primary subject: Runtime operations
Subjects: Minos SN107; Hardware requirements; Runtime operations; Docker runtime; PM2 supervision; Miner onboarding; Validators
Related memories: Minos SN107 - PM2 And Docker Runtime; Minos SN107 - Demo Mode; Minos SN107 - Validator Guide; Minos SN107 - Troubleshooting Playbook

This is public-safe runtime guidance for Minos miners and validators.

## Required Runtime Pieces

Docker is required because Minos runs genomics tools in containers. Python environment setup and reference data must also be healthy before mining or validating can work.

PM2 is helpful for keeping a miner or validator process running, but PM2 online only proves the process is alive. It does not prove rounds are detected, files are downloaded, Docker jobs complete, submissions happen, scoring appears, eligibility is met, weight is assigned, or emissions are earned.

## Miner Hardware Guidance

CPU, RAM, disk, swap, Docker health, and outbound HTTPS all matter. DeepVariant is heavier and commonly needs more RAM than BCFtools. GATK is tunable and can be slower. BCFtools is lightweight and fast, but speed alone does not guarantee score.

If a tool fails, check logs, memory, disk, Docker image availability, and timeout symptoms before tuning config parameters.

Practice mode needs extra disk. Its first run fetches the reference FASTA and RTG SDF for chr18 through chr22, and each downloaded sample is kept under `datasets/practice/<sample_id>/` so later runs reuse it.

## Validator Hardware Guidance

Validators are heavier than basic miners because they rerun miner configs and score outputs. Validator machines need more CPU, RAM, disk, Docker capacity, reference/scoring data, and operational reliability.

Do not paste validator private files or unreleased truth data into support. Share only public-safe status and redacted errors.

## Tool Tradeoffs

- GATK: tunable, powerful, more knobs, can be slower.
- DeepVariant: strong model-driven caller, heavier, fewer knobs.
- BCFtools: fast and lightweight, useful for smaller machines, still needs careful filtering.
- hap.py: validator-side benchmarking concept for comparing generated calls against truth data.

## Safe Checks

Useful public-safe commands:

```bash
docker info
df -h
free -h
pm2 status
pm2 logs minos-miner --lines 50
pm2 logs minos-validator --lines 50
```

Redact secrets, private paths, headers, signatures, nonces, presigned URLs, and private files before sharing output.
