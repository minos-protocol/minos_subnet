#!/usr/bin/env bash
# Minos Subnet 107 — Interactive Demo
# Runs a one-shot demo end-to-end so a new miner can verify their
# variant-calling pipeline works — and see the score it earns — before
# going live.
#
# What happens:
#   1. Runs verify.sh to check prerequisites (Docker, Python, etc.)
#   2. Ensures a .env file exists (creates a temporary demo one if not)
#   3. Starts the miner with --demo (no wallet, no chain)
#   4. The miner downloads a fixed, fully-answered sample (BAM + truth),
#      runs your chosen variant caller, and self-scores it
#   5. It prints the exact score a validator would give your config
#
# Usage: bash scripts/demo.sh [--template gatk|deepvariant|bcftools]
#        Run from the minos_subnet/ directory.
set -euo pipefail

# ---------------------------------------------------------------------------
# Colors (same palette as verify.sh)
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# ---------------------------------------------------------------------------
# Resolve directories
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
TEMPLATE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --template)
            TEMPLATE="$2"
            shift 2
            ;;
        --template=*)
            TEMPLATE="${1#*=}"
            shift
            ;;
        -h|--help)
            echo "Usage: bash scripts/demo.sh [--template gatk|deepvariant|bcftools]"
            echo ""
            echo "Runs a one-shot demo that scores your config to verify your miner setup."
            echo "If --template is not specified, the value from .env is used (default: gatk)."
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown argument: $1${NC}"
            echo "Usage: bash scripts/demo.sh [--template gatk|deepvariant|bcftools]"
            exit 1
            ;;
    esac
done

# Validate template choice if provided
if [[ -n "$TEMPLATE" ]]; then
    case "$TEMPLATE" in
        gatk|deepvariant|bcftools) ;;
        *)
            echo -e "${RED}Invalid template: $TEMPLATE${NC}"
            echo "Valid options: gatk, deepvariant, bcftools"
            exit 1
            ;;
    esac
fi

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
echo ""
echo -e "${BOLD}================================================================${NC}"
echo -e "${BOLD}  Minos SN107 — Demo Round${NC}"
echo -e "${BOLD}================================================================${NC}"
echo ""
echo -e "  This script runs a ${CYAN}one-shot demo${NC} end-to-end so you can confirm"
echo -e "  your miner setup works — and see its score — before going live."
echo ""
echo -e "  What will happen:"
echo -e "    1. Verify prerequisites (Docker, Python packages)"
echo -e "    2. Start the miner with ${CYAN}--demo${NC} — no wallet needed, no chain conn."
echo -e "    3. It downloads a fixed, fully-answered sample (BAM + truth)"
echo -e "    4. It runs your variant caller on the sample"
echo -e "    5. It prints the exact score a validator would give your config"
echo ""
echo -e "  ${YELLOW}Estimated time: 3-30 minutes${NC} (depends on your machine and"
echo -e "  the variant caller — bcftools is fastest, DeepVariant slowest)"
echo ""
echo -e "  Press Ctrl+C at any time to abort."
echo ""

# ---------------------------------------------------------------------------
# Step 1: Run verify.sh
# ---------------------------------------------------------------------------
echo -e "${BOLD}--- Step 1/3: Verifying prerequisites ---${NC}"
echo ""

if [[ ! -f "$SCRIPT_DIR/verify.sh" ]]; then
    echo -e "  ${RED}[FAIL]${NC} verify.sh not found at $SCRIPT_DIR/verify.sh"
    exit 1
fi

if ! bash "$SCRIPT_DIR/verify.sh" --miner; then
    echo ""
    echo -e "${RED}Prerequisites check failed. Fix the issues above and re-run.${NC}"
    exit 1
fi

echo ""
echo -e "  ${GREEN}Prerequisites OK.${NC}"
echo ""

# ---------------------------------------------------------------------------
# Step 2: Ensure .env exists
# ---------------------------------------------------------------------------
echo -e "${BOLD}--- Step 2/3: Checking configuration ---${NC}"
echo ""

CREATED_TEMP_ENV=false
ORIGINAL_TEMPLATE=""

