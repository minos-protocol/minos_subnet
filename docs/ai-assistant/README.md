# Minos Miner AI Assistant Runtime Pack

This directory contains local runtime assets for the **Minos Miner AI Assistant**
and the public Minos SN107 knowledge graph.

Ditto provides shared public Minos knowledge for miners, validators, and agent
runtimes through the `@minos` graph.
Minos MCP provides live public subnet data through `https://mcp.theminos.ai`.
OpenClaw and Hermes provide optional local agent runtimes. The files here make
those runtimes Minos-aware during setup.

## 1.0.0 Scope

The 1.0.0 setup is intentionally narrow:

1. Subscribe Ditto read-only to the public `@minos` knowledge graph.
2. Install OpenClaw by default, or Hermes if selected.
3. Install the Ditto skill into the selected runtime.
4. Configure the selected runtime to use Minos MCP for live/current subnet data.
5. Install the portable `minos-miner` skill and runtime persona files.
6. Check whether the runtime already has a usable model provider.
7. If needed, launch the selected runtime's provider setup path without Minos
   reading or storing provider credentials.

Minos 1.0.0 does not install, tune, or benchmark local open-source models. Most
miners should connect an existing provider such as Claude/Anthropic,
Codex/OpenAI, OpenRouter, or Hermes/Nous Portal.

## Setup

The friendly entrypoint is:

```bash
bash start-miner.sh --setup-ai-assistant
```

Direct commands:

```bash
bash scripts/setup_ai_assistant.sh --with-ditto --openclaw
bash scripts/setup_ai_assistant.sh --with-ditto --hermes
bash scripts/setup_ai_assistant.sh --ditto-only
```

On MinosVM, use:

```bash
bash scripts/minosvm_first_run.sh
```

## What Gets Installed

Ditto-only:

- Ditto CLI auth or a Ditto agent claim flow.
- Read-only subscription to the public `@minos` graph.
- Local private memory copies saved from `memory-pack/graph_seed_order` only if
  graph subscription or search cannot be verified, or fallback seeding is
  explicitly requested. MinosVM disables this fallback by default and relies on
  the public graph.
- Knowledge-graph subscription only. This is useful for Ditto web/app support, but it
  does not install a local OpenClaw/Hermes runtime.

OpenClaw runtime:

- Global `heyditto` command when needed.
- `DITTO_API_KEY` written to `~/.openclaw/.env` without printing it.
- `minos-miner` skill copied to the OpenClaw workspace.
- Minos `AGENTS.md`, `SOUL.md`, and `TOOLS.md` context blocks.
- OpenClaw Ditto skill.
- OpenClaw Minos MCP connection named `minos`, pointing to `https://mcp.theminos.ai`.
- Local OpenClaw gateway configured for loopback and run under PM2 as `openclaw-gateway` when PM2 is available.
- Provider check plus a short OpenAI/Codex, Claude/Anthropic, OpenRouter, guided setup, or skip menu.

Hermes runtime:

- Global `heyditto` command when needed.
- `DITTO_API_KEY` written to `~/.hermes/.env` without printing it.
- `HERMES_TUI_BACKGROUND=#0A0A0A` for cleaner SSH terminal startup.
- `minos-miner` skill copied to `~/.hermes/skills/minos-miner`.
- Minos SOUL context installed or appended to `~/.hermes/SOUL.md`.
- Hermes Ditto skill.
- Hermes Minos MCP connection named `minos`, merged into `~/.hermes/config.yaml`.
- Provider menu for Hermes/Nous Portal, full Hermes setup, or skip.

Minos MCP is for live/current public data such as current round, leaderboards,
recent rounds, miner history, subnet overview, incentives, and emissions. Ditto
remains the public knowledge graph for setup, troubleshooting, concepts, and
safety guidance.

## Files

- `minos-miner-skill/SKILL.md` - portable local skill for OpenClaw and Hermes.
- `minos-miner-skill/references/operator-guide.md` - miner lifecycle, troubleshooting, and safety.
- `minos-miner-skill/references/variant-calling-primer.md` - BAM/VCF/GATK/DeepVariant/BCFtools/hap.py basics.
- `minos-miner-skill/references/runtime-commands.md` - public commands, endpoints, Ditto CLI, and provider setup.
- `openclaw-workspace/AGENTS.md` - OpenClaw operating context block.
- `openclaw-workspace/SOUL.md` - OpenClaw persona block.
- `openclaw-workspace/TOOLS.md` - OpenClaw command notes.
- `hermes/SOUL.minos-miner-template.md` - Hermes persona template.
- `model-provider-setup.md` - human-facing provider setup guidance.

Related scripts:

- `scripts/prompt_ai_assistant.sh` - reusable first-run prompt.
- `scripts/setup_ai_assistant.sh` - Ditto, Minos MCP, OpenClaw, and Hermes setup.
- `scripts/setup_ditto_agent.sh` - Ditto CLI auth plus public `@minos` graph subscription, with private fallback seeding from public docs only when needed or explicitly requested.
- `scripts/openclaw-gateway.sh` - foreground OpenClaw gateway wrapper for PM2/headless VMs.
- `scripts/openclaw-tui.sh` - OpenClaw TUI launcher that reads the local gateway token without printing it.
- `scripts/minosvm_first_run.sh` - MinosVM first-run menu.

## Safety

The runtime pack is public-only. It must not include or ask for wallet secrets,
`.env`, API keys, SSH keys, request signatures, presigned URLs, private miner
configs, private BAM/VCF files, unreleased truth files, private validator data, admin endpoints,
database access, or production infrastructure details.

Model provider keys should be entered only into OpenClaw, Hermes, or the
provider's own trusted setup flow. Public support channels should never receive those
keys.
