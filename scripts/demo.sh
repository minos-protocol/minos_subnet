#!/usr/bin/env bash
# Minos Subnet 107 — Interactive Demo
# Runs a single demo round end-to-end so a new miner can verify
# that their variant-calling pipeline works before going live.
#
# What happens:
#   1. Runs verify.sh to check prerequisites (Docker, Python, etc.)
#   2. Ensures a .env file exists (creates a temporary demo one if not)
#   3. Starts the miner, which connects to the platform in demo mode
#   4. The miner downloads a BAM file, runs your chosen variant caller,
#      and attempts to submit — the platform responds with "demo complete"
#   5. This script inspects the output VCF and prints a summary
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
            echo "Runs a single demo round to verify your miner setup."
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
echo -e "  This script runs a ${CYAN}single demo round${NC} end-to-end so you can"
echo -e "  confirm that your miner setup works before going live."
echo ""
echo -e "  What will happen:"
echo -e "    1. Verify prerequisites (Docker, Python packages)"
echo -e "    2. Start the miner with ${CYAN}--demo${NC} — no wallet needed, no chain conn."
echo -e "    3. The miner connects to ${CYAN}https://api.theminos.ai/v2/demo/*${NC}"
echo -e "    4. It downloads a sandbox BAM and runs your variant caller"
echo -e "    5. Results are displayed here with a score estimate"
echo ""
echo -e "  ${YELLOW}Estimated time: 3-30 minutes${NC} (depends on your machine and"
echo -e "  the variant caller — bcftools is fastest, DeepVariant slowest)"
echo ""
echo -e "  Press Ctrl+C at any time to abort."
echo ""

# ---------------------------------------------------------------------------
# Step 1: Run verify.sh
# ---------------------------------------------------------------------------
echo -e "${BOLD}--- Step 1/4: Verifying prerequisites ---${NC}"
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
echo -e "${BOLD}--- Step 2/4: Checking configuration ---${NC}"
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
echo -e "${BOLD}--- Step 3/4: Running demo round ---${NC}"
echo ""
echo -e "  Starting miner with template ${CYAN}$ACTIVE_TEMPLATE${NC}..."
echo -e "  The miner will poll the platform for the demo round, download"
echo -e "  a BAM file, and run variant calling inside Docker."
echo ""
echo -e "  ${YELLOW}You will see miner logs below. This may take several minutes.${NC}"
echo -e "  ${YELLOW}The demo ends automatically after one round completes.${NC}"
echo ""
echo -e "  ---- miner output begin ----"
echo ""

# We run the miner and watch for the demo-complete signal.
# The miner runs forever in a polling loop, so we need to kill it
# once we see it has completed the demo round.
#
# Strategy: run the miner in background, tail its output, and kill it
# once we see the "DEMO COMPLETE" or "demo mode" banner, or after
# a timeout.

DEMO_LOG="$PROJECT_DIR/.demo_output.log"
DEMO_TIMEOUT=1800  # 30 minutes max
DEMO_PID=""

# Clean up any previous log
rm -f "$DEMO_LOG"

# Resolve venv Python
PYTHON="python3"
if [[ -f "$PROJECT_DIR/.venv/bin/python3" ]]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python3"
fi

# Run the miner in the background, capturing output. --demo routes the
# client to /v2/demo/* (no wallet required, no chain connection) so the
# script works even if the user hasn't run setup.py / has no registered
# hotkey. Demo path is sandboxed: no submissions are persisted, no TAO
# is earned — purely a pipeline-test loop.
(
    cd "$PROJECT_DIR"
    $PYTHON -m neurons.miner --demo 2>&1
) > "$DEMO_LOG" 2>&1 &
DEMO_PID=$!

# Also clean up child process on exit
cleanup_all() {
    if [[ -n "$DEMO_PID" ]] && kill -0 "$DEMO_PID" 2>/dev/null; then
        kill "$DEMO_PID" 2>/dev/null || true
        wait "$DEMO_PID" 2>/dev/null || true
    fi
    rm -f "$DEMO_LOG"
    cleanup
}
trap cleanup_all EXIT

