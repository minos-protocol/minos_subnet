#!/bin/bash
# Start Minos validator
cd "$(dirname "$0")"

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# --- Parse flags ---

FLAG_WALLET_NAME=""
FLAG_WALLET_HOTKEY=""
FLAG_STORAGE=""
RUN_SETUP=false

show_help() {
    echo "Usage: bash start-validator.sh [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --wallet-name <name>        Wallet name"
    echo "  --wallet-hotkey <name>      Hotkey name"
    echo "  --storage <backend>         Fetch order: hippius (default) or aws_s3 (R2/AWS first)"
    echo "  --setup                     Re-run interactive setup wizard"
    echo "  --help                      Show this help message"
    echo ""
    echo "Examples:"
    echo "  bash start-validator.sh                                # First run: interactive setup"
    echo "  bash start-validator.sh --wallet-name validator"
    echo "  bash start-validator.sh --setup                        # Re-run setup wizard"
    exit 0
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --wallet-name) FLAG_WALLET_NAME="$2"; shift 2 ;;
        --wallet-hotkey) FLAG_WALLET_HOTKEY="$2"; shift 2 ;;
        --storage) FLAG_STORAGE="$2"; shift 2 ;;
        --setup) RUN_SETUP=true; shift ;;
        --help|-h) show_help ;;
        *) echo -e "${RED}Unknown option: $1${NC}"; echo "Run with --help for usage."; exit 1 ;;
    esac
done

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
    local images=(
        "genonet/hap-py@sha256:03acabe84bbfba35f5a7234129d524c563f5657e1f21150a2ea2797f8e6d05f2"
        "broadinstitute/gatk:4.5.0.0"
        "google/deepvariant:1.5.0"
        "staphb/freebayes:1.3.7"
        "quay.io/biocontainers/bcftools:1.20--h8b25389_0"
        "quay.io/biocontainers/samtools:1.20--h50ea8bc_0"
    )
    local image

    if [ -f datasets/reference/chr20/chr20.fa ] || [ -f datasets/reference/chr20.fa ]; then
        ref_ok=true
    fi

    for image in "${images[@]}"; do
        if ! docker image inspect "$image" >/dev/null 2>&1; then
            missing=$((missing + 1))
        fi
    done

    if [ "$ref_ok" = true ] && [ "$missing" -eq 0 ]; then
        return 0
    fi

    echo -e "${YELLOW}Required Minos validator assets are missing; downloading them automatically.${NC}"
    if [ "$ref_ok" != true ]; then
        echo "  - reference data"
    fi
    if [ "$missing" -gt 0 ]; then
        echo "  - ${missing} Docker image(s)"
    fi

    if ! python setup.py --update-data-only; then
        echo -e "${RED}Could not download required validator assets.${NC}"
        echo "  Retry: python setup.py --update-data-only"
        exit 1
    fi
}

# --- Load existing .env defaults (if any) ---

if [ -f .env ]; then
    set -a; source .env; set +a
fi

# A bare "sed s/^KEY=.*/KEY=value/" only substitutes: when the key was absent
# from an older .env the write silently did nothing and the flag reverted on the
# next pm2 restart, and any value containing '/' (an S3-style endpoint) broke the
# s/// expression outright. Same helper as start-miner.sh.
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

if [ ! -f .env ] || [ "$RUN_SETUP" = true ]; then
    # Use flag values or existing .env values as defaults
    DEFAULT_WALLET_NAME="${FLAG_WALLET_NAME:-${WALLET_NAME:-default}}"
    DEFAULT_WALLET_HOTKEY="${FLAG_WALLET_HOTKEY:-${WALLET_HOTKEY:-default}}"
    DEFAULT_STORAGE="${FLAG_STORAGE:-${STORAGE_PRIMARY_BACKEND:-hippius}}"

    echo -e "${BLUE}"
    if [ "$RUN_SETUP" = true ]; then
        echo "  Minos validator setup (current defaults shown in brackets)"
    else
        echo "  First-time validator setup"
    fi
    echo -e "${NC}"

    # Wallet setup
    echo -e "${BLUE}Wallet setup:${NC}"
    echo "  1) Create new wallet"
    echo "  2) Import wallet (mnemonic)"
    echo "  3) Use existing wallet"
    read -p "  Choice (1/2/3): " WALLET_CHOICE

    if [ "$WALLET_CHOICE" = "3" ]; then
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
            # Bash >= 4.3 resolves a negative subscript from the END of the
            # array, so a bare Enter here made W_IDX-1 == -1 and silently
            # launched the validator on the last wallet in the list.
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

    # Per-job scoring resources auto-tune from host CPU/RAM at startup.
    # Override via SCORING_THREADS, SCORING_MEMORY_GB (>=16 for DeepVariant),
    # or MINOS_VALIDATOR_CONCURRENCY.
    cat > .env << EOF
NETUID=107
WALLET_NAME=$WALLET_NAME
WALLET_HOTKEY=$HOTKEY_NAME
PLATFORM_URL=https://api.theminos.ai
PLATFORM_TIMEOUT=60
STORAGE_PRIMARY_BACKEND=${DEFAULT_STORAGE}
EOF

    echo -e "${GREEN}.env created${NC}"
    echo ""

    # Reload
    set -a; source .env; set +a
fi

ensure_runtime_assets

echo -e "${GREEN}Starting Minos Validator...${NC}"
python -m neurons.validator \
    --netuid ${NETUID:-107} \
    --subtensor.network finney \
    --wallet.name ${WALLET_NAME:-default} \
    --wallet.hotkey ${WALLET_HOTKEY:-default}
