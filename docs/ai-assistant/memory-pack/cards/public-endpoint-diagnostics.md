# Minos SN107: Public Endpoint Diagnostics

Memory name: Minos SN107 - Public Endpoint Diagnostics
Version: 1.0.0
Primary subject: Public endpoints
Subjects: Public endpoints; Endpoint safety; Live data boundary; Miner monitoring; Troubleshooting; Rewards and emissions
Related memories: Minos SN107 - Public Endpoint Safety Reference; Minos SN107 - Live Data Boundary; Minos SN107 - Troubleshooting Playbook; Minos SN107 - Safe Paste-Back Template

Minos support can use public read-only endpoints for beginner diagnostics.

Safe public GET endpoint categories:

- platform health
- subnet info
- public scoring summaries
- public miner history and metrics by hotkey
- public current and finalized leaderboards
- public network stats
- public validator dashboard data when exposed
- accepted tool-config parameters and their valid ranges

Signed POST endpoints are managed by the official miner software. Beginners should not manually construct signed requests, signatures, active nonces, authorization headers, or submission payloads.

Use public endpoints to answer questions like:

- "Is the platform healthy?"
- "Is this hotkey scoring?"
- "Is my miner eligible?"
- "Why is PM2 online but weight is 0?"
- "Who is on the latest finalized leaderboard?"
- "What parameter values are accepted, and what happens if I submit one out of range?"

Use Minos MCP when available for live/current data. Use the `@minos` graph for stable interpretation and safety guidance.