# Follow the log in real time, watching for completion signals
SECONDS_WAITED=0
DEMO_COMPLETE=false
LAST_LINE=0

while [[ $SECONDS_WAITED -lt $DEMO_TIMEOUT ]]; do
    # Check if miner process is still running
    if ! kill -0 "$DEMO_PID" 2>/dev/null; then
        # Process ended — print remaining output
        if [[ -f "$DEMO_LOG" ]]; then
            tail -n +$((LAST_LINE + 1)) "$DEMO_LOG" 2>/dev/null || true
        fi
        break
    fi

    # Print new lines from the log
    if [[ -f "$DEMO_LOG" ]]; then
        NEW_LINES=$(wc -l < "$DEMO_LOG" 2>/dev/null || echo 0)
        if [[ "$NEW_LINES" -gt "$LAST_LINE" ]]; then
            tail -n +$((LAST_LINE + 1)) "$DEMO_LOG" | head -n $((NEW_LINES - LAST_LINE))
            LAST_LINE=$NEW_LINES
        fi

        # Check for demo completion signals
        if grep -q "DEMO COMPLETE" "$DEMO_LOG" 2>/dev/null; then
            DEMO_COMPLETE=true
            sleep 2  # Let the miner finish printing
            # Print any final lines
            NEW_LINES=$(wc -l < "$DEMO_LOG" 2>/dev/null || echo 0)
            if [[ "$NEW_LINES" -gt "$LAST_LINE" ]]; then
                tail -n +$((LAST_LINE + 1)) "$DEMO_LOG" | head -n $((NEW_LINES - LAST_LINE))
            fi
            break
        fi

        # Also check for errors that indicate the round was processed
        if grep -q "Total rounds participated: 1" "$DEMO_LOG" 2>/dev/null; then
            DEMO_COMPLETE=true
            sleep 2
            NEW_LINES=$(wc -l < "$DEMO_LOG" 2>/dev/null || echo 0)
            if [[ "$NEW_LINES" -gt "$LAST_LINE" ]]; then
                tail -n +$((LAST_LINE + 1)) "$DEMO_LOG" | head -n $((NEW_LINES - LAST_LINE))
            fi
            break
        fi
    fi

    sleep 2
    SECONDS_WAITED=$((SECONDS_WAITED + 2))
done

echo ""
echo -e "  ---- miner output end ----"
echo ""

# Kill the miner if it is still running
if [[ -n "$DEMO_PID" ]] && kill -0 "$DEMO_PID" 2>/dev/null; then
    kill "$DEMO_PID" 2>/dev/null || true
    wait "$DEMO_PID" 2>/dev/null || true
fi

# Check what happened
if [[ $SECONDS_WAITED -ge $DEMO_TIMEOUT ]]; then
    echo -e "  ${RED}[TIMEOUT]${NC} Demo did not complete within $((DEMO_TIMEOUT / 60)) minutes."
    echo -e "  This might mean:"
    echo -e "    - No demo round is currently active on the platform"
    echo -e "    - Docker is slow to start on your machine"
    echo -e "    - The variant caller hit an error"
    echo ""
    echo -e "  Check the full log: ${CYAN}$DEMO_LOG${NC}"
    echo -e "  Or try running the miner directly:"
    echo -e "    cd $PROJECT_DIR && bash start-miner.sh"
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 4: Inspect results
# ---------------------------------------------------------------------------
echo -e "${BOLD}--- Step 4/4: Results summary ---${NC}"
echo ""

# Find the most recent output VCF in the working directory
VCF_FILE=""
ROUND_DIR=""