if [[ -f "$PROJECT_DIR/.env" ]]; then
    echo -e "  ${GREEN}[OK]${NC} .env file found"

    # If user specified --template, temporarily override it in the env
    if [[ -n "$TEMPLATE" ]]; then
        ORIGINAL_TEMPLATE="$(grep -E '^MINER_TEMPLATE=' "$PROJECT_DIR/.env" 2>/dev/null | cut -d= -f2 || true)"
        if [[ "$ORIGINAL_TEMPLATE" != "$TEMPLATE" ]]; then
            # Use a temp copy so we don't modify the user's .env
            cp "$PROJECT_DIR/.env" "$PROJECT_DIR/.env.demo.bak"
            sed -i.tmp "s/^MINER_TEMPLATE=.*/MINER_TEMPLATE=$TEMPLATE/" "$PROJECT_DIR/.env"
            rm -f "$PROJECT_DIR/.env.tmp"
            echo -e "  ${YELLOW}[INFO]${NC} Overriding template: ${ORIGINAL_TEMPLATE:-unset} -> $TEMPLATE"
            CREATED_TEMP_ENV=true
        fi
    fi

    ACTIVE_TEMPLATE="$(grep -E '^MINER_TEMPLATE=' "$PROJECT_DIR/.env" 2>/dev/null | cut -d= -f2 || echo 'gatk')"
    echo -e "  ${GREEN}[OK]${NC} Variant caller: ${CYAN}$ACTIVE_TEMPLATE${NC}"
else
    echo -e "  ${YELLOW}[WARN]${NC} No .env file found — creating a temporary demo config"

    ACTIVE_TEMPLATE="${TEMPLATE:-gatk}"

    cat > "$PROJECT_DIR/.env" <<ENVEOF
# Temporary demo configuration — created by demo.sh
# Copy .env.miner.example to .env and customize for production use.
NETUID=107
WALLET_NAME=default
WALLET_HOTKEY=default
MINER_TEMPLATE=$ACTIVE_TEMPLATE
PLATFORM_URL=https://api.theminos.ai
PLATFORM_TIMEOUT=60
STORAGE_PRIMARY_BACKEND=hippius
ENVEOF

    CREATED_TEMP_ENV=true
    echo -e "  ${GREEN}[OK]${NC} Temporary .env created (template: ${CYAN}$ACTIVE_TEMPLATE${NC})"
fi

echo ""

