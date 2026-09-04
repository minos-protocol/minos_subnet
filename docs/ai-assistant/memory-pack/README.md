# Minos SN107 Public Knowledge Pack

This directory contains the canonical public knowledge source for the **Minos
SN107 Public Knowledge Graph** and Minos-aware assistant runtimes.

The content is Minos-owned. It can be seeded into the public Ditto `@minos`
knowledge graph, paired with live Minos MCP data from `https://mcp.theminos.ai`,
bundled into OpenClaw/Hermes skills, or reused by other agent runtimes.

## Current Phase

1.0.0 is a public miner and validator knowledge pack. It is designed to be
seeded into the public Ditto `@minos` graph as focused memories instead of one
large document, so search and fetch return the right topic cleanly.

Use this memory pack with the Ditto graph subscription script:

```bash
bash scripts/setup_ditto_agent.sh
# or
bash start-miner.sh --setup-ditto
```

The Ditto setup script subscribes the current Ditto account or agent
to the public `@minos` graph. The expected launch source is
`subscribed_graph:minos-public-knowledge-graph-1.0.0`. If graph subscription or
search cannot be verified, the script can seed the public files listed in
`graph_seed_order` into that account as a private fallback copy. The preferred
source remains the shared public `@minos` graph. If no Ditto CLI auth exists,
setup can create an agent account, print the private claim URL at the end of
setup, and save the same URL locally with private file permissions. The user
should open that claim URL themselves and avoid pasting it into public channels,
screenshots, or logs before claiming it.

For miners who also want a local agent runtime, use:

```bash
bash scripts/setup_ai_assistant.sh
# or
bash start-miner.sh --setup-ai-assistant
```

That flow subscribes Ditto to `@minos` first, then optionally installs OpenClaw
or Hermes support with Minos MCP live-data access and the local Minos
skill/persona files from `docs/ai-assistant/`.

## Files

- `agent-instruction.md` - behavior and safety layer for the agent
- `protocol-rules-and-rewards.md` - stable public protocol shape, round cadence, eligibility, rewards, dust, and score/weight interpretation
- `validator-guide.md` - public-safe validator role, startup concept, workload, and support boundary
- `scoring-and-advancedscorer.md` - scoring flow, hap.py concepts, score components, and AdvancedScorer overview
- `hardware-and-runtime.md` - Docker, PM2, miner/validator runtime, tool tradeoffs, and safe runtime checks
- `live-data-boundary.md` - rule that live/current facts come from Minos MCP or public endpoints, not static memory
- `quickstart.md` - beginner-facing onboarding guide
- `troubleshooting.md` - operational support playbook
- `endpoint-safety.md` - public endpoint and paste-back boundaries
- `config-tuning.md` - safe beginner tuning principles
- `glossary.md` - beginner glossary for Minos, Bittensor, and genomics terms
- `cards/*.md` - focused public memory cards for individual retrieval topics
- `evals.md` - maintainer evaluation and safety checks
- `manifest.yaml` - version, graph target, subjects, and safety exclusions

Runtime assets live beside this pack:

- `../minos-miner-skill/` - portable local skill for OpenClaw and Hermes
- `../openclaw-workspace/` - OpenClaw `AGENTS.md`, `SOUL.md`, and `TOOLS.md` Minos blocks
- `../hermes/SOUL.minos-miner-template.md` - Hermes persona template
- `../model-provider-setup.md` - OpenClaw/Hermes provider setup guidance

## Canonical Subjects

The canonical launch subject list lives in `manifest.yaml`. Seeded memories use
those exact names in their `Primary subject` and `Subjects` metadata so Ditto
does not create duplicate near-synonym subjects.

Important subject groups:

- Minos SN107, Public knowledge graph
- Miner onboarding, Demo mode, Practice mode, Live mining, Bittensor wallet and hotkey
- Docker runtime, PM2 supervision, Hardware requirements, Runtime operations
- Variant calling, BAM files, VCF files, Reference genome and reads, SNPs and indels
- GATK, DeepVariant, BCFtools, hap.py benchmarking, AdvancedScorer
- Scoring basics, Protocol rules, Rewards and emissions, Eligibility and weight
- Validators, Validator setup, Validator safety
- Public endpoints, Endpoint safety, Live data boundary, Miner monitoring
- Troubleshooting, Config tuning, How to compete well, Safe paste-back, Safe support behavior
- Assistant runtimes, Ditto, Minos MCP, OpenClaw, Hermes

## Safety Boundary

This pack is public-only. It must not include or request secrets, private
configs, private BAM/VCF files, private challenge data, database access, admin
endpoints, cloud bucket credentials, production infrastructure details,
presigned URLs, unreleased truth files, confident-region files, private validator data,
private benchmark data, model provider API keys, or guaranteed winning config
claims.
