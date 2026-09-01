<!-- MINOS-MINER-TOOLS-START -->
# Minos Miner Tool Notes

Useful local checks:

```bash
pm2 status
pm2 logs minos-miner --lines 50
docker info
docker ps
curl https://api.theminos.ai/health
```

Useful Minos commands:

```bash
bash install.sh
bash scripts/verify.sh --miner
bash start-miner.sh --demo
bash start-miner.sh --practice
bash start-miner.sh --practice --config configs/gatk.conf
bash start-miner.sh
bash start-miner.sh --setup-ditto
bash start-miner.sh --setup-ai-assistant
bash scripts/openclaw-tui.sh
openclaw gateway status
pm2 logs openclaw-gateway --lines 80
openclaw models status
openclaw models auth login --provider openai --set-default
openclaw models auth login --provider anthropic --set-default
openclaw models auth login --provider openrouter --set-default
```

Useful Minos MCP commands:

```bash
openclaw mcp show minos --json
openclaw mcp probe minos --json
openclaw mcp reload
```

Useful Ditto commands:

```bash
heyditto graphs list
heyditto graphs add @minos
heyditto search "Minos miner scoring eligibility"
bash scripts/setup_ditto_agent.sh --yes
npx -y @heyditto/cli status
```

Do not run destructive commands, edit miner configs, restart PM2, or change wallets unless the user explicitly asks.
<!-- MINOS-MINER-TOOLS-END -->
