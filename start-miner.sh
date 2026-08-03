#!/bin/bash
# Start Minos miner
cd "$(dirname "$0")"

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# --- Parse flags ---

FLAG_WALLET_NAME=""
FLAG_WALLET_HOTKEY=""
FLAG_MINER_TEMPLATE=""
FLAG_STORAGE=""
FLAG_DEMO=false
FLAG_PRACTICE=false
FLAG_PRACTICE_CONFIG=""
FLAG_PRACTICE_SAMPLE_ID=""
FLAG_SETUP_DITTO=false
FLAG_SETUP_AI_ASSISTANT=false
RUN_SETUP=false
CREATED_OR_UPDATED_ENV=false

show_help() {
    echo "Usage: bash start-miner.sh [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --wallet-name <name>        Wallet name"
    echo "  --wallet-hotkey <name>      Hotkey name"
    echo "  --miner-template <tool>     Variant caller: gatk, deepvariant, bcftools"
    echo "  --storage <backend>         Fetch order: hippius (default) or aws_s3 (R2/AWS first)"
    echo "  --demo                      Score your config on a fixed sample and see your"
    echo "                              result (no wallet, no chain). A one-shot onboarding"
    echo "                              run: download a fully-answered sample, run your"
    echo "                              config, print the exact score a validator would give."
    echo "  --practice                  Interactive practice mode: pick a fully-answered"
    echo "                              sample, run your config, and see your score"
    echo "                              (no wallet needed, no chain connection)"
    echo "  --config <file>             [--practice] Config file to score (default configs/gatk.conf)"
    echo "  --sample-id <id>            [--practice] Skip the picker; use this sample directly"
    echo "  --setup-ditto               Subscribe Ditto to the public @minos knowledge graph"
    echo "  --setup-ai-assistant        Open the Minos assistant setup menu"
    echo "  --setup                     Re-run interactive setup wizard"
    echo "  --help                      Show this help message"
    echo ""
    echo "Examples:"
    echo "  bash start-miner.sh                                    # First run: interactive setup"
    echo "  bash start-miner.sh --wallet-name miner --miner-template deepvariant"
    echo "  bash start-miner.sh --demo                             # One-shot: score your config on a fixed sample (no wallet)"
    echo "  bash start-miner.sh --practice                         # Interactive: pick a sample and score your config"
    echo "  bash start-miner.sh --practice --config configs/gatk.conf --sample-id <sample-id>"
    echo "  bash start-miner.sh --setup-ditto                      # Add optional Ditto @minos knowledge"
    echo "  bash start-miner.sh --setup-ai-assistant               # Choose assistant setup; MinosVM offers OpenClaw/Hermes or skip"
    echo "  bash start-miner.sh --setup                            # Re-run setup wizard"
    echo "  bash start-miner.sh --miner-template bcftools          # Change tool and restart"
    exit 0
}

require_value() {
    local flag="$1"
    local value="${2:-}"
    if [ -z "$value" ] || [[ "$value" == --* ]]; then
        echo -e "${RED}Missing value for $flag${NC}" >&2
        echo "Run with --help for usage." >&2
        exit 1
    fi
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --wallet-name) require_value "$1" "${2:-}"; FLAG_WALLET_NAME="$2"; shift 2 ;;
        --wallet-hotkey) require_value "$1" "${2:-}"; FLAG_WALLET_HOTKEY="$2"; shift 2 ;;
        --miner-template) require_value "$1" "${2:-}"; FLAG_MINER_TEMPLATE="$2"; shift 2 ;;
        --storage) require_value "$1" "${2:-}"; FLAG_STORAGE="$2"; shift 2 ;;
        --demo) FLAG_DEMO=true; shift ;;
        --practice) FLAG_PRACTICE=true; shift ;;
        --config) require_value "$1" "${2:-}"; FLAG_PRACTICE_CONFIG="$2"; shift 2 ;;
        --sample-id) require_value "$1" "${2:-}"; FLAG_PRACTICE_SAMPLE_ID="$2"; shift 2 ;;
        --setup-ditto) FLAG_SETUP_DITTO=true; shift ;;
        --setup-ai-assistant) FLAG_SETUP_AI_ASSISTANT=true; shift ;;
        --setup) RUN_SETUP=true; shift ;;
        --help|-h) show_help ;;
        *) echo -e "${RED}Unknown option: $1${NC}"; echo "Run with --help for usage."; exit 1 ;;
    esac
