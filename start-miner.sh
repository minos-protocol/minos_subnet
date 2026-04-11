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
RUN_SETUP=false

show_help() {
    echo "Usage: bash start-miner.sh [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --wallet-name <name>        Wallet name"
    echo "  --wallet-hotkey <name>      Hotkey name"
    echo "  --miner-template <tool>     Variant caller: gatk, deepvariant, freebayes, bcftools"
    echo "  --storage <backend>         Storage backend: hippius, aws_s3"
    echo "  --setup                     Re-run interactive setup wizard"
    echo "  --help                      Show this help message"
    echo ""
    echo "Examples:"
    echo "  bash start-miner.sh                                    # First run: interactive setup"
    echo "  bash start-miner.sh --wallet-name miner --miner-template deepvariant"
    echo "  bash start-miner.sh --setup                            # Re-run setup wizard"
    echo "  bash start-miner.sh --miner-template freebayes         # Change tool and restart"
    exit 0
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --wallet-name) FLAG_WALLET_NAME="$2"; shift 2 ;;
        --wallet-hotkey) FLAG_WALLET_HOTKEY="$2"; shift 2 ;;
        --miner-template) FLAG_MINER_TEMPLATE="$2"; shift 2 ;;
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

# 3. Reference data
REF_CHECK=""
if [ -f datasets/reference/chr20/chr20.fa ]; then
    REF_CHECK="new"
elif [ -f datasets/reference/chr20.fa ]; then
    REF_CHECK="legacy"
fi

if [ -z "$REF_CHECK" ]; then
    echo -e "${RED}Reference data not found.${NC}"
    echo "  Run: bash install.sh"
    exit 1
fi

# Activate venv
source "$VENV/bin/activate"

# --- Load existing .env defaults (if any) ---

if [ -f .env ]; then
    set -a; source .env; set +a
fi

# --- Apply flag overrides directly (no wizard) ---

if [ -f .env ] && [ "$RUN_SETUP" = false ]; then
    CHANGED=false

    if [ -n "$FLAG_WALLET_NAME" ]; then
        sed -i.bak "s/^WALLET_NAME=.*/WALLET_NAME=$FLAG_WALLET_NAME/" .env && rm -f .env.bak
        WALLET_NAME="$FLAG_WALLET_NAME"
        CHANGED=true
    fi
    if [ -n "$FLAG_WALLET_HOTKEY" ]; then
        sed -i.bak "s/^WALLET_HOTKEY=.*/WALLET_HOTKEY=$FLAG_WALLET_HOTKEY/" .env && rm -f .env.bak
        WALLET_HOTKEY="$FLAG_WALLET_HOTKEY"
        CHANGED=true
    fi
    if [ -n "$FLAG_MINER_TEMPLATE" ]; then
        sed -i.bak "s/^MINER_TEMPLATE=.*/MINER_TEMPLATE=$FLAG_MINER_TEMPLATE/" .env && rm -f .env.bak
        MINER_TEMPLATE="$FLAG_MINER_TEMPLATE"
        CHANGED=true
    fi
    if [ -n "$FLAG_STORAGE" ]; then
        sed -i.bak "s/^STORAGE_PRIMARY_BACKEND=.*/STORAGE_PRIMARY_BACKEND=$FLAG_STORAGE/" .env && rm -f .env.bak
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
    echo -e "${BLUE}[1/2] Wallet setup:${NC}"
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
    TOOLS=("gatk" "deepvariant" "freebayes" "bcftools")
    LABELS=("GATK HaplotypeCaller" "DeepVariant" "FreeBayes" "BCFtools")
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

    read -p "  Choice (1/2/3/4) [$DEFAULT_TOOL_NUM]: " TOOL_CHOICE

    case ${TOOL_CHOICE:-$DEFAULT_TOOL_NUM} in
        1) MINER_TEMPLATE="gatk" ;;
        2) MINER_TEMPLATE="deepvariant" ;;
        3) MINER_TEMPLATE="freebayes" ;;
        4) MINER_TEMPLATE="bcftools" ;;
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

    # Reload
    set -a; source .env; set +a
fi

echo -e "${GREEN}Starting Minos Miner (${MINER_TEMPLATE:-gatk})...${NC}"
python -m neurons.miner \
    --netuid ${NETUID:-107} \
    --subtensor.network finney \
    --wallet.name ${WALLET_NAME:-default} \
    --wallet.hotkey ${WALLET_HOTKEY:-default}
