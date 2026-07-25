# Minos Runtime Commands And Provider Setup

Use these public-safe commands when helping a Minos subnet 107 miner.

## Minos Commands

Install or update:

```bash
bash install.sh
```

Verify miner prerequisites:

```bash
bash scripts/verify.sh --miner
```

Demo mode:

```bash
bash start-miner.sh --demo
```

Start live miner:

```bash
bash start-miner.sh
```

PM2:

```bash
pm2 status
pm2 logs minos-miner --lines 50
```

Docker:

```bash
docker info
docker ps
```

Machine basics:

```bash
df -h
free -h
```

AI assistant setup:

```bash
bash start-miner.sh --setup-ditto
bash start-miner.sh --setup-ai-assistant
bash scripts/setup_ai_assistant.sh --with-ditto --openclaw
bash scripts/setup_ai_assistant.sh --with-ditto --hermes
```

## Public Endpoint Checks

Health:

```bash
curl https://api.theminos.ai/health
```

Scoring:

```text
GET https://api.theminos.ai/scoring/all
GET https://api.theminos.ai/scoring/detailed/{hotkey}
GET https://api.theminos.ai/scoring/parameter-ranges
GET https://api.theminos.ai/scoring/rounds/current/leaderboard
GET https://api.theminos.ai/scoring/rounds/latest-finalized/leaderboard
```

Do not manually call signed POST endpoints for beginners. The miner software
owns signed round-status and submission calls.

## Ditto CLI

Verify the public Minos graph subscription:

```bash
heyditto graphs list
```

Subscribe if needed:

```bash
heyditto graphs add @minos
```

Search Minos knowledge:

```bash
heyditto search "Minos miner PM2 online zero weight"
```

Search results should show `subscribed_graph:minos-public-knowledge-graph-1.0.0`
or `@minos / Minos SN107 - ...` source context. If graph subscription works but
search does not clearly return the 1.0.0 public graph, retry setup:

```bash
bash scripts/setup_ditto_agent.sh --yes
```

If `heyditto` is not globally installed:

```bash
npx -y @heyditto/cli graphs list
npx -y @heyditto/cli search "Minos miner PM2 online zero weight"
```

Verify Ditto auth:

```bash
npx -y @heyditto/cli status
```

## OpenClaw Setup Guidance

OpenClaw still needs a model provider. Ditto supplies memory, not the model.
The Minos setup script installs the global `heyditto` command, writes
`DITTO_API_KEY` into `~/.openclaw/.env` without printing it, installs the
`minos-miner` skill into the OpenClaw workspace, appends Minos context into
`AGENTS.md`, `SOUL.md`, and `TOOLS.md` with backups, and installs the OpenClaw
Ditto skill. If public graph retrieval cannot be verified, setup can save a
private fallback copy from the public Minos memory pack outside MinosVM. On
headless or container VMs, Minos runs `openclaw gateway run` under PM2 as
`openclaw-gateway` because OpenClaw's managed systemd-user gateway cannot run
without a user systemd bus.

Minos skips full OpenClaw onboarding by default, but it checks whether OpenClaw
already has a usable model provider. If no usable provider is found, Minos asks
the miner to choose OpenAI/Codex, Claude/Anthropic, OpenRouter, OpenClaw guided
provider setup, or skip.

Provider credentials must be entered only into OpenClaw or the provider OAuth
flow.

Common provider setup commands:

```bash
openclaw models auth login --provider openai --set-default
openclaw models auth login --provider anthropic --set-default
openclaw models auth login --provider openrouter --set-default
openclaw models auth add
```

Start OpenClaw:

```bash
bash scripts/openclaw-tui.sh
openclaw models status
openclaw gateway status
pm2 logs openclaw-gateway --lines 80
```

Minos MCP for live data:

```bash
openclaw mcp show minos --json
openclaw mcp probe minos --json
openclaw mcp reload
```

Repair OpenClaw Minos MCP manually:

```bash
openclaw mcp set minos '{"url":"https://mcp.theminos.ai","transport":"streamable-http","timeout":120,"connectTimeout":30,"supportsParallelToolCalls":true,"toolFilter":{"include":["get_current_round","get_leaderboard","list_recent_rounds","get_miner_history","get_subnet_overview"]}}'
openclaw mcp probe minos --json
openclaw mcp reload
```

If OpenClaw says Ditto is unavailable because `heyditto` is missing or
`DITTO_API_KEY` is not configured:

```bash
bash scripts/setup_ai_assistant.sh --with-ditto --openclaw
```

Never share the generated Ditto API key. Treat an unclaimed Ditto claim URL as
sensitive too: open it yourself, and do not paste it into public channels,
screenshots, or logs.

## Hermes Setup Guidance

Hermes still needs a model provider. Ditto supplies memory, not the model. The
Minos setup script installs Hermes noninteractively when needed, writes
`DITTO_API_KEY` into `~/.hermes/.env` without printing it, installs the
`minos-miner` skill, appends Minos context into `~/.hermes/SOUL.md`, writes
`HERMES_TUI_BACKGROUND=#0A0A0A`, and installs the Hermes Ditto skill.

Minos asks the miner to choose Hermes Portal, full Hermes setup, or skip.
Provider credentials must be entered only into Hermes or the provider OAuth
flow.

Hermes provider setup:

```bash
hermes setup --portal
hermes setup
```

Start Hermes with the Minos skill:

```bash
hermes --tui --skills minos-miner
hermes chat --cli --skills minos-miner
```

Minos MCP for live data is configured in `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  minos:
    url: "https://mcp.theminos.ai"
    enabled: true
    timeout: 120
    connect_timeout: 30
    supports_parallel_tool_calls: true
    tools:
      include:
        - get_current_round
        - get_leaderboard
        - list_recent_rounds
        - get_miner_history
        - get_subnet_overview
      resources: false
      prompts: false
```

After editing Hermes MCP config, reload it inside Hermes:

```text
/reload-mcp
```

If the Hermes CLI exposes MCP checks:

```bash
hermes mcp test minos
hermes mcp list
```

If `hermes chat` opens an unexpected editor screen in an SSH terminal:

```bash
export HERMES_TUI_BACKGROUND="${HERMES_TUI_BACKGROUND:-#0A0A0A}"
hermes chat --cli --skills minos-miner
```

If Hermes says Ditto is unavailable because `heyditto` is missing or
`DITTO_API_KEY` is not configured:

```bash
bash scripts/setup_ai_assistant.sh --with-ditto --hermes
```

Never paste model API keys into public support channels. Enter keys only into the
runtime's own setup flow or secret/config system.