done

if [ "$FLAG_SETUP_DITTO" = true ]; then
    DITTO_SETUP_SCRIPT="scripts/setup_ditto_agent.sh"
    if [ ! -f "$DITTO_SETUP_SCRIPT" ]; then
        echo -e "${RED}Ditto setup script not found: $DITTO_SETUP_SCRIPT${NC}"
        exit 1
    fi
    exec bash "$DITTO_SETUP_SCRIPT" --yes
fi

if [ "$FLAG_SETUP_AI_ASSISTANT" = true ]; then
    AI_ASSISTANT_PROMPT_SCRIPT="scripts/prompt_ai_assistant.sh"
    if [ ! -f "$AI_ASSISTANT_PROMPT_SCRIPT" ]; then
        echo -e "${RED}AI assistant setup script not found: $AI_ASSISTANT_PROMPT_SCRIPT${NC}"
        exit 1
    fi
    if [ -f /opt/minosvm_venv/bin/activate ]; then
        exec bash "$AI_ASSISTANT_PROMPT_SCRIPT" --prompt --default y --role minosvm --runtime-only
    fi
    exec bash "$AI_ASSISTANT_PROMPT_SCRIPT" --prompt --default y --role manual
fi

maybe_prompt_ai_assistant() {
    local prompt_script
    if [ "$CREATED_OR_UPDATED_ENV" != true ]; then
        return
    fi
    if [ "$DEMO_INTENT" = true ]; then
        return
    fi
    if [ ! -t 0 ] || [ ! -t 1 ]; then
        return
    fi
    prompt_script="scripts/prompt_ai_assistant.sh"
    if [ -f "$prompt_script" ]; then
        if [ -f /opt/minosvm_venv/bin/activate ]; then
            bash "$prompt_script" --prompt --once --default y --role minosvm --runtime-only || true
            return
        fi
        bash "$prompt_script" --prompt --once --default y --role miner || true
    fi
}

# --- Check prerequisites ---

# 1. Venv
VENV=""
if [ -f /opt/minosvm_venv/bin/activate ]; then
    VENV="/opt/minosvm_venv"
elif [ -f .venv/bin/activate ]; then
    VENV=".venv"
fi

if [ -z "$VENV" ]; then
    echo -e "${RED}Python environment not found.${NC}"
    echo "  Run: bash install.sh"
    exit 1
fi

# 2. Docker
if ! docker info >/dev/null 2>&1; then
    echo -e "${RED}Docker is not running.${NC}"
    echo "  Run: bash install.sh"
    exit 1
fi

# Activate venv
source "$VENV/bin/activate"

ensure_runtime_assets() {
    local ref_ok=false
    local missing=0
    local template="${MINER_TEMPLATE:-gatk}"
    local images=()
    local image

    if [ -f datasets/reference/chr20/chr20.fa ] || [ -f datasets/reference/chr20.fa ]; then
        ref_ok=true
    fi

    case "$template" in
        gatk)
            images=(
                "broadinstitute/gatk:4.5.0.0"
                "quay.io/biocontainers/samtools:1.20--h50ea8bc_0"
                "quay.io/biocontainers/bcftools:1.20--h8b25389_0"
            )
            ;;
        deepvariant)
            images=("google/deepvariant:1.5.0")
            ;;
        bcftools)
            images=("quay.io/biocontainers/bcftools:1.20--h8b25389_0")
            ;;
    esac

    for image in "${images[@]}"; do
        if ! docker image inspect "$image" >/dev/null 2>&1; then
            missing=$((missing + 1))
        fi
    done

    if [ "$ref_ok" = true ] && [ "$missing" -eq 0 ]; then
        return 0
    fi

    echo -e "${YELLOW}Required Minos runtime assets are missing; downloading them automatically.${NC}"
    if [ "$ref_ok" != true ]; then
        echo "  - reference data"
    fi
    if [ "$missing" -gt 0 ]; then
        echo "  - ${missing} Docker image(s) for ${template}"
    fi

    if ! python setup.py --update-data-only; then
        echo -e "${RED}Could not download required runtime assets.${NC}"
        echo "  Retry: python setup.py --update-data-only"
        exit 1
    fi
}