# ---------------------------------------------------------------------------
# Cleanup function — restore .env and remove temp files
# ---------------------------------------------------------------------------
cleanup() {
    if [[ "$CREATED_TEMP_ENV" == "true" ]]; then
        if [[ -f "$PROJECT_DIR/.env.demo.bak" ]]; then
            # We modified an existing .env — restore it
            mv "$PROJECT_DIR/.env.demo.bak" "$PROJECT_DIR/.env"
        elif grep -q "created by demo.sh" "$PROJECT_DIR/.env" 2>/dev/null; then
            # We created a throwaway .env — remove it
            rm -f "$PROJECT_DIR/.env"
        fi
    fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Step 3: Run the miner and capture output
# ---------------------------------------------------------------------------
echo -e "${BOLD}--- Step 3/3: Scoring your config ---${NC}"
echo ""
echo -e "  Starting miner with template ${CYAN}$ACTIVE_TEMPLATE${NC}..."
echo -e "  The miner will download a fixed, fully-answered sample, run variant"
echo -e "  calling inside Docker, and self-score the result against truth."
echo ""
echo -e "  ${YELLOW}You will see miner logs below. This may take several minutes.${NC}"
echo -e "  ${YELLOW}It ends automatically once your score is printed.${NC}"
echo ""
echo -e "  ---- miner output begin ----"
echo ""

# Delegate to start-miner.sh --demo. That is the single place that prepares
# everything scoring needs — Docker images, the chr20 reference, AND the RTG
# SDF (required by vcfeval) — before exec'ing the one-shot scorer. Running the
# miner directly here would skip the SDF fetch and fail on a fresh box.
DEMO_RC=0
(
    cd "$PROJECT_DIR"
    bash start-miner.sh --demo
) || DEMO_RC=$?

echo ""
echo -e "  ---- miner output end ----"
echo ""

if [[ $DEMO_RC -ne 0 ]]; then
    echo -e "  ${RED}[FAIL]${NC} Demo run exited with an error (code $DEMO_RC)."
    echo -e "  Check the output above for the cause (Docker, reference data, or"
    echo -e "  variant-caller error). You can retry with:"
    echo -e "    cd $PROJECT_DIR && bash start-miner.sh --demo"
    exit 1
fi

# ---------------------------------------------------------------------------
# Scoring explanation
# ---------------------------------------------------------------------------
echo ""
echo -e "${BOLD}================================================================${NC}"
echo -e "${BOLD}  How Scoring Works on Minos SN107${NC}"
echo -e "${BOLD}================================================================${NC}"
echo ""
echo -e "  When live rounds are active, validators score your VCF using"
echo -e "  ${CYAN}hap.py${NC} (Illumina's variant comparison tool) against a truth set."
echo -e "  The ${CYAN}AdvancedScorer${NC} combines metrics into a 0-100 final score:"
echo ""
echo -e "  ${BOLD}Score Components:${NC}"
echo -e "    Core F1        60%  — truth-weighted F1 across SNPs and INDELs"
echo -e "    Completeness   15%  — recall and coverage"
echo -e "    FP Rate        15%  — false positive penalty"
echo -e "    Quality        10%  — Ti/Tv and Het/Hom ratio consistency"
echo ""
echo -e "  The raw score (0-100) is normalized to 0-1 for current-round ranking."
echo ""
echo -e "  ${BOLD}Typical score ranges (0-100):${NC}"
echo -e "    80 - 95+  ${GREEN}Excellent${NC}  — competitive for top rewards"
echo -e "    60 - 80   ${YELLOW}Good${NC}       — solid but room to optimize"
echo -e "    40 - 60   ${YELLOW}Fair${NC}       — check tool parameters"
echo -e "    < 40      ${RED}Needs work${NC} — likely a configuration issue"
echo ""
echo -e "  ${BOLD}Tips to improve your score:${NC}"
echo -e "    - Tune parameters in ${CYAN}configs/$ACTIVE_TEMPLATE.conf${NC}"
echo -e "    - Try different variant callers (GATK and DeepVariant score highest)"
echo -e "    - Ensure enough RAM is available for your tool"
echo -e "      (DeepVariant: 8GB+, GATK: 4GB+, bcftools: 2GB+)"
echo ""

# ---------------------------------------------------------------------------
# Next steps
# ---------------------------------------------------------------------------
echo -e "${BOLD}================================================================${NC}"
echo -e "${BOLD}  Next Steps${NC}"
echo -e "${BOLD}================================================================${NC}"
echo ""
echo -e "  ${BOLD}1. Register on the subnet:${NC}"
echo -e "     btcli subnets register --netuid 107 \\"
echo -e "       --wallet.name <your_wallet> --wallet.hotkey <your_hotkey>"
echo ""
echo -e "  ${BOLD}2. Configure your .env:${NC}"
echo -e "     cp .env.miner.example .env"
echo -e "     # Edit .env with your wallet name, hotkey, and preferred template"
echo ""
echo -e "  ${BOLD}3. (Optional) Tune variant-caller parameters:${NC}"
echo -e "     Edit ${CYAN}configs/$ACTIVE_TEMPLATE.conf${NC} to tweak tool-specific settings."
echo -e "     See templates/${ACTIVE_TEMPLATE}.py for supported parameters."
echo ""
echo -e "  ${BOLD}4. Run the miner for real:${NC}"
echo -e "     cd $PROJECT_DIR"
echo -e "     bash start-miner.sh          # interactive (recommended first time)"
echo -e "     bash pm2-miner.sh            # PM2 (auto-restart, background)"
echo ""
echo -e "     The miner will poll for rounds every 30 seconds and"
echo -e "     automatically participate in each 72-minute round cycle."
echo ""
echo -e "  ${BOLD}5. Monitor your miner:${NC}"
echo -e "     pm2 logs minos-miner         # if using PM2"
echo -e "     bash scripts/verify.sh       # check environment"
echo -e "     Dashboard: ${CYAN}https://app.theminos.ai${NC}"
echo ""
echo -e "${BOLD}================================================================${NC}"

echo -e "  ${GREEN}Demo completed successfully. Your miner is ready!${NC}"

echo -e "${BOLD}================================================================${NC}"
echo ""
