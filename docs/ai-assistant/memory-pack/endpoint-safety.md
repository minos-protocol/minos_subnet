# Minos SN107 Public Endpoint Safety Reference

Memory name: Minos SN107 - Public Endpoint Safety Reference
Version: 1.0.0
Primary subject: Endpoint safety
Subjects: Minos SN107; Endpoint safety; Public endpoints; Live data boundary; Safe paste-back; Safe support behavior
Related memories: Minos SN107 - Public Endpoint Diagnostics; Minos SN107 - Live Data Boundary; Minos SN107 - Safe Paste-Back Template; Minos SN107 - Public Assistant Instruction

This public reference tells Ditto and miners which Minos endpoints are safe for beginner diagnostics.

## Base URL

```text
https://api.theminos.ai
```

## Safe Public GET Endpoints

- `GET /health`
- `GET /v2/info`
- `GET /reference/{path}`
- `GET /scoring/all`
- `GET /scoring/detailed/{hotkey}`
- `GET /scoring/metagraph/miners`
- `GET /scoring/weights`
- `GET /scoring/parameter-ranges`
- `GET /scoring/rounds`
- `GET /scoring/rounds/current/leaderboard`
- `GET /scoring/rounds/latest-finalized/leaderboard`
- `GET /dashboard/network-stats`
- `GET /dashboard/miner-history/{hotkey}`
- `GET /dashboard/miner-metrics/{hotkey}`
- `GET /dashboard/recent-activity`
- `GET /dashboard/validators`

## Miner-Software-Managed Signed POST Endpoints

- `POST /v2/round-status`
- `POST /v2/submit-config`
- `POST /v2/demo/round-status`
- `POST /v2/demo/submit-result`

Beginners should not manually call signed POST endpoints. The miner software handles request signing and submission.

## Safe Use Cases

- check whether the platform is healthy
- find public UID/hotkey/metagraph status
- check score, eligibility, and weight
- inspect current or latest finalized leaderboard
- inspect public miner score history
- inspect public network stats
- check which tool-config parameters and value ranges the platform accepts

## Not For Beginner Support

Do not suggest admin endpoints, database access, production environment details, private validator files, unreleased truth files, presigned URL inspection, or manual signed request construction.