# --- Compute demo intent from flag AND env var (must happen BEFORE the
# practice/demo fast-path and before .env is sourced, so MINER_DEMO from the
# parent shell environment is honored even when there is no .env yet — e.g.
# `MINER_DEMO=true bash start-miner.sh` exports MINER_DEMO before we source
# .env).
# Portable lowercase (works on macOS bash 3.2 — ${VAR,,} is bash 4+).
# Accepted values match the Python miner's env parser (1|true|yes|on).
MINER_DEMO_LC="$(printf '%s' "${MINER_DEMO:-}" | tr '[:upper:]' '[:lower:]')"
case "$MINER_DEMO_LC" in 1|true|yes|on) MINER_DEMO_FLAG=true ;; *) MINER_DEMO_FLAG=false ;; esac
if [ "$FLAG_DEMO" = true ] || [ "$MINER_DEMO_FLAG" = true ]; then
    DEMO_INTENT=true
else
    DEMO_INTENT=false
fi

# --- Practice / demo fast-path ------------------------------------------------
# Both --practice and --demo are one-shot scoring tools: download a
# fully-answered sample, run the user's config, and print the exact score a
# validator would compute. They need the venv, Docker, reference data, and
# PLATFORM_URL — but no wallet, no .env, and no chain. Short-circuit here so
# none of the wallet/setup machinery below runs. Honors an .env for
# PLATFORM_URL / MINER_TEMPLATE if one exists, but does not require it.
#
# The only difference: --demo is pinned to a single fixed sample (no picker),
# so a brand-new operator gets a score in one command. --practice is
# interactive (or takes --sample-id / --config to skip the menus).
if [ "$FLAG_PRACTICE" = true ] || [ "$DEMO_INTENT" = true ]; then
    if [ -f .env ]; then
        set -a; source .env; set +a
    fi
    : "${PLATFORM_URL:=https://api.theminos.ai}"
    export PLATFORM_URL
    # --miner-template wins over .env / the gatk default so a caller can pick
    # the tool to score with in one shot, e.g. --demo --miner-template deepvariant.
    if [ -n "$FLAG_MINER_TEMPLATE" ]; then
        case "$FLAG_MINER_TEMPLATE" in
            gatk|deepvariant|bcftools) MINER_TEMPLATE="$FLAG_MINER_TEMPLATE" ;;
            *)
                echo -e "${RED}invalid --miner-template '$FLAG_MINER_TEMPLATE'. Choose gatk, deepvariant, or bcftools.${NC}"
                exit 1
                ;;
        esac
    fi
    export MINER_TEMPLATE="${MINER_TEMPLATE:-gatk}"

    # --demo is pinned to a chr20 sample, so on a fresh box it only needs the
    # chr20 reference. Signal demo scope to setup.py (via ensure_runtime_assets)
    # so it fetches chr20 (~60 MB) instead of the full validator superset. Only
    # for --demo — --practice can pick a chr19/chr21/chr22 sample and needs the wider set.
    if [ "$DEMO_INTENT" = true ] && [ "$FLAG_PRACTICE" != true ]; then
        export MINER_DEMO=true
    fi

    ensure_runtime_assets

    # Chromosomes the chosen mode can serve: --demo is pinned to a chr20 sample,
    # --practice can pick chr19/chr20/chr21/chr22. Fetch only those, not the ~5 GB
    # all-chromosome validator superset.
    REF_BASE="${REF_S3_BASE:-https://api.theminos.ai/reference}"
    if [ "$FLAG_PRACTICE" = true ]; then
        PRACTICE_CHROMS="chr19 chr20 chr21 chr22"
    else
        PRACTICE_CHROMS="chr20"
    fi

    # Reference FASTA (+ .fai + .dict) for those chromosomes. ensure_runtime_assets
    # only probes chr20.fa, and the demo-scoped data set fetches chr20 alone — so
    # on a demo-first box a chr19/chr21/chr22 practice sample would otherwise fail with
    # "reference not found for <chrom>". Fetch any that are missing (~60 MB/chrom).
    for chrom in $PRACTICE_CHROMS; do
        fa_dir="datasets/reference/$chrom"
        if [ -f "$fa_dir/$chrom.fa" ]; then
            continue
        fi
        echo -e "${YELLOW}Fetching reference FASTA for $chrom (needed to call)...${NC}"
        mkdir -p "$fa_dir"
        for ext in fa fa.fai dict; do
            if ! curl -fsSL "$REF_BASE/$chrom/$chrom.$ext" -o "$fa_dir/$chrom.$ext"; then
                echo -e "${RED}Could not download reference $chrom.$ext. Scoring will fail.${NC}"
                rm -f "$fa_dir/$chrom.$ext"
                exit 1
            fi
        done
    done

    # Scoring uses hap.py/vcfeval, which needs the RTG SDF — a validator-only
    # asset the normal miner data set omits. Fetch it for the same chromosomes
    # (~24 MB/chrom). The SDF files are public.
    SDF_FILES="done mainIndex nameIndex0 namedata0 namepointer0 progress seqdata0 seqpointer0 sequenceIndex0 summary.txt"
    for chrom in $PRACTICE_CHROMS; do
        sdf_dir="datasets/reference/$chrom/$chrom.sdf"
        if [ -f "$sdf_dir/seqdata0" ]; then
            continue
        fi
        echo -e "${YELLOW}Fetching RTG SDF for $chrom (needed to score)...${NC}"
        mkdir -p "$sdf_dir"
        for f in $SDF_FILES; do
            if ! curl -fsSL "$REF_BASE/$chrom/$chrom.sdf/$f" -o "$sdf_dir/$f"; then
                echo -e "${RED}Could not download SDF file $chrom/$f. Scoring will fail.${NC}"
                rm -rf "$sdf_dir"
                exit 1
            fi
        done
    done

    PRACTICE_ARGS=(--practice)
    if [ "$DEMO_INTENT" = true ]; then
        # --demo: one-shot score on a single fixed sample (no picker). The
        # sample id is env-overridable so ops can rotate it without a code
        # change; the default is a chr20 sample from the practice set.
        DEMO_SAMPLE_ID="${DEMO_SAMPLE_ID:-d7b99f4a-061c-4202-8b12-2c1425bd4974}"
        PRACTICE_ARGS+=(--sample_id "$DEMO_SAMPLE_ID")
        PRACTICE_ARGS+=(--config "${FLAG_PRACTICE_CONFIG:-configs/${MINER_TEMPLATE}.conf}")
        echo -e "${GREEN}Starting Minos Miner (${MINER_TEMPLATE}) in DEMO MODE...${NC}"
        echo -e "${GREEN}  - no wallet required, no chain connection${NC}"
        echo -e "${GREEN}  - scores your config on a fixed sample so you can verify${NC}"
        echo -e "${GREEN}    your pipeline works and see the number a validator gives${NC}"
    else
        if [ -n "$FLAG_PRACTICE_CONFIG" ]; then
            PRACTICE_ARGS+=(--config "$FLAG_PRACTICE_CONFIG")
        fi
        if [ -n "$FLAG_PRACTICE_SAMPLE_ID" ]; then
            PRACTICE_ARGS+=(--sample_id "$FLAG_PRACTICE_SAMPLE_ID")
        fi
        echo -e "${GREEN}Starting Minos Miner (${MINER_TEMPLATE}) in PRACTICE MODE...${NC}"
        echo -e "${GREEN}  - no wallet required, no chain connection${NC}"
        echo -e "${GREEN}  - pick a sample, run your config, see your score${NC}"
    fi
    exec python -m neurons.miner "${PRACTICE_ARGS[@]}"
