#!/bin/bash
# Start Minos miner
cd "$(dirname "$0")"

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

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

# --- Setup .env if missing ---

if [ ! -f .env ]; then
    echo -e "${BLUE}"
    echo "  First-time miner setup"
    echo -e "${NC}"

    # Wallet setup
    echo -e "${BLUE}[1/3] Wallet setup:${NC}"
    echo "  1) Create new wallet"
    echo "  2) Import existing wallet (mnemonic)"
    echo "  3) Skip (wallet already exists)"
    read -p "  Choice (1/2/3): " WALLET_CHOICE

    read -p "  Wallet name [default]: " WALLET_NAME
    WALLET_NAME=${WALLET_NAME:-default}

    read -p "  Hotkey name [default]: " HOTKEY_NAME
    HOTKEY_NAME=${HOTKEY_NAME:-default}

    if [ "$WALLET_CHOICE" = "1" ]; then
        echo -e "${YELLOW}Creating wallet...${NC}"
        btcli wallet create --wallet-name "$WALLET_NAME" --wallet-hotkey "$HOTKEY_NAME"
    elif [ "$WALLET_CHOICE" = "2" ]; then
        echo -e "${YELLOW}Importing coldkey...${NC}"
        btcli wallet regen-coldkey --wallet-name "$WALLET_NAME"
        echo -e "${YELLOW}Importing hotkey...${NC}"
        btcli wallet regen-hotkey --wallet-name "$WALLET_NAME" --wallet-hotkey "$HOTKEY_NAME"
    fi

    # Tool selection
    echo ""
    echo -e "${BLUE}[2/3] Select variant calling tool:${NC}"
    echo "  1) GATK HaplotypeCaller (recommended)"
    echo "  2) DeepVariant (best accuracy, needs more resources)"
    echo "  3) FreeBayes"
    echo "  4) BCFtools (fastest)"
    read -p "  Choice (1/2/3/4) [1]: " TOOL_CHOICE

    case ${TOOL_CHOICE:-1} in
        1) MINER_TEMPLATE="gatk" ;;
        2) MINER_TEMPLATE="deepvariant" ;;
        3) MINER_TEMPLATE="freebayes" ;;
        4) MINER_TEMPLATE="bcftools" ;;
        *) MINER_TEMPLATE="gatk" ;;
    esac

    # Network
    echo ""
    echo -e "${BLUE}[3/3] Network:${NC}"
    echo "  1) Mainnet (finney)"
    echo "  2) Testnet (test)"
    read -p "  Choice (1/2) [1]: " NET_CHOICE

    NETWORK="finney"
    [ "$NET_CHOICE" = "2" ] && NETWORK="test"

    # Generate .env
    cat > .env << EOF
NETUID=107
NETWORK=$NETWORK
WALLET_NAME=$WALLET_NAME
WALLET_HOTKEY=$HOTKEY_NAME
MINER_TEMPLATE=$MINER_TEMPLATE
PLATFORM_URL=https://api.theminos.ai
PLATFORM_TIMEOUT=60
STORAGE_PRIMARY_BACKEND=hippius
EOF

    echo -e "${GREEN}.env created${NC}"
    echo ""
fi

# Load .env
set -a; source .env; set +a

echo -e "${GREEN}Starting Minos Miner (${MINER_TEMPLATE:-gatk})...${NC}"
python -m neurons.miner \
    --netuid ${NETUID:-107} \
    --subtensor.network ${NETWORK:-finney} \
    --wallet.name ${WALLET_NAME:-default} \
    --wallet.hotkey ${WALLET_HOTKEY:-default}