# The miner writes output to output/<round_id>/output.vcf.gz
if [[ -d "$PROJECT_DIR/output" ]]; then
    # Find the newest output.vcf.gz
    VCF_FILE=$(find "$PROJECT_DIR/output" -name "output.vcf.gz" -type f -newer "$DEMO_LOG" 2>/dev/null | head -1 || true)
    if [[ -z "$VCF_FILE" ]]; then
        # Fallback: find any output.vcf.gz, sorted by time
        VCF_FILE=$(find "$PROJECT_DIR/output" -name "output.vcf.gz" -type f 2>/dev/null | xargs ls -t 2>/dev/null | head -1 || true)
    fi
fi

if [[ -n "$VCF_FILE" && -f "$VCF_FILE" ]]; then
    ROUND_DIR="$(dirname "$VCF_FILE")"
    VCF_SIZE=$(du -h "$VCF_FILE" | cut -f1)

    echo -e "  ${GREEN}[OK]${NC} Output VCF found: $VCF_FILE ($VCF_SIZE)"
    echo ""

    # Count variants
    VARIANT_COUNT=0
    SNP_COUNT=0
    INDEL_COUNT=0

    if command -v zcat &>/dev/null || command -v gzcat &>/dev/null; then
        # macOS uses gzcat, Linux uses zcat
        ZCAT="zcat"
        if [[ "$(uname)" == "Darwin" ]]; then
            ZCAT="gzcat"
        fi

        VARIANT_COUNT=$($ZCAT "$VCF_FILE" 2>/dev/null | grep -v "^#" | wc -l | tr -d ' ' || echo 0)

        # Count SNPs vs INDELs (SNP = REF and ALT are both single chars)
        SNP_COUNT=$($ZCAT "$VCF_FILE" 2>/dev/null | grep -v "^#" | awk 'length($4)==1 && length($5)==1' | wc -l | tr -d ' ' || echo 0)
        INDEL_COUNT=$((VARIANT_COUNT - SNP_COUNT))

        echo -e "  ${BOLD}Variant Summary:${NC}"
        echo -e "    Total variants: ${CYAN}$VARIANT_COUNT${NC}"
        echo -e "    SNPs:           ${CYAN}$SNP_COUNT${NC}"
        echo -e "    INDELs:         ${CYAN}$INDEL_COUNT${NC}"
        echo ""

        # Show first 10 data lines
        echo -e "  ${BOLD}First 10 variant calls:${NC}"
        echo ""
        $ZCAT "$VCF_FILE" 2>/dev/null | grep -v "^#" | head -10 | while IFS=$'\t' read -r CHROM POS ID REF ALT QUAL FILTER REST; do
            # Determine variant type
            if [[ ${#REF} -eq 1 && ${#ALT} -eq 1 ]]; then
                VTYPE="SNP"
            else
                VTYPE="INDEL"
            fi
            printf "    %-6s %-12s %s>%s  QUAL=%-8s %s\n" "$CHROM" "$POS" "$REF" "$ALT" "$QUAL" "$VTYPE"
        done
        echo ""

        if [[ $VARIANT_COUNT -gt 10 ]]; then
            echo -e "    ... and $((VARIANT_COUNT - 10)) more variants"
            echo ""
        fi
    else
        echo -e "  ${YELLOW}[WARN]${NC} Cannot decompress VCF (zcat/gzcat not found)"
        echo -e "  File exists at: $VCF_FILE"
    fi
else
    if [[ "$DEMO_COMPLETE" == "true" ]]; then
        echo -e "  ${YELLOW}[INFO]${NC} Demo round completed but no VCF file found."
        echo -e "  The demo round may have been a connectivity-only test."
    else
        echo -e "  ${RED}[FAIL]${NC} No output VCF found. The variant caller may have failed."
        echo -e "  Check the miner logs above for error messages."
    fi
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
echo -e "  The raw score (0-100) is normalized to 0-1 for EMA tracking."
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

if [[ "$DEMO_COMPLETE" == "true" ]]; then
    echo -e "  ${GREEN}Demo completed successfully. Your miner is ready!${NC}"
else
    echo -e "  ${YELLOW}Demo finished (check output above for any issues).${NC}"
fi

echo -e "${BOLD}================================================================${NC}"
echo ""