fi

# Past this point demo intent has already been handled (the fast-path above
# exec's for any --demo / MINER_DEMO invocation), so DEMO_INTENT is false here
# and the block below is the normal wallet/chain setup path.

# --- Load existing .env defaults (if any) ---

if [ -f .env ]; then
    set -a; source .env; set +a
fi

set_env_value() {
    local key="$1"
    local value="$2"
    local escaped_value

    escaped_value="$(printf '%s' "$value" | sed 's/[\/&]/\\&/g')"
    if grep -q "^${key}=" .env; then
        sed -i.bak "s/^${key}=.*/${key}=${escaped_value}/" .env && rm -f .env.bak
    else
        printf '%s=%s\n' "$key" "$value" >> .env
    fi
}

first_existing_wallet_hotkey() {
    local wallet_root="$HOME/.bittensor/wallets"
    local wallet_path hotkey_path wallet_name hotkey_name

    [ -d "$wallet_root" ] || return 1

    for wallet_path in "$wallet_root"/*; do
        [ -d "$wallet_path" ] || continue
        wallet_name="$(basename "$wallet_path")"
        for hotkey_path in "$wallet_path"/hotkeys/*; do
            [ -f "$hotkey_path" ] || continue
            hotkey_name="$(basename "$hotkey_path")"
            printf '%s\t%s\n' "$wallet_name" "$hotkey_name"
            return 0
        done
    done

    return 1
}

# --- Demo fast-path: any signal of demo intent + no .env → skip wallet wizard ---
# The demo flow uses an ephemeral keypair generated inside the Python miner,
# so wallet prompts are actively wrong here. We write a self-documenting .env
# so downstream tooling (PM2, .env loaders, etc.) finds the expected file,
# AND so the user can switch back to live by editing two lines.
#
# WALLET_NAME/HOTKEY are written as `default` placeholders (not omitted) so
# (a) the .env is a complete template the user can edit for live mode, and
# (b) the --wallet-name / --wallet-hotkey flag handlers below find existing
# keys to sed-replace instead of silently no-op'ing.
if [ "$DEMO_INTENT" = true ] && [ ! -f .env ]; then
    DEMO_TEMPLATE="${FLAG_MINER_TEMPLATE:-${MINER_TEMPLATE:-gatk}}"
    case "$DEMO_TEMPLATE" in
        gatk|deepvariant|bcftools) ;;
        *)
            echo -e "${YELLOW}Unknown template '$DEMO_TEMPLATE' — defaulting to gatk for demo.${NC}"
            DEMO_TEMPLATE="gatk"
            ;;
    esac
    DEMO_STORAGE="${FLAG_STORAGE:-${STORAGE_PRIMARY_BACKEND:-hippius}}"
    cat > .env <<EOF
# Minos miner — DEMO MODE configuration
# Auto-generated by start-miner.sh on first demo launch.
#
# To switch to LIVE mining (earn TAO, real scoring):
#   1. Set WALLET_NAME / WALLET_HOTKEY below to a wallet you control
#      (or run \`bash start-miner.sh --setup\` to re-do the wizard)
#   2. Register that hotkey on subnet 107:
#        btcli subnets register --netuid 107 \\
#          --wallet.name <name> --wallet.hotkey <hotkey>
#   3. Change MINER_DEMO=true to MINER_DEMO=false (or delete the line)
#   4. Re-run: bash start-miner.sh
#
NETUID=107
WALLET_NAME=default
WALLET_HOTKEY=default
MINER_TEMPLATE=$DEMO_TEMPLATE
MINER_DEMO=true
PLATFORM_URL=https://api.theminos.ai
PLATFORM_TIMEOUT=60
STORAGE_PRIMARY_BACKEND=$DEMO_STORAGE
EOF
    set -a; source .env; set +a
    echo -e "${GREEN}.env created for demo (template: $DEMO_TEMPLATE)${NC}"
fi

# --- Apply flag overrides directly (no wizard) ---

if [ -f .env ] && [ "$RUN_SETUP" = false ]; then
    CHANGED=false

    if [ -n "$FLAG_WALLET_NAME" ]; then
        set_env_value "WALLET_NAME" "$FLAG_WALLET_NAME"
        WALLET_NAME="$FLAG_WALLET_NAME"
        CHANGED=true
    fi
    if [ -n "$FLAG_WALLET_HOTKEY" ]; then
        set_env_value "WALLET_HOTKEY" "$FLAG_WALLET_HOTKEY"
        WALLET_HOTKEY="$FLAG_WALLET_HOTKEY"
        CHANGED=true
    fi
    if [ -n "$FLAG_MINER_TEMPLATE" ]; then
        case "$FLAG_MINER_TEMPLATE" in
            gatk|deepvariant|bcftools) ;;
            freebayes)
                echo -e "${RED}freebayes was deprecated 2026-05-09 16:00 UTC. Choose gatk, deepvariant, or bcftools.${NC}"
                exit 1
                ;;
            *)
                echo -e "${RED}invalid --miner-template '$FLAG_MINER_TEMPLATE'. Choose gatk, deepvariant, or bcftools.${NC}"
                exit 1
                ;;
        esac
        set_env_value "MINER_TEMPLATE" "$FLAG_MINER_TEMPLATE"
        MINER_TEMPLATE="$FLAG_MINER_TEMPLATE"
        CHANGED=true
    fi
    if [ -n "$FLAG_STORAGE" ]; then
        set_env_value "STORAGE_PRIMARY_BACKEND" "$FLAG_STORAGE"
        STORAGE_PRIMARY_BACKEND="$FLAG_STORAGE"
        CHANGED=true
    fi

    if [ "$CHANGED" = true ]; then
        echo -e "${GREEN}.env updated${NC}"
    fi
fi

# --- Interactive setup (first run or --setup) ---
# Demo-mode launches never need this — the wallet wizard would prompt
# for a hotkey the demo miner won't use. Skip when EITHER --demo is set
# or MINER_DEMO is truthy in the env. (In practice demo intent is already
# handled by the fast-path far above, which exec's; this is belt-and-braces.)

if { [ ! -f .env ] || [ "$RUN_SETUP" = true ]; } && [ "$DEMO_INTENT" = false ]; then
    if [ ! -t 0 ] || [ ! -t 1 ]; then
        echo -e "${RED}Miner setup needs an interactive terminal because .env is missing or --setup was requested.${NC}"
        echo "  Run manually: bash start-miner.sh --setup"
        echo "  Demo mode:    bash start-miner.sh --demo"
        echo "  PM2 live mode requires .env first: bash start-miner.sh --setup, then bash pm2-miner.sh"
        exit 1
    fi

    # Use flag values or existing .env values as defaults
    DEFAULT_WALLET_NAME="${FLAG_WALLET_NAME:-${WALLET_NAME:-default}}"
    DEFAULT_WALLET_HOTKEY="${FLAG_WALLET_HOTKEY:-${WALLET_HOTKEY:-default}}"
    DEFAULT_MINER_TEMPLATE="${FLAG_MINER_TEMPLATE:-${MINER_TEMPLATE:-gatk}}"
    DEFAULT_STORAGE="${FLAG_STORAGE:-${STORAGE_PRIMARY_BACKEND:-hippius}}"

    echo -e "${BLUE}"
    if [ "$RUN_SETUP" = true ]; then
        echo "  Minos miner setup (current defaults shown in brackets)"
    else
        echo "  First-time miner setup"
    fi
    echo -e "${NC}"

    # Wallet setup
    SKIP_WALLET_MENU=false
    echo -e "${BLUE}[1/2] Wallet setup:${NC}"
    if [ -z "$FLAG_WALLET_NAME" ] && [ -z "$FLAG_WALLET_HOTKEY" ]; then
        DETECTED_PAIR="$(first_existing_wallet_hotkey || true)"
        if [ -n "$DETECTED_PAIR" ]; then
            DETECTED_WALLET="${DETECTED_PAIR%%	*}"
            DETECTED_HOTKEY="${DETECTED_PAIR#*	}"
            echo -e "  Detected wallet: ${GREEN}${DETECTED_WALLET}/${DETECTED_HOTKEY}${NC}"
            read -p "  Use this wallet for live mining? [Y/n]: " USE_DETECTED
            USE_DETECTED="${USE_DETECTED:-y}"
            case "$USE_DETECTED" in
                y|Y|yes|YES)
                    WALLET_NAME="$DETECTED_WALLET"
                    HOTKEY_NAME="$DETECTED_HOTKEY"
                    SKIP_WALLET_MENU=true
                    echo -e "  Using wallet: ${GREEN}$WALLET_NAME/$HOTKEY_NAME${NC}"
                    ;;
            esac
        fi
    fi

    if [ "$SKIP_WALLET_MENU" != true ]; then
        echo "  1) Create new wallet"
        echo "  2) Import wallet (mnemonic)"
        echo "  3) Use existing wallet"
        read -p "  Choice (1/2/3) [3]: " WALLET_CHOICE
        WALLET_CHOICE="${WALLET_CHOICE:-3}"
    fi

    if [ "$SKIP_WALLET_MENU" = true ]; then
        :
    elif [ "$WALLET_CHOICE" = "3" ]; then
        # Auto-detect wallets from ~/.bittensor/wallets/
        WALLET_DIR="$HOME/.bittensor/wallets"
        if [ -d "$WALLET_DIR" ] && [ "$(ls -A "$WALLET_DIR" 2>/dev/null)" ]; then
            echo ""
            echo -e "${BLUE}  Detected wallets:${NC}"
            WALLETS=($(ls -1 "$WALLET_DIR"))
            for i in "${!WALLETS[@]}"; do
                echo "    $((i+1))) ${WALLETS[$i]}"
            done
            read -p "  Select wallet (1-${#WALLETS[@]}): " W_IDX
            if ! [[ "$W_IDX" =~ ^[0-9]+$ ]] || [ "$W_IDX" -lt 1 ] || [ "$W_IDX" -gt "${#WALLETS[@]}" ]; then
                W_IDX=1
            fi
            WALLET_NAME="${WALLETS[$((W_IDX-1))]}"
            WALLET_NAME=${WALLET_NAME:-${WALLETS[0]}}

            # Auto-detect hotkeys
            HOTKEY_DIR="$WALLET_DIR/$WALLET_NAME/hotkeys"
            if [ -d "$HOTKEY_DIR" ] && [ "$(ls -A "$HOTKEY_DIR" 2>/dev/null)" ]; then
                HOTKEYS=($(ls -1 "$HOTKEY_DIR"))
                if [ ${#HOTKEYS[@]} -eq 1 ]; then
                    HOTKEY_NAME="${HOTKEYS[0]}"
                    echo -e "  Using hotkey: ${GREEN}$HOTKEY_NAME${NC}"
                else
                    echo ""
                    echo -e "${BLUE}  Detected hotkeys:${NC}"
                    for i in "${!HOTKEYS[@]}"; do
                        echo "    $((i+1))) ${HOTKEYS[$i]}"
                    done
                    read -p "  Select hotkey (1-${#HOTKEYS[@]}): " H_IDX
                    if ! [[ "$H_IDX" =~ ^[0-9]+$ ]] || [ "$H_IDX" -lt 1 ] || [ "$H_IDX" -gt "${#HOTKEYS[@]}" ]; then
                        H_IDX=1
                    fi
                    HOTKEY_NAME="${HOTKEYS[$((H_IDX-1))]}"
                    HOTKEY_NAME=${HOTKEY_NAME:-${HOTKEYS[0]}}
                fi
            else
                read -p "  Hotkey name [$DEFAULT_WALLET_HOTKEY]: " HOTKEY_NAME
                HOTKEY_NAME=${HOTKEY_NAME:-$DEFAULT_WALLET_HOTKEY}
            fi
        else
            echo -e "${YELLOW}  No wallets found in ~/.bittensor/wallets/${NC}"
            read -p "  Wallet name [$DEFAULT_WALLET_NAME]: " WALLET_NAME
            WALLET_NAME=${WALLET_NAME:-$DEFAULT_WALLET_NAME}
            read -p "  Hotkey name [$DEFAULT_WALLET_HOTKEY]: " HOTKEY_NAME
            HOTKEY_NAME=${HOTKEY_NAME:-$DEFAULT_WALLET_HOTKEY}
        fi
    else
        read -p "  Wallet name [$DEFAULT_WALLET_NAME]: " WALLET_NAME
        WALLET_NAME=${WALLET_NAME:-$DEFAULT_WALLET_NAME}

        read -p "  Hotkey name [$DEFAULT_WALLET_HOTKEY]: " HOTKEY_NAME
        HOTKEY_NAME=${HOTKEY_NAME:-$DEFAULT_WALLET_HOTKEY}

        if [ "$WALLET_CHOICE" = "1" ]; then
            echo -e "${YELLOW}Creating wallet...${NC}"
            btcli wallet create --wallet-name "$WALLET_NAME" --wallet-hotkey "$HOTKEY_NAME"
        elif [ "$WALLET_CHOICE" = "2" ]; then
            echo -e "${YELLOW}Importing coldkey...${NC}"
            btcli wallet regen-coldkey --wallet-name "$WALLET_NAME"
            echo -e "${YELLOW}Importing hotkey...${NC}"
            btcli wallet regen-hotkey --wallet-name "$WALLET_NAME" --wallet-hotkey "$HOTKEY_NAME"
        fi
    fi

    # Tool selection — highlight current default
    echo ""
    echo -e "${BLUE}[2/2] Select variant calling tool:${NC}"
    TOOLS=("gatk" "deepvariant" "bcftools")
    LABELS=("GATK HaplotypeCaller" "DeepVariant" "BCFtools")
    for i in "${!TOOLS[@]}"; do
        MARKER=""
        [ "${TOOLS[$i]}" = "$DEFAULT_MINER_TEMPLATE" ] && MARKER=" ${GREEN}(current)${NC}"
        echo -e "  $((i+1))) ${LABELS[$i]}${MARKER}"
    done

    # Find default number for current template
    DEFAULT_TOOL_NUM=1
    for i in "${!TOOLS[@]}"; do
        [ "${TOOLS[$i]}" = "$DEFAULT_MINER_TEMPLATE" ] && DEFAULT_TOOL_NUM=$((i+1))
    done

    read -p "  Choice (1/2/3) [$DEFAULT_TOOL_NUM]: " TOOL_CHOICE

    case ${TOOL_CHOICE:-$DEFAULT_TOOL_NUM} in
        1) MINER_TEMPLATE="gatk" ;;
        2) MINER_TEMPLATE="deepvariant" ;;
        3) MINER_TEMPLATE="bcftools" ;;
        *) MINER_TEMPLATE="$DEFAULT_MINER_TEMPLATE" ;;
    esac

    # Generate .env
    cat > .env << EOF
NETUID=107
WALLET_NAME=$WALLET_NAME
WALLET_HOTKEY=$HOTKEY_NAME
MINER_TEMPLATE=$MINER_TEMPLATE
PLATFORM_URL=https://api.theminos.ai
PLATFORM_TIMEOUT=60
STORAGE_PRIMARY_BACKEND=${DEFAULT_STORAGE}
EOF

    echo -e "${GREEN}.env created${NC}"
    echo ""
    CREATED_OR_UPDATED_ENV=true

    # Reload
    set -a; source .env; set +a
fi

# Demo intent may only become visible AFTER .env is sourced (a persisted
# MINER_DEMO=true with no --demo flag and nothing exported in the shell). The
# top-of-script fast-path runs before .env is read, so re-check here: if demo
# is set, re-exec through the fast-path with MINER_DEMO exported so it takes
# the demo scoring path instead of falling through to LIVE mining below (which
# would attempt the chain with a placeholder wallet).
MINER_DEMO_LC="$(printf '%s' "${MINER_DEMO:-}" | tr '[:upper:]' '[:lower:]')"
case "$MINER_DEMO_LC" in 1|true|yes|on) MINER_DEMO_FLAG=true ;; *) MINER_DEMO_FLAG=false ;; esac
if [ "$FLAG_DEMO" != true ] && [ "$MINER_DEMO_FLAG" = true ]; then
    export MINER_DEMO
    exec bash "$0" --demo
fi

if [ -f .env ] && [ -z "${MINER_TEMPLATE:-}" ]; then
    MINER_TEMPLATE="gatk"
    set_env_value "MINER_TEMPLATE" "$MINER_TEMPLATE"
fi

ensure_runtime_assets
maybe_prompt_ai_assistant

# Live mining. (Demo intent — --demo / MINER_DEMO — is handled by the
# practice/demo scoring fast-path above, which exec's and never returns here.)
echo -e "${GREEN}Starting Minos Miner (${MINER_TEMPLATE:-gatk})...${NC}"
python -m neurons.miner \
    --netuid ${NETUID:-107} \
    --subtensor.network finney \
    --wallet.name ${WALLET_NAME:-default} \
    --wallet.hotkey ${WALLET_HOTKEY:-default}
